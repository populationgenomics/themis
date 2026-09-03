"""Resolve a paper's identifier to its `metadata.pb` (a `PaperMetadata` envelope).

The envelope carries the resolving index's record whole, in that index's own schema: PubMed's
journal or book record as efetch answers a PMID, in the envelope's `pubmed` field, or a Crossref
or OpenAlex record in its own field for a paper PubMed does not index. Two entry points:

- `resolve_metadata` — the per-paper ladder: a PMID resolves through PubMed efetch
  (`themis.litcache.efetch`), else the DOI falls to Crossref (`themis.litcache.crossref`).
  A full miss raises `MetadataUnresolvedError`: no index has a record, so there is nothing
  to put in the envelope. A PMID efetch answers with a record that fails the store's
  precondition raises `efetch.RecordPreconditionError`; Crossref is not tried in its place.
  A Crossref record that does not fit its mirror raises `mirror.SchemaDriftError`.
- `resolve_batch` — the bulk entry point the ingestion pipeline uses, fully batched to
  eliminate the per-paper rate domain. PMIDs go through batched efetch; DOI-only papers
  take an all-batched DOI path (`_resolve_doi_batch`): litfetch's batched resolver
  (`litfetch.resolvers.default_batch_resolver`) fills each DOI's pmid/pmcid in bulk — a
  discovered pmid routes back into efetch for a PubMed-native record; the no-pmid
  residual takes an OpenAlex (`themis.litcache.openalex`) bibliographic record. It does
  *not* use per-DOI Crossref (un-batchable, rate-limited), and returns partial results
  (an unresolved paper is absent, not raised — a batch is not failed by one member). A
  paper efetch answers with a record that fails the store's precondition is a
  `RecordPreconditionFailure` carrying the reason: the record exists, so no other source is
  tried in its place. An OpenAlex record that does not fit its mirror is a `SchemaDriftFailure`
  carrying the parser's message, charged to its paper alone.

efetch harvests the cross-ids from the record's own id lists: DOI↔PMID↔PMCID for a
journal record, PMID and the Bookshelf accession for a book record.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Iterator, Sequence

import httpx2
import litfetch
from litfetch import resolvers

from themis.litcache import crossref, efetch, openalex
from themis.litcache.models import litcache_pb2

_LOG = logging.getLogger(__name__)

# Crossref answers an unknown DOI with 404 — that is the paper's "unknown", a
# resolver miss, not a transport failure; other statuses propagate (transient).
_NOT_FOUND = 404
# Crossref rate-limits a bursty fan-out with 429; it is retried with backoff rather
# than failing the batch (one 429 in the gather would sink every paper in it).
_TOO_MANY_REQUESTS = 429
_CROSSREF_MAX_ATTEMPTS = 5

# efetch caps a PMID list at 200; a resolve batch larger than that is split into this
# many ids per efetch call. litfetch's batched resolver self-chunks at its own caps.
_ID_CALL_LIMIT = 200


class MetadataUnresolvedError(Exception):
    """Neither efetch nor Crossref resolved a paper — it is fully unknown.

    Carries the identifiers tried so the orchestrator can surface the paper in the
    `unknown`-metadata diagnostics rather than invent a record: no index answered, so
    there is no record to put in the envelope.
    """

    def __init__(self, *, pmid: str | None, doi: str | None) -> None:
        super().__init__(f'no bibliographic metadata resolvable (pmid={pmid!r}, doi={doi!r})')
        self.pmid = pmid
        self.doi = doi


@dataclasses.dataclass(frozen=True)
class ResolvedPaper:
    """The bibliographic outputs of resolving one paper through the ladder.

    Attributes:
        metadata: The canonical `metadata.pb` bytes (a serialized `PaperMetadata`
            envelope carrying the resolving index's record).
        external_ids: The cross-ids harvested for the manifest. The efetch rung
            harvests doi/pmid/pmcid from a journal record and pmid/bookid (and any
            DOI) from a book record; the Crossref rung carries the DOI only; the
            OpenAlex rung carries the DOI and the pmid/pmcid the work states.
        publisher: The Crossref or OpenAlex publisher (for `Licensed` access); `None`
            on the efetch rung (a PubMed record states none).
    """

    metadata: bytes
    external_ids: litcache_pb2.ExternalIds
    publisher: str | None


@dataclasses.dataclass(frozen=True)
class RecordPreconditionFailure:
    """A paper whose efetch record fails the store's precondition (`efetch.RecordPreconditionError`).

    The record exists and converts, so the paper is not unknown and no other source is tried in
    its place; the fault is in the record, to be reviewed, and no `metadata.pb` is written for it.

    Attributes:
        reason: Which precondition the record fails, as `efetch.parse_response` states it.
    """

    reason: str


@dataclasses.dataclass(frozen=True)
class SchemaDriftFailure:
    """A paper whose index record does not fit its mirror (`mirror.SchemaDriftError`).

    The record exists, so the paper is not unknown and no other source is tried in its place; the
    fault is the mirror's lag, repaired by its field, and no `metadata.pb` is written until then.

    Attributes:
        reason: The parser's message naming the key, as `mirror.SchemaDriftError.detail` states it.
    """

    reason: str


type Outcome = ResolvedPaper | RecordPreconditionFailure | SchemaDriftFailure


def _retry_after_seconds(response: httpx2.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429: the `Retry-After` header, else backoff."""
    header = response.headers.get('retry-after')
    if header is not None and header.isdigit():
        return float(header)
    return float(2**attempt)


async def _crossref_or_none(doi: str, *, http_client: httpx2.AsyncClient) -> crossref.CrossrefResult | None:
    """Resolve a DOI via Crossref, mapping a 404 (unknown DOI) to `None`.

    A 429 is retried with backoff (honoring `Retry-After`) up to `_CROSSREF_MAX_ATTEMPTS`
    — a rate response is transient and must not fail the surrounding batch. Other
    non-2xx statuses propagate.
    """
    for attempt in range(_CROSSREF_MAX_ATTEMPTS):
        try:
            return await crossref.resolve(doi, http_client=http_client)
        except httpx2.HTTPStatusError as e:
            if e.response.status_code == _NOT_FOUND:
                return None
            if e.response.status_code != _TOO_MANY_REQUESTS or attempt == _CROSSREF_MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(_retry_after_seconds(e.response, attempt))
    raise AssertionError('unreachable: the loop returns or raises on the final attempt')


async def resolve_metadata(*, pmid: str | None, doi: str | None, http_client: httpx2.AsyncClient) -> ResolvedPaper:
    """Resolve one paper's bibliographic metadata via the efetch → Crossref ladder.

    Args:
        pmid: The paper's PMID, or `None` — tried first via efetch when present.
        doi: The paper's DOI, or `None` — the Crossref fallback when efetch finds
            nothing.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The `ResolvedPaper` from the first rung that resolves.

    Raises:
        MetadataUnresolvedError: If neither rung resolves the paper (fully unknown).
        efetch.RecordPreconditionError: If efetch answers the PMID with a record that fails the
            store's precondition; the record exists, so the DOI rung is not tried in its place.
        mirror.SchemaDriftError: If Crossref's record does not fit its mirror; the record exists,
            so nothing else is tried in its place.
        httpx2.HTTPStatusError: On a non-404 transport failure from either source
            (a transient error the caller retries — distinct from a clean miss).
    """
    if pmid is not None:
        response = await efetch.resolve([pmid], http_client=http_client)
        record = response.resolved.get(pmid)
        if record is not None:
            return ResolvedPaper(metadata=record.metadata, external_ids=record.external_ids, publisher=None)
        if pmid in response.precondition_failed:
            raise efetch.RecordPreconditionError(response.precondition_failed[pmid])

    if doi is not None:
        result = await _crossref_or_none(doi, http_client=http_client)
        if result is not None:
            return ResolvedPaper(metadata=result.metadata, external_ids=result.external_ids, publisher=result.publisher)

    raise MetadataUnresolvedError(pmid=pmid, doi=doi)


@dataclasses.dataclass(frozen=True)
class ResolveRequest:
    """One paper's identifiers to resolve, tagged by the key results join back on.

    Attributes:
        claim_key: The key results join back on; the result map is keyed by it
            (ingestion: the identity claim key; a metadata refresh: the `doc_id`).
        pmid: The paper's PMID, or `None`.
        doi: The paper's DOI, or `None`.
    """

    claim_key: str
    pmid: str | None
    doi: str | None


def _chunk(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    """Yield `items` in slices of at most `size` (the underlying id-call ceiling)."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def _efetch_batched(pmids: Sequence[str], *, http_client: httpx2.AsyncClient) -> efetch.ParsedResponse:
    """Resolve `pmids` through efetch in chunks of `_ID_CALL_LIMIT`, merging the chunks' answers."""
    resolved: dict[str, efetch.ResolvedMetadata] = {}
    precondition_failed: dict[str, str] = {}
    for chunk in _chunk(pmids, _ID_CALL_LIMIT):
        response = await efetch.resolve(chunk, http_client=http_client)
        resolved.update(response.resolved)
        precondition_failed.update(response.precondition_failed)
    return efetch.ParsedResponse(resolved=resolved, precondition_failed=precondition_failed)


def _efetch_outcome(
    efetched: efetch.ParsedResponse, pmid: str | None
) -> ResolvedPaper | RecordPreconditionFailure | None:
    """What efetch's answer settles for `pmid`: resolved, a failed precondition, or `None` (not answered)."""
    if pmid is None:
        return None
    record = efetched.resolved.get(pmid)
    if record is not None:
        return ResolvedPaper(metadata=record.metadata, external_ids=record.external_ids, publisher=None)
    reason = efetched.precondition_failed.get(pmid)
    if reason is not None:
        return RecordPreconditionFailure(reason=reason)
    return None


async def resolve_batch(
    requests: Sequence[ResolveRequest], *, http_client: httpx2.AsyncClient, session: litfetch.Session
) -> dict[str, Outcome]:
    """Resolve a batch of papers by identifier, batching the NCBI calls.

    The batched analogue of `resolve_metadata`, fully batched to eliminate the
    per-paper NCBI rate domain: every known PMID resolves through batched efetch (one
    call per `_ID_CALL_LIMIT`); the papers efetch does not return, with the DOI-only
    papers, take a DOI path (`_resolve_doi_batch`) that is also all-batched — litfetch's
    batched resolver fills pmid/pmcid, a discovered pmid routes back into efetch, and
    the no-pmid residual takes an OpenAlex bibliographic record. No per-DOI Crossref.

    Unlike `resolve_metadata`, a paper resolvable by no path is simply absent from the
    result — the caller's `unknown`, surfaced when the write stage finds no entry for
    its `claim_key`, never raised here (a batch is not failed by one unresolvable
    member). Likewise a paper efetch answers with a record that fails the store's
    precondition is a `RecordPreconditionFailure` in the result, not raised: the fault is that
    record's alone, and the DOI path is not tried in its place. Likewise a paper whose OpenAlex
    record does not fit its mirror is a `SchemaDriftFailure`, charged to it alone.

    Args:
        requests: The papers to resolve (deduplicated on identifier internally).
        http_client: The async HTTP client for efetch / OpenAlex metadata (caller owns
            its lifecycle).
        session: An entered litfetch `Session` the batched id-resolver issues its
            NCBI / Europe PMC / OpenAlex lookups on.

    Returns:
        A mapping of `claim_key` → the paper's outcome: a `ResolvedPaper`, a
        `RecordPreconditionFailure` carrying the reason, or a `SchemaDriftFailure` naming the
        key. A paper resolved through efetch (directly or via a discovered pmid) carries
        efetch's harvested cross-ids; the non-PubMed residual carries OpenAlex's doi/pmid/pmcid.

    Raises:
        httpx2.HTTPStatusError: On a non-404 transport failure (transient; the caller
            retries the batch).
        ValueError: If an efetch answer does not read as one record per PMID
            (`efetch.parse_set`) — then the parse itself is not trustworthy, and the
            chunk fails rather than any one paper.
    """
    outcomes: dict[str, Outcome] = {}

    pmids = sorted({r.pmid for r in requests if r.pmid is not None})
    efetched = await _efetch_batched(pmids, http_client=http_client)

    doi_requests: list[ResolveRequest] = []
    for request in requests:
        outcome = _efetch_outcome(efetched, request.pmid)
        if outcome is not None:
            outcomes[request.claim_key] = outcome
        elif request.doi is not None:
            doi_requests.append(request)

    if doi_requests:
        outcomes.update(await _resolve_doi_batch(doi_requests, http_client=http_client, session=session))
    return outcomes


async def _resolve_doi_batch(
    requests: Sequence[ResolveRequest], *, http_client: httpx2.AsyncClient, session: litfetch.Session
) -> dict[str, Outcome]:
    """Resolve DOI-keyed papers, batched throughout — no per-DOI Crossref.

    litfetch's batched resolver (NCBI ID Converter → Europe PMC → OpenAlex) fills each
    DOI's pmid/pmcid in bulk. Any discovered pmid routes back into a batched efetch for
    a PubMed-native record; a DOI with no pmid at all (a preprint) takes an OpenAlex
    bibliographic record — a second, metadata-only OpenAlex call scoped to that
    residual, since litfetch's resolver returns ids, not the record. A DOI neither the
    resolver nor OpenAlex resolves is absent; one whose discovered pmid efetch answers
    with a record that fails the store's precondition is a `RecordPreconditionFailure`; one
    whose OpenAlex record does not fit its mirror is a `SchemaDriftFailure`.
    """
    dois = sorted({r.doi for r in requests if r.doi is not None})
    bundles = [litfetch.ArticleIds(doi=doi) for doi in dois]
    enriched, abandoned = await resolvers.default_batch_resolver()(bundles, session)
    if abandoned:
        _LOG.warning('litfetch resolver abandoned %d of %d DOI lookups (transient)', len(abandoned), len(dois))
    # Key by each bundle's own doi (never overwritten by resolution): an abandoned or
    # unmatched bundle stays present but un-enriched, falling through as unresolved
    # rather than misaligning to another paper's ids or failing the whole batch.
    cross_ids = {bundle.doi: bundle for bundle in enriched}

    pmids = sorted({bundle.pmid for bundle in enriched if bundle.pmid is not None})
    efetched = await _efetch_batched(pmids, http_client=http_client)

    residual = [doi for doi in dois if cross_ids[doi].pmid is None]
    parsed = await openalex.resolve(residual, http_client=http_client) if residual else openalex.ParsedWorks({}, {})

    outcomes: dict[str, Outcome] = {}
    for request in requests:
        if request.doi is None:
            continue
        outcome = _efetch_outcome(efetched, cross_ids[request.doi].pmid)
        if outcome is not None:
            outcomes[request.claim_key] = outcome
            continue
        work = parsed.works.get(request.doi)
        if work is not None:
            pmcid = cross_ids[request.doi].pmcid or openalex.bare_pmcid(work)
            outcomes[request.claim_key] = ResolvedPaper(
                metadata=openalex.to_metadata(work),
                external_ids=litcache_pb2.ExternalIds(doi=request.doi, pmid=openalex.bare_pmid(work), pmcid=pmcid),
                publisher=openalex.publisher(work),
            )
        elif request.doi in parsed.drifted:
            outcomes[request.claim_key] = SchemaDriftFailure(reason=parsed.drifted[request.doi])
    return outcomes
