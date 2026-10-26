"""Ingest one paper by external id: resolve it upstream, mint a `doc_id`, commit its manifest.

The door into the corpus for a caller holding a DOI or a PMID and nothing else — the on-demand half of
`docs/design/evidence-fulltext.md`. `pipeline.py` is the other ingestion core (a seed object already in
the bucket, wrapped for Dataflow by `ingest_beam.py`); this one starts from an identifier, so the paper
it commits has no sources and no renderings, and the worker's steps (`produce.run`) fill it in from the
ids alone.

Three steps, in one order: resolve the id upstream, `crosswalk.mint` the paper's ids, then
`writer.write_paper` commits the manifest. Only an id the resolution answers for is minted, and the two
writes are idempotent, so a crash between them leaves a claimed `doc_id` with no manifest and the re-run
adopts it and completes.

**This path never rewrites a committed manifest, so an id is claimed only where it also writes the manifest naming it.**
That rules out two things it would otherwise do, and it logs both rather than doing them: adding the
requested id to a paper the corpus already holds, and writing the equivalence edge when the ids bridge
two `doc_id`s. Either would mean rewriting a committed manifest, and a claim without one is a crosswalk
row `rebuild` deletes. The caller's answer is unaffected — the paper resolves to the canonical `doc_id`
either way — and the repairs are left to a sweep, off the request path (`pipeline._link_equivalence` is
the bulk ingester's version of the second).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence

import httpx2
from google.cloud import storage as gcs

from themis.litcache import crosswalk, identity, openalex, resolve, writer
from themis.litcache.models import litcache_pb2

_logger = logging.getLogger(__name__)

# The id schemes `resolve.resolve_metadata` keys on: a PMID through PubMed efetch, a DOI through
# Crossref. A PMCID reaches neither rung, so a paper known only by its PMCID cannot be ingested here —
# the caller filters on this rather than handing over an id the ladder would refuse.
RESOLVABLE_SCHEMES = frozenset({'doi', 'pmid'})

# The schemes minted and carried into the manifest. `ExternalIds` also models `arxiv`/`biorxiv`, which
# neither rung of the metadata ladder harvests.
_MINTED_SCHEMES = ('doi', 'pmid', 'pmcid')

# The crosswalk read and the crosswalk mint, over `{scheme}:{value}` keys. Injected rather than taken
# as a connection, so the caller owns the dial and any pooling — as in `pipeline.ingest_paper`.
Lookup = Callable[[Iterable[str]], dict[str, str]]
Minter = Callable[[Iterable[str]], crosswalk.MintResult]

# `resolve.resolve_metadata`'s shape, injected so the ladder can be faked offline in tests.
MetadataResolver = Callable[..., Awaitable[resolve.ResolvedPaper]]


class UnstorableRecordError(Exception):
    """The record an upstream answered with is not one the store can keep.

    Permanent for that id: the record exists and resolving it again returns the same bytes, so a
    caller that retries reaches the same refusal. Typed apart from the `ValueError` a malformed
    *response* raises on its way through the ladder — a truncated JSON body is an outage, and a caller
    told that permanently would never ask again.
    """


@dataclasses.dataclass(frozen=True)
class IngestResult:
    """What ingesting one paper did.

    Attributes:
        doc_id: The `doc_id` the paper now claims — freshly minted, or an incumbent's, adopted.
        minted: True when the `doc_id` is new; False when an incumbent was adopted (another id of the
            same paper was already in the crosswalk).
        written: True when this call committed the manifest; False when the paper was already
            committed under the adopted `doc_id`.
    """

    doc_id: str
    minted: bool
    written: bool


def manifest_external_ids(external_ids: Sequence[identity.ExternalId]) -> litcache_pb2.ExternalIds:
    """Map a paper's minted ids to the manifest `ExternalIds` (doi/pmid/pmcid).

    Only the schemes the manifest models; `pii`/`binhash` have no field. What goes in here is what was
    minted — an id in the manifest that no crosswalk row holds is unreachable by the caller who
    supplied it.

    Args:
        external_ids: The ids claimed for this paper, any scheme.

    Returns:
        The `ExternalIds` for the manifest, carrying whichever of doi/pmid/pmcid is present.
    """
    by_scheme = {eid.scheme: eid.value for eid in external_ids}
    present = {scheme: by_scheme[scheme] for scheme in _MINTED_SCHEMES if scheme in by_scheme}
    return litcache_pb2.ExternalIds(**present)


async def ingest_unknown_paper(
    bucket: gcs.Bucket,
    lookup: Lookup,
    mint: Minter,
    external_id: identity.ExternalId,
    *,
    http_client: httpx2.AsyncClient,
    openalex_api_key: openalex.ApiKey,
    resolve_metadata: MetadataResolver = resolve.resolve_metadata,
) -> IngestResult | None:
    """Bring the paper `external_id` names into the corpus, if an upstream holds it.

    Resolves the id upstream, then claims the paper's ids and commits a manifest carrying them and the
    bibliographic record it resolved — no source bytes and no rendering, so the paper reads PENDING and
    the conversion lane fills it in. A paper an already-claimed cross-id shows the corpus to hold is
    adopted rather than claimed again.

    Args:
        bucket: The cache bucket to commit the paper into.
        lookup: The crosswalk read, over a connection the caller owns.
        mint: The crosswalk mint, over a connection the caller owns.
        external_id: The id to ingest; its scheme must be in `RESOLVABLE_SCHEMES`.
        http_client: The async HTTP client the metadata ladder issues its calls on (caller owns its
            lifecycle).
        openalex_api_key: The key the ladder's OpenAlex requests carry.
        resolve_metadata: The metadata ladder; defaults to `resolve.resolve_metadata`. Injected so
            tests run offline.

    Returns:
        The `IngestResult`, or `None` when no upstream holds the id — a settled per-id fact, not an
        error, and nothing was claimed or written.

    Raises:
        ValueError: If `external_id`'s scheme is not in `RESOLVABLE_SCHEMES` — the ladder has no rung
            for it, so a caller must filter rather than have it silently read as "no such paper".
            Permanent, and a caller's bug rather than a fact about the paper.
        UnstorableRecordError: If the resolved record is not one `writer.write_paper` will keep.
            Permanent for that id.
        efetch.RecordPreconditionError, upstream_record.SchemaDriftError, openalex.AmbiguousWorkError: If
            an index holds a record of the paper this store cannot take (`resolve.resolve_metadata`).
            Permanent for that id.
        httpx2.HTTPError: On an upstream failure that is not a clean miss — transient, so it propagates
            rather than being recorded as a paper that does not exist.
        RuntimeError: If the mint did not converge within its retry bound (`crosswalk.mint`) — a bug,
            not an expected outcome.
        pg8000.exceptions.DatabaseError: On any crosswalk failure that is not the insert race `mint`
            itself absorbs.
    """
    if external_id.scheme not in RESOLVABLE_SCHEMES:
        raise ValueError(f'cannot ingest {external_id.key!r}: resolvable schemes are {sorted(RESOLVABLE_SCHEMES)}')
    try:
        resolved = await resolve_metadata(
            pmid=external_id.value if external_id.scheme == 'pmid' else None,
            doi=external_id.value if external_id.scheme == 'doi' else None,
            http_client=http_client,
            openalex_api_key=openalex_api_key,
        )
    except resolve.MetadataUnresolvedError:
        return None
    # The crosswalk read and the two writes are blocking (Cloud SQL, GCS); off-load them so an async
    # caller's event loop keeps serving while this paper lands.
    return await asyncio.to_thread(_commit, bucket, lookup, mint, external_id, resolved)


def _commit(
    bucket: gcs.Bucket,
    lookup: Lookup,
    mint: Minter,
    external_id: identity.ExternalId,
    resolved: resolve.ResolvedPaper,
) -> IngestResult:
    """Claim the paper's ids and commit its manifest, or adopt the `doc_id` the corpus already holds."""
    ids = _paper_ids(external_id, resolved.external_ids)
    keys = [eid.key for eid in ids]
    incumbent = _committed_incumbent(bucket, lookup(keys))
    if incumbent is not None:
        # A committed paper: adopt its `doc_id` and claim nothing. This path will not rewrite that
        # manifest, so claiming the requested id would leave a crosswalk row no manifest names — and
        # `rebuild`, which inverts the manifests, drops exactly those. The caller still gets the
        # `doc_id`; what it does not get is the crosswalk learning the id it asked by.
        _logger.info('%s names a paper the corpus already holds as %s; claiming nothing', external_id.key, incumbent)
        return IngestResult(doc_id=incumbent, minted=False, written=False)
    # No incumbent, or only ones whose manifest a crashed run never wrote: claim and commit. `mint`
    # adopts the lowest orphaned `doc_id` rather than minting beside it, so the re-run completes it.
    result = mint(keys)
    paper = writer.PaperInput(
        doc_id=result.doc_id,
        external_ids=manifest_external_ids(ids),
        claim_key=identity.claim_key(ids),
        equivalence=litcache_pb2.Equivalence(edges=[], canonical_doc_id=result.doc_id),
        retraction=litcache_pb2.Retraction(),
        sources=[],
        renderings=[],
        metadata=resolved.metadata,
    )
    try:
        write = writer.write_paper(bucket, paper)
    except ValueError as e:
        # Typed where it is raised, so a caller can tell it from the `ValueError` any malformed
        # upstream response raises somewhere up the ladder.
        raise UnstorableRecordError(str(e)) from e
    if not write.written:
        # A concurrent ingest of the same paper won the race between the read above and this commit.
        # Its manifest carries the same ids (both callers minted from the same upstream record), so the
        # corpus is consistent; logged because the alternative — a claim its manifest does not name —
        # is what a rebuild would drop.
        _logger.info('%s was committed concurrently as %s', external_id.key, result.doc_id)
    return IngestResult(doc_id=result.doc_id, minted=result.minted, written=write.written)


