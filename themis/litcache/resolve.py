"""Resolve a paper's identifier to its `metadata.pb` (a `SourcedMetadata`).

Its source record carries the resolving index's record: PubMed's journal or book record as efetch
answers a PMID, in the `pubmed` field, or for a paper PubMed does not index a Crossref or OpenAlex
record, as JSON text. Once the ladder settles which index resolved the paper, OpenAlex is asked for
the same work (`openalex.find`) by the PMID of a PubMed record, and on a miss by its DOI, or by the
DOI of a Crossref record, and its work is stored beside the resolving record
(docs/design/litcache-manifest.md, "The resolving index's record, and OpenAlex's beside it"); a
paper OpenAlex resolved is not looked up again. The lookup has four answers: one work, stored
beside; none, the resolving record stored alone; more than one, the paper not stored
(`AmbiguousWorkFailure`, `openalex.AmbiguousWorkError`); and a failure to reach OpenAlex, which
fails the call as a failure to reach the resolving index does. The metadata is
filled from the records the source record holds. Two entry points:

- `resolve_metadata` — the per-paper ladder: a PMID resolves through PubMed efetch
  (`themis.litcache.efetch`), else the DOI falls to Crossref (`themis.litcache.crossref`).
  A full miss raises `MetadataUnresolvedError`: no index has a record, so there is nothing
  to put in the source record. A PMID efetch answers with a record that fails the store's
  precondition raises `efetch.RecordPreconditionError`; Crossref is not tried in its place.
  A Crossref record Themis cannot read or store raises `upstream_record.SchemaDriftError`, as does an
  OpenAlex work of the paper; OpenAlex holding more than one work for the paper raises
  `openalex.AmbiguousWorkError`.
- `resolve_batch` — the bulk entry point the ingestion pipeline uses, fully batched to
  eliminate the per-paper rate domain. PMIDs go through batched efetch; DOI-only papers
  take an all-batched DOI path (`_resolve_doi_batch`): litfetch's batched resolver
  (`litfetch.resolvers.default_batch_resolver`) fills each DOI's pmid/pmcid in bulk — a
  discovered pmid routes back into efetch for a PubMed-native record; the no-pmid
  residual is looked up in OpenAlex (`themis.litcache.openalex`), whose work's PMID routes
  into efetch too, and only a work stating none is the paper's record. It does
  *not* use per-DOI Crossref (un-batchable, rate-limited), and returns partial results
  (an unresolved paper is absent, not raised — a batch is not failed by one member). A
  paper efetch answers with a record that fails the store's precondition is a
  `RecordPreconditionFailure` carrying the reason: the record exists, so no other source is
  tried in its place. An OpenAlex record Themis cannot read or store is a `SchemaDriftFailure`
  carrying the message naming the key, and OpenAlex holding more than one work for a paper's
  identifier an `AmbiguousWorkFailure` naming the works; each is charged to its paper alone.

efetch harvests the cross-ids from the record's own id lists: DOI↔PMID↔PMCID for a
journal record, PMID and the Bookshelf accession for a book record.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence

import httpx2
import litfetch
from litfetch import resolvers

from themis.litcache import crossref, efetch, openalex, sourced_metadata, upstream_record
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
    there is no record to put in the source record.
    """

    def __init__(self, *, pmid: str | None, doi: str | None) -> None:
        super().__init__(f'no bibliographic metadata resolvable (pmid={pmid!r}, doi={doi!r})')
        self.pmid = pmid
        self.doi = doi


