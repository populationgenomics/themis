"""The deployed literature backend: the litcache store and the live indexes behind one port.

``LiveBackend`` is what the ``live`` selector builds. It holds no logic of its own — the store reads
sit in ``litcache``, the index orchestration in ``discovery`` — and exists to present the two as the
single port the servicer depends on (docs/design/literature-evidence-layer.md).

It takes both halves already built. Whoever assembles them owns what they hold open — a GCS client,
a Cloud SQL connector, the image's HTTP client — and that is ``config``, which has the environment
and the exit stack; a backend that built its own would decide the lifetime of things it does not own.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import override

from themis.rpc import literature_pb2
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import discovery as discovery_mod
from themis.services.evidence.literature import litcache, variants
from themis.services.evidence.upstreams import europe_pmc, pubmed


class LiveBackend(literature_backend.LiteratureBackend):
    """The whole port over the real world: GCS + the crosswalk for the store, HTTP for the indexes."""

    def __init__(self, store: litcache.Store, indexes: discovery_mod.Indexes) -> None:
        self._store = store
        self._indexes = indexes

    @override
    async def describe_paper(self, doc_id: str) -> literature_pb2.PaperInfo:
        return await self._store.describe_paper(doc_id)

    @override
    async def get_markdown(self, doc_id: str, max_chars: int) -> literature_pb2.GetMarkdownResponse:
        return await self._store.get_markdown(doc_id, max_chars)

    @override
    async def resolve_content(
        self, doc_id: str, selector: literature_backend.ContentSelector
    ) -> literature_pb2.ContentLocation:
        return await self._store.resolve_content(doc_id, selector)

    @override
    async def locate(
        self, doc_id: str, quote: str, representation: literature_pb2.Representation
    ) -> literature_pb2.LocateResponse:
        return await self._store.locate(doc_id, quote, representation)

    @override
    async def validate(self, doc_id: str, quote: str) -> literature_pb2.ValidateResponse:
        return await self._store.validate(doc_id, quote)

    @override
    async def resolve_external_ids(self, external_ids: Sequence[str]) -> dict[str, str]:
        return await self._store.resolve_external_ids(external_ids)

    @override
    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        return await self._store.full_text_readiness(doc_ids)

    @override
    async def request_conversions(self, doc_ids: Sequence[str]) -> None:
        await self._store.request_conversions(doc_ids)

    @override
    async def search_europe_pmc(self, query: str, max_results: int) -> europe_pmc.SearchHits:
        return await self._indexes.search_europe_pmc(query, max_results)

    @override
    async def fetch_pubmed_articles(self, pmids: Sequence[str]) -> pubmed.FetchedArticles:
        return await self._indexes.fetch_pubmed_articles(pmids)

    @override
    async def search_litvar(
        self, requested: variants.RequestedVariant, *, max_results: int, max_entities: int
    ) -> variants.VariantCensus:
        return await self._indexes.search_litvar(requested, max_results=max_results, max_entities=max_entities)

    @override
    async def list_litvar_entities(self, *, gene: str, contains: str, max_results: int) -> variants.GeneEntities:
        return await self._indexes.list_litvar_entities(gene=gene, contains=contains, max_results=max_results)
