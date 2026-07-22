"""Resolve a paper's identifier to its `metadata.pb` (a pubmed_proto `PubmedArticle`).

Two entry points:

- `resolve_metadata` — the per-paper ladder: a PMID resolves through PubMed efetch
  (`themis.litcache.efetch`), else the DOI falls to Crossref (`themis.litcache.crossref`).
  A full miss raises `MetadataUnresolvedError` — `PubmedArticle`'s required title /
  `pub_date` cannot be satisfied without inventing data.
- `resolve_batch` — the bulk entry point the ingestion pipeline uses, fully batched to
  eliminate the per-paper rate domain. PMIDs go through batched efetch; DOI-only papers
  take an all-batched DOI path (`_resolve_doi_batch`): litfetch's batched resolver
  (`litfetch.resolvers.default_batch_resolver`) fills each DOI's pmid/pmcid in bulk — a
  discovered pmid routes back into efetch for a PubMed-native record; the no-pmid
  residual takes an OpenAlex (`themis.litcache.openalex`) bibliographic record. It does
  *not* use per-DOI Crossref (un-batchable, rate-limited), and returns partial results
  (an unresolved paper is absent, not raised — a batch is not failed by one member).

efetch harvests the cross-ids (DOI↔PMID↔PMCID) from the record's own id list.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Iterator, Sequence

import httpx
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
    `unknown`-metadata diagnostics rather than invent a record.
    """

    def __init__(self, *, pmid: str | None, doi: str | None) -> None:
        super().__init__(f'no bibliographic metadata resolvable (pmid={pmid!r}, doi={doi!r})')
        self.pmid = pmid
        self.doi = doi


@dataclasses.dataclass(frozen=True)
class ResolvedPaper:
    """The bibliographic outputs of resolving one paper through the ladder.

    Attributes:
        metadata: The canonical `metadata.pb` bytes (serialized pubmed_proto
            `PubmedArticle`).
        external_ids: The cross-ids harvested for the manifest. The efetch rung
            harvests doi/pmid/pmcid from the record; the Crossref rung carries the
            DOI only.
        publisher: The Crossref publisher (for `Licensed` access); `None` on the
            efetch rung (PubMed records carry no publisher in this mapping).
    """

    metadata: bytes
    external_ids: litcache_pb2.ExternalIds
    publisher: str | None


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429: the `Retry-After` header, else backoff."""
    header = response.headers.get('retry-after')
    if header is not None and header.isdigit():
        return float(header)
    return float(2**attempt)


async def _crossref_or_none(doi: str, *, http_client: httpx.AsyncClient) -> crossref.CrossrefResult | None:
    """Resolve a DOI via Crossref, mapping a 404 (unknown DOI) to `None`.

    A 429 is retried with backoff (honoring `Retry-After`) up to `_CROSSREF_MAX_ATTEMPTS`
    — a rate response is transient and must not fail the surrounding batch. Other
    non-2xx statuses propagate.
    """
    for attempt in range(_CROSSREF_MAX_ATTEMPTS):
        try:
            return await crossref.resolve(doi, http_client=http_client)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == _NOT_FOUND:
                return None
            if e.response.status_code != _TOO_MANY_REQUESTS or attempt == _CROSSREF_MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(_retry_after_seconds(e.response, attempt))
    raise AssertionError('unreachable: the loop returns or raises on the final attempt')


async def resolve_metadata(*, pmid: str | None, doi: str | None, http_client: httpx.AsyncClient) -> ResolvedPaper:
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
        httpx.HTTPStatusError: On a non-404 transport failure from either source
            (a transient error the caller retries — distinct from a clean miss).
    """
    if pmid is not None:
        resolved = await efetch.resolve([pmid], http_client=http_client)
        record = resolved.get(pmid)
        if record is not None:
            return ResolvedPaper(metadata=record.metadata, external_ids=record.external_ids, publisher=None)

    if doi is not None:
        result = await _crossref_or_none(doi, http_client=http_client)
        if result is not None:
            return ResolvedPaper(metadata=result.metadata, external_ids=result.external_ids, publisher=result.publisher)

    raise MetadataUnresolvedError(pmid=pmid, doi=doi)


