"""The bibliographic metadata resolver ladder: PMID → efetch, else DOI → Crossref.

litcache resolves a paper's identifier to its `metadata.pb` (serialized pubmed_proto
`PubmedArticle`) via a two-rung ladder:

- a PMID resolves through PubMed efetch (`themis.litcache.efetch`) — the cheap path
  that also harvests the cross-ids (DOI↔PMID↔PMCID) from the record's own id list;
- a paper with no PMID (or whose PMID efetch does not return) falls to Crossref by
  DOI (`themis.litcache.crossref`), a bespoke/lossy mapping that also yields the
  publisher for the manifest's `Licensed` access.

A paper that resolves through neither rung is fully unknown and raises
`MetadataUnresolvedError`: `PubmedArticle`'s required title / `pub_date` cannot be
satisfied without inventing data, so the paper fails loud rather than get synthetic
metadata; the orchestrator surfaces it in diagnostics.

This is the per-paper ladder; the underlying efetch resolver
(`efetch.parse_response`) is itself batch-capable.
"""

from __future__ import annotations

import dataclasses

import httpx

from themis.litcache import crossref, efetch
from themis.litcache.models import litcache_pb2

# Crossref answers an unknown DOI with 404 — that is the paper's "unknown", a
# resolver miss, not a transport failure; other statuses propagate (transient).
_NOT_FOUND = 404


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


async def _crossref_or_none(doi: str, *, http_client: httpx.AsyncClient) -> crossref.CrossrefResult | None:
    """Resolve a DOI via Crossref, mapping a 404 (unknown DOI) to `None`."""
    try:
        return await crossref.resolve(doi, http_client=http_client)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == _NOT_FOUND:
            return None
        raise


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