@dataclasses.dataclass(frozen=True)
class ResolvedPaper:
    """The bibliographic outputs of resolving one paper through the ladder.

    Attributes:
        metadata: The canonical `metadata.pb` bytes (a serialized `SourcedMetadata` carrying the
            resolving index's record).
        external_ids: The cross-ids harvested for the manifest. The efetch rung
            harvests doi/pmid/pmcid from a journal record and pmid/bookid (and any
            DOI) from a book record; the Crossref rung carries the DOI only; the
            OpenAlex rung carries the DOI and the pmcid the work states.
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
    """A paper whose index record Themis cannot read or store (`upstream_record.SchemaDriftError`).

    The record exists, so the paper is not unknown and no other source is tried in its place. The
    fault is a field a reader reads holding a value of another shape, or a number standard JSON text
    cannot carry; it is repaired in that reader, and no `metadata.pb` is written until then.

    Attributes:
        reason: The message naming the key, as `upstream_record.SchemaDriftError.detail` states it.
    """

    reason: str


@dataclasses.dataclass(frozen=True)
class AmbiguousWorkFailure:
    """A paper for whose identifier OpenAlex holds more than one work (`openalex.AmbiguousWorkError`).

    Taking either work could give the paper another work's authors, so no `metadata.pb` is written; the paper is
    reviewed, and ingested again once OpenAlex holds one work for it.

    Attributes:
        reason: The identifier and the OpenAlex ids of its works, as `openalex.AmbiguousWorkError` states them.
    """

    reason: str


type Outcome = ResolvedPaper | RecordPreconditionFailure | SchemaDriftFailure | AmbiguousWorkFailure


@dataclasses.dataclass(frozen=True)
class _Settled:
    """A paper the ladder resolved, before OpenAlex's work of it is looked up.

    Attributes:
        sourced: The resolving index's record, and the metadata filled from it alone.
        external_ids: As `ResolvedPaper.external_ids`.
        publisher: As `ResolvedPaper.publisher`.
        openalex_work: OpenAlex's work of the paper when the ladder already found it (the work whose PMID routed a
            DOI to PubMed), stored beside without a lookup; `None` when it is still to be looked up.
    """

    sourced: litcache_pb2.SourcedMetadata
    external_ids: litcache_pb2.ExternalIds
    publisher: str | None
    openalex_work: openalex.Work | None = None


def _openalex_identifiers(settled: _Settled) -> list[tuple[openalex.Scheme, str]]:
    """The identifiers OpenAlex is asked for the paper's work by, in turn.

    None when OpenAlex resolved the paper, or the ladder already found its work (`_Settled.openalex_work`).

    A paper PubMed resolved is asked for by its PMID, and on a miss by its DOI when it has one; a paper Crossref
    resolved by its DOI.

    Raises:
        ValueError: If the resolving record's identifier is not among the paper's cross-ids.
    """
    source, ids = settled.sourced.source, settled.external_ids
    if settled.openalex_work is not None:
        return []
    if source.HasField('pubmed'):
        identifiers = [(openalex.Scheme.PMID, ids.pmid)] + ([(openalex.Scheme.DOI, ids.doi)] if ids.doi else [])
    elif source.HasField('crossref_json'):
        identifiers = [(openalex.Scheme.DOI, ids.doi)]
    else:
        return []
    scheme, identifier = identifiers[0]
    if not identifier:
        raise ValueError(f'a paper resolved by its {scheme} carries no {scheme} among its cross-ids')
    return identifiers


async def _look_up(
    identifiers: Iterable[tuple[openalex.Scheme, str]],
    *,
    http_client: httpx2.AsyncClient,
    openalex_api_key: openalex.ApiKey,
) -> dict[openalex.Scheme, openalex.Found]:
    """OpenAlex's works for `identifiers`, one batched lookup per scheme that has any.

    Raises:
        httpx2.HTTPError: If OpenAlex cannot be reached or answers with a non-2xx status.
        ValueError: As `openalex.find`.
        upstream_record.SchemaDriftError: As `openalex.find`: a work that cannot be charged to one identifier.
    """
    wanted = list(identifiers)
    found: dict[openalex.Scheme, openalex.Found] = {}
    for scheme in openalex.Scheme:
        values = sorted({value for s, value in wanted if s is scheme})
        found[scheme] = (
            await openalex.find(scheme, values, http_client=http_client, api_key=openalex_api_key)
            if values
            else openalex.Found(scheme=scheme, works={}, ambiguous={}, drifted={})
        )
    return found


async def _look_up_beside(
    papers: Iterable[_Settled], *, http_client: httpx2.AsyncClient, openalex_api_key: openalex.ApiKey
) -> dict[openalex.Scheme, openalex.Found]:
    """OpenAlex's works for the papers, each asked for by its identifiers in turn (`_openalex_identifiers`).

    Each round asks, batched per scheme, the next identifier of every paper OpenAlex holds no work for by the ones
    asked before; a paper's lookup ends at its first identifier OpenAlex answers, with one work or more.

    Raises:
        httpx2.HTTPError: As `_look_up`.
        ValueError: As `_look_up`.
        upstream_record.SchemaDriftError: As `_look_up`.
    """
    found = {scheme: openalex.Found(scheme=scheme, works={}, ambiguous={}, drifted={}) for scheme in openalex.Scheme}
    asked: set[tuple[openalex.Scheme, str]] = set()
    pending = [identifiers for identifiers in map(_openalex_identifiers, papers) if identifiers]
    while pending:
        wanted = {identifiers[0] for identifiers in pending} - asked
        for scheme, answered in (
            await _look_up(wanted, http_client=http_client, openalex_api_key=openalex_api_key)
        ).items():
            found[scheme].update(answered)
        asked |= wanted
        pending = [rest for (scheme, value), *rest in pending if rest and found[scheme].holds_none(value)]
    return found


def _work_beside(settled: _Settled, found: Mapping[openalex.Scheme, openalex.Found]) -> openalex.Work | None:
    """OpenAlex's one work of the paper, from its first identifier OpenAlex answers, or `None` when it holds none.

    The work the ladder already found is taken as it is. A work found by the DOI of a paper PubMed resolved that states
    another PMID is another paper's, so none.

    Raises:
        openalex.AmbiguousWorkError: If OpenAlex holds more than one work for that identifier.
        upstream_record.SchemaDriftError: If that work cannot be stored, or a PMID it states cannot be read.
    """
    if settled.openalex_work is not None:
        return settled.openalex_work
    for scheme, value in _openalex_identifiers(settled):
        work = found[scheme].work(value)
        if work is None:
            continue
        pmid = settled.external_ids.pmid if settled.sourced.source.HasField('pubmed') else None
        if scheme is openalex.Scheme.DOI and pmid and openalex.stated_pmid(work) not in (None, pmid):
            return None
        return work
    return None


def _paper(settled: _Settled, found: Mapping[openalex.Scheme, openalex.Found]) -> ResolvedPaper:
    """The paper's `ResolvedPaper`: its resolving record, with OpenAlex's work beside it where `found` holds one.

    Raises:
        openalex.AmbiguousWorkError: As `_work_beside`.
        upstream_record.SchemaDriftError: As `_work_beside`, or if a metadata field cannot be read from the records.
    """
    sourced = settled.sourced
    work = _work_beside(settled, found)
    if work is not None:
        sourced = openalex.paired(sourced, work)
    return ResolvedPaper(
        metadata=sourced_metadata.to_canonical_bytes(sourced),
        external_ids=settled.external_ids,
        publisher=settled.publisher,
    )


async def _papers(
    settled: Mapping[str, _Settled], *, http_client: httpx2.AsyncClient, openalex_api_key: openalex.ApiKey
) -> dict[str, ResolvedPaper | SchemaDriftFailure | AmbiguousWorkFailure]:
    """Look up OpenAlex's work of every settled paper, batched per scheme and round, and finish each (`_paper`).

    Raises:
        httpx2.HTTPError: As `_look_up_beside`.
        ValueError: As `_look_up_beside`.
        upstream_record.SchemaDriftError: As `_look_up_beside`.
    """
    found = await _look_up_beside(settled.values(), http_client=http_client, openalex_api_key=openalex_api_key)
    outcomes: dict[str, ResolvedPaper | SchemaDriftFailure | AmbiguousWorkFailure] = {}
    for join_key, paper in settled.items():
        try:
            outcomes[join_key] = _paper(paper, found)
        except openalex.AmbiguousWorkError as e:
            outcomes[join_key] = AmbiguousWorkFailure(reason=str(e))
        except upstream_record.SchemaDriftError as e:
            outcomes[join_key] = SchemaDriftFailure(reason=e.detail)
    return outcomes


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


async def resolve_metadata(
    *, pmid: str | None, doi: str | None, http_client: httpx2.AsyncClient, openalex_api_key: openalex.ApiKey
) -> ResolvedPaper:
    """Resolve one paper's bibliographic metadata via the efetch → Crossref ladder, with OpenAlex's work beside it.

    OpenAlex's work of a paper PubMed resolved is looked up by its PMID, and when OpenAlex holds none, by its DOI; of a
    paper Crossref resolved, by its DOI (`_openalex_identifiers`).

    Args:
        pmid: The paper's PMID, or `None` — tried first via efetch when present.
        doi: The paper's DOI, or `None` — the Crossref fallback when efetch finds
            nothing.
        http_client: The async HTTP client (caller owns its lifecycle).
        openalex_api_key: The key every OpenAlex request carries (`openalex.find`).

    Returns:
        The `ResolvedPaper` from the first rung that resolves.

    Raises:
        MetadataUnresolvedError: If neither rung resolves the paper (fully unknown).
        efetch.RecordPreconditionError: If efetch answers the PMID with a record that fails the
            store's precondition; the record exists, so the DOI rung is not tried in its place.
        upstream_record.SchemaDriftError: If Crossref's record or OpenAlex's work cannot be read or stored; the record
            exists, so nothing else is tried in its place.
        openalex.AmbiguousWorkError: If OpenAlex holds more than one work for the paper's PMID or DOI.
        httpx2.HTTPError: On a transport failure or a non-2xx status from any index other than Crossref's 404
            (a transient error the caller retries — distinct from a clean miss).
    """
    settled = await _settle_one(pmid=pmid, doi=doi, http_client=http_client)
    found = await _look_up_beside([settled], http_client=http_client, openalex_api_key=openalex_api_key)
    return _paper(settled, found)


async def _settle_one(*, pmid: str | None, doi: str | None, http_client: httpx2.AsyncClient) -> _Settled:
    """The record of the first rung of the efetch → Crossref ladder that resolves the paper (see `resolve_metadata`)."""
    if pmid is not None:
        response = await efetch.resolve([pmid], http_client=http_client)
        record = response.resolved.get(pmid)
        if record is not None:
            return _Settled(sourced=record.sourced, external_ids=record.external_ids, publisher=None)
        if pmid in response.precondition_failed:
            raise efetch.RecordPreconditionError(response.precondition_failed[pmid])

    if doi is not None:
        result = await _crossref_or_none(doi, http_client=http_client)
        if result is not None:
            return _Settled(sourced=result.sourced, external_ids=result.external_ids, publisher=result.publisher)

    raise MetadataUnresolvedError(pmid=pmid, doi=doi)


@dataclasses.dataclass(frozen=True)
class ResolveRequest:
    """One paper's identifiers to resolve, tagged by the key its outcome is returned under.

    Attributes:
        join_key: The key the outcome is returned under (ingestion: the seed's claim key and
            identifiers; a metadata refresh: the `doc_id`). Requests sharing a key share one
            outcome, so they must carry the same identifiers.
        pmid: The paper's PMID, or `None`.
        doi: The paper's DOI, or `None`.
    """

    join_key: str
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


def _efetch_outcome(efetched: efetch.ParsedResponse, pmid: str | None) -> _Settled | RecordPreconditionFailure | None:
    """What efetch's answer settles for `pmid`: resolved, a failed precondition, or `None` (not answered)."""
    if pmid is None:
        return None
    record = efetched.resolved.get(pmid)
    if record is not None:
        return _Settled(sourced=record.sourced, external_ids=record.external_ids, publisher=None)
    reason = efetched.precondition_failed.get(pmid)
    if reason is not None:
        return RecordPreconditionFailure(reason=reason)
    return None