@dataclasses.dataclass(frozen=True)
class ResolveRequest:
    """One paper's identifiers to resolve, tagged by the key results join back on.

    Attributes:
        claim_key: The paper's join key (identity's precedence-primary id); the
            result map is keyed by it.
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


async def resolve_batch(
    requests: Sequence[ResolveRequest], *, http_client: httpx.AsyncClient, session: litfetch.Session
) -> dict[str, ResolvedPaper]:
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
    member).

    Args:
        requests: The papers to resolve (deduplicated on identifier internally).
        http_client: The async HTTP client for efetch / OpenAlex metadata (caller owns
            its lifecycle).
        session: An entered litfetch `Session` the batched id-resolver issues its
            NCBI / Europe PMC / OpenAlex lookups on.

    Returns:
        A mapping of `claim_key` → `ResolvedPaper` for each resolved paper. A paper
        resolved through efetch (directly or via a discovered pmid) carries efetch's
        harvested cross-ids; the non-PubMed residual carries OpenAlex's doi/pmid/pmcid.

    Raises:
        httpx.HTTPStatusError: On a non-404 transport failure (transient; the caller
            retries the batch).
    """
    resolved: dict[str, ResolvedPaper] = {}

    pmids = sorted({r.pmid for r in requests if r.pmid is not None})
    efetched: dict[str, efetch.ResolvedMetadata] = {}
    for chunk in _chunk(pmids, _ID_CALL_LIMIT):
        efetched.update(await efetch.resolve(chunk, http_client=http_client))

    doi_requests: list[ResolveRequest] = []
    for request in requests:
        record = efetched.get(request.pmid) if request.pmid is not None else None
        if record is not None:
            resolved[request.claim_key] = ResolvedPaper(
                metadata=record.metadata, external_ids=record.external_ids, publisher=None
            )
        elif request.doi is not None:
            doi_requests.append(request)

    if doi_requests:
        resolved.update(await _resolve_doi_batch(doi_requests, http_client=http_client, session=session))
    return resolved


async def _resolve_doi_batch(
    requests: Sequence[ResolveRequest], *, http_client: httpx.AsyncClient, session: litfetch.Session
) -> dict[str, ResolvedPaper]:
    """Resolve DOI-keyed papers, batched throughout — no per-DOI Crossref.

    litfetch's batched resolver (NCBI ID Converter → Europe PMC → OpenAlex) fills each
    DOI's pmid/pmcid in bulk. Any discovered pmid routes back into a batched efetch for
    a PubMed-native record; a DOI with no pmid at all (a preprint) takes an OpenAlex
    bibliographic record — a second, metadata-only OpenAlex call scoped to that
    residual, since litfetch's resolver returns ids, not the record. A DOI neither the
    resolver nor OpenAlex resolves is absent.
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
    efetched: dict[str, efetch.ResolvedMetadata] = {}
    for chunk in _chunk(pmids, _ID_CALL_LIMIT):
        efetched.update(await efetch.resolve(chunk, http_client=http_client))

    residual = [doi for doi in dois if cross_ids[doi].pmid is None]
    works = await openalex.resolve(residual, http_client=http_client) if residual else {}

    resolved: dict[str, ResolvedPaper] = {}
    for request in requests:
        if request.doi is None:
            continue
        pmid = cross_ids[request.doi].pmid
        record = efetched.get(pmid) if pmid is not None else None
        if record is not None:
            resolved[request.claim_key] = ResolvedPaper(
                metadata=record.metadata, external_ids=record.external_ids, publisher=None
            )
            continue
        work = works.get(request.doi)
        if work is not None:
            pmcid = cross_ids[request.doi].pmcid or work.pmcid
            resolved[request.claim_key] = ResolvedPaper(
                metadata=openalex.to_pubmed_article(work),
                external_ids=litcache_pb2.ExternalIds(doi=request.doi, pmid=work.pmid, pmcid=pmcid),
                publisher=work.publisher,
            )
    return resolved