def _committed_incumbent(bucket: gcs.Bucket, held: Mapping[str, str]) -> str | None:
    """The lowest `doc_id` among the held ones whose manifest is committed, or None if none is.

    Every held `doc_id` is checked, not only the lowest. Ids can bridge an orphaned claim — a run that
    crashed before its manifest — and a committed paper, and if the orphan is the lower of the two,
    adopting it would commit a second manifest naming ids the committed one already names: the work
    held twice, and a crosswalk `rebuild` refuses. A committed paper is always the one adopted.

    Two or more distinct `doc_id`s means the corpus holds one work twice — an inconsistency this path
    discovers and does not create. The lowest is canonical, matching `crosswalk.mint`; recording the
    equivalence edge means rewriting a committed manifest, which this path never does.
    """
    doc_ids = sorted(set(held.values()))
    if len(doc_ids) > 1:
        _logger.warning('external ids bridge %d separately-held papers: %s', len(doc_ids), doc_ids)
    return next((doc_id for doc_id in doc_ids if bucket.blob(writer.manifest_path(doc_id)).exists()), None)


def _paper_ids(external_id: identity.ExternalId, resolved: litcache_pb2.ExternalIds) -> tuple[identity.ExternalId, ...]:
    """The paper's ids: the requested one, plus a harvested cross-id for each scheme it leaves free.

    Harvesting is what makes the paper reachable by the ids the caller did not ask by — without them
    the same work asked for later by its DOI would take a second `doc_id`. At most one id per scheme,
    because `ExternalIds` holds one: a second would be minted into a crosswalk row the manifest cannot
    name. Where they collide the requested spelling wins, since that is the one the caller will ask by
    again — so a DOI alias resolves and the primary DOI upstream returned for it does not.
    """
    ids: dict[str, identity.ExternalId] = {external_id.scheme: external_id}
    for scheme in _MINTED_SCHEMES:
        value = getattr(resolved, scheme)
        if resolved.HasField(scheme) and value:
            ids.setdefault(scheme, identity.ExternalId(scheme=scheme, value=value))
    return tuple(sorted(ids.values(), key=lambda eid: eid.key))