async def resolve_batch(
    requests: Sequence[ResolveRequest],
    *,
    http_client: httpx2.AsyncClient,
    session: litfetch.Session,
    openalex_api_key: openalex.ApiKey,
) -> dict[str, Outcome]:
    """Resolve a batch of papers by identifier, batching the NCBI calls.

    The batched analogue of `resolve_metadata`, fully batched to eliminate the
    per-paper NCBI rate domain: every known PMID resolves through batched efetch (one
    call per `_ID_CALL_LIMIT`); the papers efetch does not return, with the DOI-only
    papers, take a DOI path (`_resolve_doi_batch`) that is also all-batched — litfetch's
    batched resolver fills pmid/pmcid, a discovered pmid routes back into efetch, and
    the no-pmid residual is looked up in OpenAlex: a work stating a PMID routes it into
    efetch as well, and only a work stating none is the paper's record. No per-DOI Crossref.
    OpenAlex's work of every paper PubMed resolved is then looked up by PMID in batches
    (`openalex.find`), the papers it holds no work for again by DOI, and stored beside
    its record.

    Unlike `resolve_metadata`, a paper resolvable by no path is simply absent from the
    result — the caller's `unknown`, surfaced when the write stage finds no entry for
    its `join_key`, never raised here (a batch is not failed by one unresolvable
    member). Likewise a paper efetch answers with a record that fails the store's
    precondition is a `RecordPreconditionFailure` in the result, not raised: the fault is that
    record's alone, and the DOI path is not tried in its place. Likewise a paper whose OpenAlex
    record cannot be read or stored is a `SchemaDriftFailure`, and one OpenAlex holds more than
    one work for an `AmbiguousWorkFailure`, each charged to it alone.

    Args:
        requests: The papers to resolve (deduplicated on identifier internally).
        http_client: The async HTTP client for efetch / OpenAlex metadata (caller owns
            its lifecycle).
        session: An entered litfetch `Session` the batched id-resolver issues its
            NCBI / Europe PMC / OpenAlex lookups on.
        openalex_api_key: The key every OpenAlex request carries, this module's own lookups
            (`openalex.find`) and litfetch's batched resolver alike.

    Returns:
        A mapping of `join_key` → the paper's outcome: a `ResolvedPaper`, a
        `RecordPreconditionFailure` carrying the reason, a `SchemaDriftFailure` naming the
        key, or an `AmbiguousWorkFailure` naming the works. A paper resolved through efetch
        (directly or via a discovered pmid) carries efetch's harvested cross-ids; a paper
        OpenAlex resolved carries its DOI and pmcid.

    Raises:
        httpx2.HTTPError: On a transport failure or a non-2xx status from efetch or OpenAlex
            (transient; the caller retries the batch).
        ValueError: If two requests share a `join_key` but not their identifiers. Also if an
            efetch answer does not read as one record per PMID (`efetch.parse_set`): the parse
            itself is then not trustworthy, so the chunk fails rather than any one paper; likewise
            if an OpenAlex answer does not read as works of the identifiers asked (`openalex.find`).
        upstream_record.SchemaDriftError: If an OpenAlex work's `id`, or the identifier it was
            looked up by, cannot be read, so it cannot be charged to one paper (`openalex.find`).
    """
    _check_join_keys(requests)
    settled: dict[str, _Settled | Outcome] = {}

    pmids = sorted({r.pmid for r in requests if r.pmid is not None})
    efetched = await _efetch_batched(pmids, http_client=http_client)

    doi_requests: list[ResolveRequest] = []
    for request in requests:
        outcome = _efetch_outcome(efetched, request.pmid)
        if outcome is not None:
            settled[request.join_key] = outcome
        elif request.doi is not None:
            doi_requests.append(request)

    if doi_requests:
        settled.update(
            await _resolve_doi_batch(
                doi_requests, http_client=http_client, session=session, openalex_api_key=openalex_api_key
            )
        )
    outcomes: dict[str, Outcome] = {k: v for k, v in settled.items() if not isinstance(v, _Settled)}
    outcomes.update(
        await _papers(
            {k: v for k, v in settled.items() if isinstance(v, _Settled)},
            http_client=http_client,
            openalex_api_key=openalex_api_key,
        )
    )
    return outcomes


