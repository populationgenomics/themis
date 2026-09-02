"""The live index orchestration over stubbed upstreams.

``Indexes`` is driven through an httpx2 `MockTransport` serving LitVar2 from canned payloads, so the
composition under test is the real one: autocomplete reaches the candidates, each surviving entity's
labels are fetched, and its ranked PMIDs are walked up to the per-entity budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence

import httpx2

from themis.services.evidence.literature import discovery, variants


def _requested(**fields: str) -> variants.RequestedVariant:
    return variants.RequestedVariant(
        **{'gene': '', 'hgvs_c': '', 'protein_change': '', 'rsid': '', 'caid': '', 'entity_id': '', **fields}
    )


def _litvar_page(pmids: Sequence[str], *, count: int) -> dict[str, object]:
    return {'count': count, 'total_pages': 1, 'results': [{'pmid': pmid} for pmid in pmids]}


# One variant as LitVar2 really splits it: an rsID query reaching a position-scoped entity that spans
# two alleles, a gene+change query reaching a change-keyed entity that shares none of its records,
# and the gene-level entity autocomplete throws in alongside them.
_UPSTREAM: dict[str, object] = {
    'autocomplete:rs00': [{'_id': 'litvar@rs00##'}],
    'autocomplete:GENE1 A355T': [{'_id': 'litvar@#77#p.A355T'}, {'_id': 'litvar@#77#'}, {'_id': 'litvar@rs00##'}],
    'entity:litvar@rs00##': {
        '_id': 'litvar@rs00##',
        'rsid': 'rs00',
        'clingen_ids': ['CA1000', 'CA2000'],
        'gene': ['GENE1'],
        'hgvs': 'c.1063G>A',
    },
    'entity:litvar@#77#p.A355T': {'_id': 'litvar@#77#p.A355T', 'gene': ['GENE1'], 'hgvs': 'p.A355T'},
    'entity:litvar@#77#': {'_id': 'litvar@#77#', 'gene': ['GENE1'], 'name': 'All GENE1 variants'},
    'search:litvar@rs00##': _litvar_page(['111', '222', '333'], count=40),
    'search:litvar@#77#p.A355T': _litvar_page(['333', '444'], count=2),
}


def _upstream_handler(
    responses: Mapping[str, object], *, asked: list[str]
) -> Callable[[httpx2.Request], httpx2.Response]:
    """Serve LitVar2 from canned payloads; 400 for an unstubbed entity.

    400 is what LitVar2 answers an unknown entity with, so an unstubbed key stands for "the index
    holds no such thing" rather than for a transport fault.
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/variant/autocomplete/'):
            key = f'autocomplete:{request.url.params["query"]}'
        elif request.url.path.endswith('/litvar2-api/search/'):
            key = f'search:{request.url.params["variant"]}'
        else:
            key = f'entity:{httpx2.URL(str(request.url)).path.rsplit("/", 1)[-1]}'
        asked.append(key)
        if key not in responses:
            return httpx2.Response(400, text='no such variant')
        return httpx2.Response(200, json=responses[key])

    return handler


def _live[T](responses: Mapping[str, object], call: Callable[[discovery.Indexes], Awaitable[T]]) -> tuple[T, list[str]]:
    asked: list[str] = []

    async def run() -> T:
        transport = httpx2.MockTransport(_upstream_handler(responses, asked=asked))
        async with httpx2.AsyncClient(transport=transport) as client:
            return await call(discovery.Indexes(client))

    return asyncio.run(run()), asked


def test_the_live_adapter_keeps_every_entity_its_queries_reach() -> None:
    # Two queries, two entities sharing no record. Stopping at the first non-empty one — or merging
    # them — loses either the second entity's PMIDs or the fact that they are a different entity's.
    found, _ = _live(
        _UPSTREAM,
        lambda d: d.search_litvar(
            _requested(gene='GENE1', rsid='rs00', protein_change='p.A355T'), max_results=10, max_entities=8
        ),
    )
    assert [entity.labels.id for entity in found.entities] == ['litvar@rs00##', 'litvar@#77#p.A355T']
    # 333 is under both, and is reported under both rather than assigned to one of them.
    assert [list(entity.pmids) for entity in found.entities] == [['111', '222', '333'], ['333', '444']]
    assert found.entities[0].total_records == 40  # the index's own count: the list reads as a prefix
    assert found.entities[0].labels.caids == ('CA1000', 'CA2000')


