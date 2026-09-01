"""NCBI LitVar2 adapter: autocomplete, one entity's labels, its paged records, a gene's inventory.

Driven by an httpx2 `MockTransport`; no test hits the network. The payload shapes are the live index's
own — including the per-gene listing, which answers in Python `repr` syntax rather than JSON.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence

import httpx2
import pytest

from themis.services.evidence import errors
from themis.services.evidence.upstreams import litvar


def _run[T](
    handler: Callable[[httpx2.Request], httpx2.Response], call: Callable[[httpx2.AsyncClient], Awaitable[T]]
) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await call(client)

    return asyncio.run(run())


def _page(pmids: Sequence[str], *, count: int, total_pages: int = 1) -> dict[str, object]:
    return {'count': count, 'total_pages': total_pages, 'results': [{'pmid': pmid} for pmid in pmids]}


# LitVar2's per-gene listing, verbatim in shape: one record per line in Python `repr` syntax, with
# `rsid` and `clingen_id` present only on the entities keyed on them.
_GENE_LISTING = (
    "{'_id': 'litvar@rs00##', 'pmids_count': 5, 'rsid': 'rs00'}\n"
    "{'_id': 'litvar@CA1000#rs00##', 'pmids_count': 2, 'rsid': 'rs00', 'clingen_id': 'CA1000'}\n"
    "{'_id': 'litvar@#77#p.A340T', 'pmids_count': 3}\n"
)


def test_autocomplete_returns_the_matched_entity_ids_in_the_indexs_order() -> None:
    asked: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.update(request.url.params)
        return httpx2.Response(200, json=[{'_id': 'litvar@rs00##'}, {'_id': 'litvar@#77#p.A355T'}, {'no_id': 1}])

    matched = _run(handler, lambda c: litvar.autocomplete_entity_ids('rs00', http_client=c))
    assert matched == ['litvar@rs00##', 'litvar@#77#p.A355T']  # a match stating no id contributes none
    assert asked['query'] == 'rs00'


def test_autocomplete_fails_loud_on_an_answer_that_is_not_a_match_list() -> None:
    with pytest.raises(ValueError, match='not a list'):
        _run(
            lambda _r: httpx2.Response(200, json={'results': []}),
            lambda c: litvar.autocomplete_entity_ids('rs00', http_client=c),
        )


def test_entity_labels_reads_every_allele_id_an_entity_spans() -> None:
    # Autocomplete states a single `clingen_id`; only the entity record states them all, and more
    # than one is what says the entity is position-scoped rather than allele-scoped.
    labels = _run(
        lambda _r: httpx2.Response(
            200,
            json={
                '_id': 'litvar@rs00##',
                'rsid': 'rs00',
                'clingen_ids': ['CA1000', 'CA2000'],
                'gene': ['GENE1', 'GENE1-AS1'],
                'hgvs': 'c.1063G>A',
            },
        ),
        lambda c: litvar.entity_labels('litvar@rs00##', http_client=c),
    )
    assert labels == litvar.EntityLabels(
        id='litvar@rs00##',
        rsid='rs00',
        caids=('CA1000', 'CA2000'),
        genes=('GENE1', 'GENE1-AS1'),
        change='c.1063G>A',
    )


def test_entity_labels_falls_back_to_the_single_clingen_id() -> None:
    labels = _run(
        lambda _r: httpx2.Response(200, json={'_id': 'litvar@CA1000##', 'clingen_id': 'CA1000', 'gene': ['GENE1']}),
        lambda c: litvar.entity_labels('litvar@CA1000##', http_client=c),
    )
    assert labels is not None
    assert labels.caids == ('CA1000',)


def test_the_entity_id_is_escaped_into_the_path() -> None:
    # An entity id carries `@` and `#`; unescaped, the `#` truncates the URL at the fragment and the
    # request asks about a different entity entirely.
    asked: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.append(str(request.url))
        return httpx2.Response(200, json={'_id': 'litvar@#77#p.A355T'})

    _run(handler, lambda c: litvar.entity_labels('litvar@#77#p.A355T', http_client=c))
    assert asked[0].endswith('litvar%40%2377%23p.A355T')


def test_entity_labels_reports_an_unknown_entity_rather_than_failing() -> None:
    # LitVar2 answers an unknown id with 400, not 404; taking that for a refusal would turn "the
    # index holds no such entity" into an INVALID_ARGUMENT the caller cannot act on.
    assert (
        _run(
            lambda _r: httpx2.Response(400, text='no such variant'),
            lambda c: litvar.entity_labels('litvar@nope##', http_client=c),
        )
        is None
    )


@pytest.mark.parametrize('status', [403, 404, 422])
def test_any_other_refusal_reaches_the_caller(status: int) -> None:
    with pytest.raises(errors.InvalidRequestError):
        _run(
            lambda _r: httpx2.Response(status, text='no'),
            lambda c: litvar.entity_labels('litvar@rs00##', http_client=c),
        )


def test_entity_labels_fails_loud_on_an_entity_stating_no_id() -> None:
    with pytest.raises(ValueError, match='no id of its own'):
        _run(
            lambda _r: httpx2.Response(200, json={'rsid': 'rs00', 'gene': ['GENE1']}),
            lambda c: litvar.entity_labels('litvar@rs00##', http_client=c),
        )


def test_search_pmids_walks_pages_up_to_the_limit() -> None:
    pages = iter(
        [
            _page([str(n) for n in range(1, 11)], count=25, total_pages=3),
            _page([str(n) for n in range(11, 21)], count=25, total_pages=3),
        ]
    )
    found, total = _run(
        lambda _r: httpx2.Response(200, json=next(pages)),
        lambda c: litvar.search_pmids('litvar@rs00##', 15, http_client=c),
    )
    assert len(found) == 15
    assert total == 25  # the walk stopped short; the count says how much it stopped short of


def test_search_pmids_reports_a_whole_short_list_as_whole() -> None:
    found, total = _run(
        lambda _r: httpx2.Response(200, json=_page(['1', '2'], count=2)),
        lambda c: litvar.search_pmids('litvar@rs00##', 50, http_client=c),
    )
    assert (found, total) == (['1', '2'], 2)


def test_search_pmids_keys_the_pmids_the_way_the_record_lookup_will() -> None:
    found, _ = _run(
        lambda _r: httpx2.Response(200, json=_page(['0000111'], count=1)),
        lambda c: litvar.search_pmids('litvar@rs00##', 10, http_client=c),
    )
    assert found == ['111']


@pytest.mark.parametrize(
    'payload',
    [
        pytest.param({'total_pages': 3, 'results': []}, id='no-count'),
        pytest.param({'count': 25, 'results': []}, id='no-page-bound'),
        pytest.param({'count': 25, 'total_pages': 3}, id='no-results'),
        pytest.param([], id='not-a-mapping'),
    ],
)
def test_search_pmids_fails_loud_on_a_page_it_cannot_read(payload: object) -> None:
    # Defaulting here would report a walk that stopped early as the whole of the entity, which is the
    # exact misstatement the census exists to rule out.
    with pytest.raises(ValueError, match='LitVar2 search page'):
        _run(
            lambda _r: httpx2.Response(200, json=payload),
            lambda c: litvar.search_pmids('litvar@rs00##', 50, http_client=c),
        )


def test_search_pmids_fails_loud_on_a_pmid_that_is_not_one() -> None:
    # These PMIDs go on to key the record lookup and its query alike, so one the index states in
    # another form would drop its article from the entity while the entity's count still counts it.
    with pytest.raises(ValueError, match='PMC4072343'):
        _run(
            lambda _r: httpx2.Response(200, json=_page(['111', 'PMC4072343'], count=2)),
            lambda c: litvar.search_pmids('litvar@rs00##', 10, http_client=c),
        )


def test_gene_listing_is_read_as_python_literals_not_json() -> None:
    # The endpoint answers in repr syntax, so a JSON parser fails on the first line; nothing about
    # the payload announces that, which is why it is pinned here.
    with pytest.raises(json.JSONDecodeError):
        json.loads(_GENE_LISTING.splitlines()[0])

    entities = _run(
        lambda _r: httpx2.Response(200, text=_GENE_LISTING),
        lambda c: litvar.gene_entities('GENE1', http_client=c),
    )
    assert [entity.id for entity in entities] == ['litvar@rs00##', 'litvar@CA1000#rs00##', 'litvar@#77#p.A340T']
    assert [entity.caid for entity in entities] == ['', 'CA1000', '']
    assert [entity.total_articles for entity in entities] == [5, 2, 3]


@pytest.mark.parametrize(
    'payload',
    [
        pytest.param("{'_id': 'litvar@rs00##',\n", id='unparseable'),
        pytest.param("['litvar@rs00##', 5]\n", id='not-a-mapping'),
        pytest.param("{'_id': 'litvar@rs00##'}\n", id='no-count'),
        pytest.param("{'pmids_count': 5}\n", id='no-id'),
    ],
)
def test_gene_listing_fails_loud_on_a_line_it_cannot_read(payload: str) -> None:
    # Skipping the line would answer with a listing silently short of the gene's entities, which is
    # the one thing this listing exists to rule out.
    with pytest.raises(ValueError, match='LitVar2 gene listing'):
        _run(lambda _r: httpx2.Response(200, text=payload), lambda c: litvar.gene_entities('GENE1', http_client=c))


def test_gene_listing_reads_an_empty_payload_as_no_entities() -> None:
    # The endpoint answers a symbol it holds nothing for with an empty body. A parser that raises on
    # every line it cannot read must still take that as a fact about the index rather than a fault.
    assert (
        _run(lambda _r: httpx2.Response(200, text=''), lambda c: litvar.gene_entities('NOTAGENE', http_client=c)) == []
    )


@pytest.mark.parametrize('status', [429, 500, 503])
def test_a_transient_failure_stays_retryable(status: int) -> None:
    with pytest.raises(httpx2.HTTPStatusError):
        _run(
            lambda _r: httpx2.Response(status, text='busy'),
            lambda c: litvar.entity_labels('litvar@rs00##', http_client=c),
        )