def _check_join_keys(requests: Sequence[ResolveRequest]) -> None:
    """Raise if two requests share a `join_key` but not their identifiers: one outcome would answer both."""
    identifiers: dict[str, tuple[str | None, str | None]] = {}
    for request in requests:
        carried = (request.pmid, request.doi)
        first = identifiers.setdefault(request.join_key, carried)
        if first != carried:
            raise ValueError(f'join key {request.join_key!r} names two identifier sets: {first} and {carried}')


async def _resolve_doi_batch(
    requests: Sequence[ResolveRequest],
    *,
    http_client: httpx2.AsyncClient,
    session: litfetch.Session,
    openalex_api_key: openalex.ApiKey,
) -> dict[str, _Settled | Outcome]:
    """Resolve DOI-keyed papers, batched throughout — no per-DOI Crossref.

    litfetch's batched resolver (NCBI ID Converter → Europe PMC → OpenAlex) fills each DOI's pmid/pmcid in bulk. A DOI
    it finds no pmid for, one whose lookup it abandoned included, is looked up in OpenAlex with Themis's key
    (`openalex.find`), since litfetch's resolver returns ids, not the record: a work stating a PMID gives the paper its
    pmid when PubMed's record of that PMID states no DOI or this one, and is stored beside PubMed's record without
    another lookup; otherwise (a work stating no PMID, a preprint, say) the work is the paper's record. Every pmid
    either finds routes into one batched efetch for a PubMed-native record. A DOI neither resolves is absent, as is one
    whose pmid efetch does not answer; one whose pmid efetch answers with a record that fails the store's precondition
    is a `RecordPreconditionFailure`; one whose OpenAlex work cannot be read or stored is a `SchemaDriftFailure`, and
    one OpenAlex holds more than one work for an `AmbiguousWorkFailure`.
    """
    dois = sorted({r.doi for r in requests if r.doi is not None})
    bundles = [litfetch.ArticleIds(doi=doi) for doi in dois]
    resolver = resolvers.default_batch_resolver(openalex_api_key=openalex_api_key.value)
    enriched, abandoned = await resolver(bundles, session)
    if abandoned:
        _LOG.warning('litfetch resolver abandoned %d of %d DOI lookups (transient)', len(abandoned), len(dois))
    # Key by each bundle's own doi (never overwritten by resolution): an abandoned or
    # unmatched bundle stays present but un-enriched, falling through to OpenAlex
    # rather than misaligning to another paper's ids or failing the whole batch.
    cross_ids = {bundle.doi: bundle for bundle in enriched}

    in_openalex = await _in_openalex(
        [doi for doi in dois if cross_ids[doi].pmid is None], http_client=http_client, openalex_api_key=openalex_api_key
    )
    pmids = {doi: cross_ids[doi].pmid or _stated_pmid(in_openalex.get(doi)) for doi in dois}
    efetched = await _efetch_batched(sorted({p for p in pmids.values() if p is not None}), http_client=http_client)

    outcomes: dict[str, _Settled | Outcome] = {}
    for request in requests:
        if request.doi is None:
            continue
        outcome = _doi_outcome(request.doi, cross_ids[request.doi], in_openalex.get(request.doi), efetched)
        if outcome is not None:
            outcomes[request.join_key] = outcome
    return outcomes