def test_the_live_adapter_drops_the_gene_level_entity_autocomplete_throws_in() -> None:
    # Autocomplete matches the gene-level entity loosely; it holds the gene's whole literature and
    # answers no variant question, so paging it would spend the budget on the wrong thing entirely.
    found, _ = _live(
        _UPSTREAM,
        lambda d: d.search_litvar(_requested(gene='GENE1', protein_change='A355T'), max_results=10, max_entities=8),
    )
    # The loosely-matched sibling stays — it names an allele, and its disagreement is stated, not
    # acted on. The gene-level entity names none, so it is not a candidate at all.
    assert [entity.labels.id for entity in found.entities] == ['litvar@#77#p.A355T', 'litvar@rs00##']
    assert found.total_entities == 2  # the gene-level entity leaves the census rather than inflating it


def test_the_pmid_budget_is_per_entity() -> None:
    # Each entity's walk stops at the budget on its own; one entity linking many records cannot
    # starve another, and neither list is anything but the entity's own top-ranked prefix.
    found, _ = _live(
        _UPSTREAM,
        lambda d: d.search_litvar(
            _requested(gene='GENE1', rsid='rs00', protein_change='p.A355T'), max_results=2, max_entities=8
        ),
    )
    assert [list(entity.pmids) for entity in found.entities] == [['111', '222'], ['333', '444']]


def test_the_live_adapter_spends_the_entity_ceiling_across_the_queries() -> None:
    # Autocomplete matches on a prefix, so one identifier can return matches enough to fill the
    # ceiling on its own. Taking them in query order would make supplying a second identifier delete
    # the first one's entity from the answer — more information, a worse result.
    prolific = [{'_id': f'litvar@CA1000{n}##'} for n in range(8)]
    upstream = {
        **_UPSTREAM,
        'autocomplete:CA1000': prolific,
        **{
            f'entity:{match["_id"]}': {'_id': match['_id'], 'clingen_id': match['_id'][7:-2], 'gene': ['GENE1']}
            for match in prolific
        },
        **{f'search:{match["_id"]}': _litvar_page([], count=0) for match in prolific},
    }
    found, _ = _live(
        upstream, lambda d: d.search_litvar(_requested(caid='CA1000', rsid='rs00'), max_results=10, max_entities=4)
    )
    assert 'litvar@rs00##' in [entity.labels.id for entity in found.entities]


def test_the_live_adapter_stops_resolving_at_the_entity_ceiling() -> None:
    # Autocomplete matches loosely and is not bounded upstream; each surviving entity costs its own
    # labels fetch and page walk, which the per-entity budget does not reach.
    found, asked = _live(
        _UPSTREAM,
        lambda d: d.search_litvar(_requested(gene='GENE1', protein_change='A355T'), max_results=10, max_entities=1),
    )
    assert len(found.entities) == 1
    assert found.total_entities == 3  # the cut is stated, so the caller knows candidates went unnamed
    assert not any('litvar@rs00##' in key for key in asked)  # nothing was fetched for the dropped one


def test_an_entity_id_the_index_does_not_hold_is_an_empty_answer() -> None:
    found, _ = _live(
        {}, lambda d: d.search_litvar(_requested(entity_id='litvar@nope##'), max_results=10, max_entities=8)
    )
    assert found.entities == ()
    assert found.total_entities == 0


def test_the_live_adapter_asks_upstream_under_the_normalised_identifiers() -> None:
    _, asked = _live(
        {**_UPSTREAM, 'autocomplete:CA1000': []},
        lambda d: d.search_litvar(_requested(caid='CA001000', rsid='00'), max_results=10, max_entities=8),
    )
    assert asked[:2] == ['autocomplete:CA1000', 'autocomplete:rs00']


def test_the_live_gene_listing_narrows_and_ranks() -> None:
    listing = (
        "{'_id': 'litvar@rs00##', 'pmids_count': 5, 'rsid': 'rs00'}\n"
        "{'_id': 'litvar@CA1000#rs00##', 'pmids_count': 2, 'rsid': 'rs00', 'clingen_id': 'CA1000'}\n"
        "{'_id': 'litvar@#77#p.A340T', 'pmids_count': 3}\n"
    )

    async def run() -> tuple[variants.GeneEntities, variants.GeneEntities]:
        transport = httpx2.MockTransport(lambda _r: httpx2.Response(200, text=listing))
        async with httpx2.AsyncClient(transport=transport) as client:
            indexes = discovery.Indexes(client)
            return (
                await indexes.list_litvar_entities(gene='GENE1', contains='', max_results=2),
                await indexes.list_litvar_entities(gene='GENE1', contains='a340', max_results=50),
            )

    listed, narrowed = asyncio.run(run())
    assert [entity.id for entity in listed.entities] == ['litvar@rs00##', 'litvar@#77#p.A340T']  # 5 then 3
    assert (listed.total_in_gene, listed.total_matched) == (3, 3)  # returned 2 of 3: a prefix, legible as one
    assert [entity.id for entity in narrowed.entities] == ['litvar@#77#p.A340T']
    assert (narrowed.total_in_gene, narrowed.total_matched) == (3, 1)
