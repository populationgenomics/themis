"""Discovery over the live indexes: keyword search, the bibliographic batch, the LitVar2 census.

Discovery reaches the live indexes rather than the full-text store
(docs/design/literature-evidence-layer.md §4): a hit here says a paper exists, never that its text is
readable — that is ``MaybeIngestPapers``' answer.

``Indexes`` composes the three upstream modules: Europe PMC for keyword search, PubMed for the whole
records — journal or book — behind a batch of PMIDs (``upstreams.pubmed``, riding litcache's
efetch path), and LitVar2 for the entity resolution itself (autocomplete for candidate ids,
``variant/get`` for each one's labels, the paged search for its ranked PMIDs).

Its methods are ``async``: the servicer runs on ``grpc.aio``, and the calls go out on the image's
shared ``httpx2.AsyncClient``. Nothing here retries — a 429 or a 5xx escapes as
``httpx2.HTTPStatusError`` and the guest's own retry helper owns the backoff.

The LitVar fan-out is sequential, awaiting each upstream call before issuing the next. That is the
pacing: LitVar2 is keyless and tolerates a few requests a second, so a gathered fan-out over a
request's entities would burst past it, and the serial shape holds to it without a limiter of its
own. The rpc deadline is what bounds the resulting latency.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx2

from themis.services.evidence.literature import variants
from themis.services.evidence.upstreams import europe_pmc, litvar, pubmed


class Indexes:
    """The live indexes behind discovery: Europe PMC + PubMed + LitVar2 (verified live at deploy, not offline)."""

    def __init__(self, http_client: httpx2.AsyncClient) -> None:
        self._http_client = http_client

    async def search_europe_pmc(self, query: str, max_results: int) -> europe_pmc.SearchHits:
        return await europe_pmc.search(query, max_results, http_client=self._http_client)

    async def fetch_pubmed_articles(self, pmids: Sequence[str]) -> pubmed.FetchedArticles:
        return await pubmed.articles_by_pmid(pmids, http_client=self._http_client)

    async def search_litvar(
        self, requested: variants.RequestedVariant, *, max_results: int, max_entities: int
    ) -> variants.VariantCensus:
        entity_ids, reached = await self._entity_ids(requested, max_entities)
        labelled = [await litvar.entity_labels(entity_id, http_client=self._http_client) for entity_id in entity_ids]
        resolved = [labels for labels in labelled if labels is not None and labels.names_an_allele()]
        # An id the index answered for with nothing, or with an entity naming no allele, is not a
        # candidate the ceiling withheld, so it leaves the census rather than inflating it.
        total_entities = reached - (len(labelled) - len(resolved))
        ranked = [
            await litvar.search_pmids(labels.id, max_results, http_client=self._http_client) for labels in resolved
        ]
        return variants.VariantCensus(
            entities=tuple(
                variants.VariantEntity(
                    labels=labels,
                    agreement=variants.identifier_agreement(requested, labels),
                    total_records=total,
                    pmids=tuple(found),
                )
                for labels, (found, total) in zip(resolved, ranked, strict=True)
            ),
            total_entities=total_entities,
        )

    async def list_litvar_entities(self, *, gene: str, contains: str, max_results: int) -> variants.GeneEntities:
        listed = await litvar.gene_entities(gene, http_client=self._http_client)
        return variants.gene_inventory(listed, contains=contains, max_results=max_results)

    async def _entity_ids(self, requested: variants.RequestedVariant, limit: int) -> tuple[list[str], int]:
        """At most ``limit`` entity ids to fetch, and how many distinct ids the queries reached.

        An explicit ``entity_id`` is resolution: it names the entity and no query is issued for it.
        Otherwise every query runs and every match it returns is a candidate — autocomplete matches
        loosely, and a match the request contradicts is one to report, not one to drop here. The
        limit is spent breadth-first across the queries: autocomplete matches on a prefix, so one
        identifier can return matches enough to fill it on its own, and taking them in query order
        would drop every entity the other identifiers reached.
        """
        if requested.entity_id:
            return [requested.entity_id], 1
        per_query = [
            await litvar.autocomplete_entity_ids(query, http_client=self._http_client)
            for query in variants.litvar_queries(requested)
        ]
        reached = len({entity_id for ids in per_query for entity_id in ids})
        return variants.round_robin(per_query, limit), reached