def _doi_outcome(
    doi: str,
    litfetched: litfetch.ArticleIds,
    in_openalex: _InOpenAlex | SchemaDriftFailure | AmbiguousWorkFailure | None,
    efetched: efetch.ParsedResponse,
) -> _Settled | Outcome | None:
    """What the DOI path settles for `doi` (`_resolve_doi_batch`), or `None` when nothing resolves it.

    A PMID OpenAlex's work states settles the paper through PubMed only when PubMed's record of it states no DOI or
    `doi` (case-folded); a record stating another DOI is another paper's, so the paper is OpenAlex-resolved, claiming no
    PMID. Settled through PubMed, the paper carries that work to store beside, not looked up again.
    """
    if isinstance(in_openalex, SchemaDriftFailure | AmbiguousWorkFailure):
        return in_openalex
    if litfetched.pmid is not None:
        return _efetch_outcome(efetched, litfetched.pmid)
    if in_openalex is None:
        return None
    if in_openalex.pmid is None:
        return _openalex_resolved(doi, in_openalex.work, pmcid=litfetched.pmcid)
    outcome = _efetch_outcome(efetched, in_openalex.pmid)
    if not isinstance(outcome, _Settled):
        return outcome
    stated = outcome.external_ids.doi
    if stated and openalex.key(openalex.Scheme.DOI, stated) != openalex.key(openalex.Scheme.DOI, doi):
        return _openalex_resolved(doi, in_openalex.work, pmcid=litfetched.pmcid)
    return dataclasses.replace(outcome, openalex_work=in_openalex.work)


@dataclasses.dataclass(frozen=True)
class _InOpenAlex:
    """A DOI's one OpenAlex work, and the PMID it states (`None`: not in PubMed)."""

    work: openalex.Work
    pmid: str | None


def _stated_pmid(answer: _InOpenAlex | SchemaDriftFailure | AmbiguousWorkFailure | None) -> str | None:
    return answer.pmid if isinstance(answer, _InOpenAlex) else None


async def _in_openalex(
    dois: Sequence[str], *, http_client: httpx2.AsyncClient, openalex_api_key: openalex.ApiKey
) -> dict[str, _InOpenAlex | SchemaDriftFailure | AmbiguousWorkFailure]:
    """OpenAlex's answer for each of `dois` it holds a work for, the work read only for the PMID it states.

    Raises:
        httpx2.HTTPError: As `_look_up`.
        ValueError: As `_look_up`.
        upstream_record.SchemaDriftError: As `_look_up`.
    """
    found = (
        await _look_up(
            [(openalex.Scheme.DOI, doi) for doi in dois], http_client=http_client, openalex_api_key=openalex_api_key
        )
    )[openalex.Scheme.DOI]
    answers: dict[str, _InOpenAlex | SchemaDriftFailure | AmbiguousWorkFailure] = {}
    for doi in dois:
        try:
            work = found.work(doi)
            if work is not None:
                answers[doi] = _InOpenAlex(work=work, pmid=openalex.stated_pmid(work))
        except openalex.AmbiguousWorkError as e:
            answers[doi] = AmbiguousWorkFailure(reason=str(e))
        except upstream_record.SchemaDriftError as e:
            answers[doi] = SchemaDriftFailure(reason=e.detail)
    return answers


def _openalex_resolved(doi: str, work: openalex.Work, *, pmcid: str | None) -> _Settled | SchemaDriftFailure:
    """A paper OpenAlex resolved: its work the only record, with the cross-ids and publisher read off it.

    Args:
        doi: The paper's DOI.
        work: OpenAlex's one work of it, stating no PMID.
        pmcid: The PMCID litfetch found for the DOI, preferred to the work's; `None` when it found none.
    """
    try:
        fields = openalex.read_work(work.record)
        sourced = openalex.sourced(work)
    except upstream_record.SchemaDriftError as e:
        return SchemaDriftFailure(reason=e.detail)
    return _Settled(
        sourced=sourced,
        external_ids=litcache_pb2.ExternalIds(doi=doi, pmcid=pmcid or fields.pmcid),
        publisher=fields.publisher,
    )
