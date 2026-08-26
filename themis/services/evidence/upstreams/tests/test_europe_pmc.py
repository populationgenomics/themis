"""Europe PMC adapter: ranked search and the batched record lookup, over recorded payloads.

Driven by an httpx `MockTransport`; no test hits the network. The batch payload is Europe PMC's own
answer to one record query for three PMIDs, recorded verbatim: a research article carrying an
abstract, a comment carrying none, and an identifier nothing is indexed under — which the answer
omits rather than reports. Nothing offline can confirm that omission is how absence arrives, so the
payload is recorded rather than assumed.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence

import httpx
import pytest

from themis.services.evidence import errors
from themis.services.evidence.upstreams import europe_pmc

_BATCH = json.loads((pathlib.Path(__file__).resolve().parent / 'fixtures' / 'europepmc_batch_records.json').read_text())

# The `EXT_ID` terms a record query names, in query order.
_EXT_ID = re.compile(r'EXT_ID:(\d+)')


def _run[T](handler: Callable[[httpx.Request], httpx.Response], call: Callable[[httpx.AsyncClient], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await call(client)

    return asyncio.run(run())


def _page(results: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """A search page carrying `results` whole — the shape a record lookup insists on."""
    return {'hitCount': len(results), 'resultList': {'result': list(results)}}


def _queried_pmids(request: httpx.Request) -> list[str]:
    """The PMIDs a record query asks about, read back out of its `EXT_ID` disjunction."""
    return _EXT_ID.findall(request.url.params['query'])


def _answering_every_pmid(seen: list[list[str]]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer each record query with one record per PMID it asked for; collect the batches."""

    def handler(request: httpx.Request) -> httpx.Response:
        pmids = _queried_pmids(request)
        seen.append(pmids)
        return httpx.Response(200, json=_page([{'pmid': pmid, 'title': f'Paper {pmid}'} for pmid in pmids]))

    return handler


def test_search_returns_the_indexs_records_in_its_own_order() -> None:
    records = _run(
        lambda _r: httpx.Response(200, json=_page([{'pmid': '111', 'title': 'A'}, {'pmid': '222', 'title': 'B'}])),
        lambda c: europe_pmc.search('GENE1 variant', 10, http_client=c),
    )
    assert [record.pmid for record in records] == ['111', '222']


def test_search_asks_for_core_records_up_to_the_budget() -> None:
    asked: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        asked.update(request.url.params)
        return httpx.Response(200, json=_page([]))

    _run(handler, lambda c: europe_pmc.search('GENE1', 7, http_client=c))
    assert asked['query'] == 'GENE1'
    assert asked['resultType'] == 'core'  # the abstract and bibliography ride on this result type
    assert asked['pageSize'] == '7'


def test_records_read_a_recorded_batch_into_its_three_outcomes() -> None:
    requested = ['24789688', '24789689', '99999999999']
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(200, json=_BATCH)

    records = _run(handler, lambda c: europe_pmc.records_by_pmid(requested, http_client=c))
    assert len(asked) == 1  # one query for the whole batch: what taking a list is for
    assert _queried_pmids(asked[0]) == requested
    assert set(records) == {'24789688', '24789689'}  # the third is absent by omission
    article = records['24789688']
    assert article.abstract
    assert article.year == '2014'
    assert article.doi
    assert article.journal  # only the nested journalInfo states it on these records
    assert not records['24789689'].abstract  # a comment the index carries no abstract under


def test_a_batch_beyond_one_querys_bound_is_split_into_a_partition() -> None:
    # The port takes any list; a query naming all of it would grow the URL without bound. Chunking is
    # only sound if the chunks partition the request, so assert that rather than a chunk count.
    batches: list[list[str]] = []
    requested = [str(pmid) for pmid in range(1000, 1000 + 2 * europe_pmc._MAX_IDS_PER_QUERY + 3)]

    records = _run(_answering_every_pmid(batches), lambda c: europe_pmc.records_by_pmid(requested, http_client=c))

    assert all(len(batch) <= europe_pmc._MAX_IDS_PER_QUERY for batch in batches)
    assert [pmid for batch in batches for pmid in batch] == requested
    assert set(records) == set(requested)


@pytest.mark.parametrize(
    'payload',
    [
        pytest.param({'hitCount': 2, 'resultList': {'result': [{'pmid': '111'}]}}, id='short-page'),
        pytest.param({'resultList': {'result': [{'pmid': '111'}]}}, id='no-hit-count'),
        pytest.param({'hitCount': 2, 'resultList': {'result': [{'pmid': '111'}, {'pmid': '777'}]}}, id='wrong-record'),
        pytest.param(
            {'hitCount': 2, 'resultList': {'result': [{'pmid': '111'}, {'title': 'T'}]}}, id='record-with-no-id'
        ),
    ],
)
def test_a_record_query_answering_other_than_what_it_asked_fails_loud(payload: object) -> None:
    # A lookup by identifier reads absence off what the answer omits, so an answer that dropped a
    # record — or that carries one under a PMID nobody asked about, leaving one that was asked about
    # unaccounted for — would report it as a record nothing is indexed under.
    with pytest.raises(ValueError, match='Europe PMC'):
        _run(
            lambda _r: httpx.Response(200, json=payload),
            lambda c: europe_pmc.records_by_pmid(['111', '222'], http_client=c),
        )


def test_a_record_lookup_for_nothing_asks_nothing() -> None:
    # An empty chunk would build the term `() AND SRC:MED`, which is not a query about no records.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f'no query should have been issued, got {request.url}')

    assert _run(handler, lambda c: europe_pmc.records_by_pmid([], http_client=c)) == {}


def test_a_record_query_is_scoped_to_medline() -> None:
    # Unscoped, an EXT_ID matches the same number in the preprint and agricola corpora, so a record
    # under another source would answer for a PMID it is not.
    asked: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        asked.update(request.url.params)
        return httpx.Response(200, json=_page([{'pmid': '111'}]))

    _run(handler, lambda c: europe_pmc.records_by_pmid(['111'], http_client=c))
    assert asked['query'] == '(EXT_ID:111) AND SRC:MED'


def test_a_book_chapter_is_cited_by_its_own_work_and_date() -> None:
    # A chapter's pubYear dates the series, not the chapter, and no journal field names the book. Read
    # flat, a chapter revised this year cites to the year the series opened, with no source at all.
    record = _run(
        lambda _r: httpx.Response(
            200,
            json=_page(
                [
                    {
                        'pmid': '20301288',
                        'title': 'Neurofibromatosis 1',
                        'pubYear': '1993',
                        'firstPublicationDate': '2025-04-03',
                        'bookOrReportDetails': {'comprisingTitle': 'GeneReviews', 'yearOfPublication': 1993},
                    }
                ]
            ),
        ),
        lambda c: europe_pmc.records_by_pmid(['20301288'], http_client=c),
    )['20301288']

    assert record.year == '2025'
    assert record.journal == 'GeneReviews'


def test_an_article_is_cited_by_its_publication_year() -> None:
    record = _run(
        lambda _r: httpx.Response(
            200,
            json=_page(
                [{'pmid': '1', 'pubYear': '2014', 'firstPublicationDate': '2014-04-24', 'journalTitle': 'Hum Mutat'}]
            ),
        ),
        lambda c: europe_pmc.records_by_pmid(['1'], http_client=c),
    )['1']

    assert record.year == '2014'
    assert record.journal == 'Hum Mutat'


@pytest.mark.parametrize(
    ('record', 'unavailable'),
    [
        pytest.param({'abstract': 'Something.'}, None, id='abstract'),
        pytest.param({'abstract': '   \n'}, europe_pmc.Unavailable.NO_ABSTRACT, id='blank-abstract'),
        pytest.param({'abstract': ''}, europe_pmc.Unavailable.NO_ABSTRACT, id='no-abstract'),
        pytest.param(None, europe_pmc.Unavailable.NO_RECORD, id='no-record'),
    ],
)
def test_a_lookups_outcome_separates_a_missing_abstract_from_a_missing_record(
    record: dict[str, str] | None, unavailable: europe_pmc.Unavailable | None
) -> None:
    # Both reach a caller reading the abstract field alone as one empty string, and they are
    # different facts: one names a paper to cite, the other says the identifier reaches nothing.
    built = (
        None
        if record is None
        else europe_pmc.ArticleRecord(pmid='111', title='T', authors='', journal='', year='', doi='', **record)
    )
    outcome = europe_pmc.FetchedArticle.of('111', built)
    assert outcome.unavailable is unavailable
    assert (outcome.record is None) == (record is None)


def test_a_refusal_reaches_the_caller_as_an_invalid_request() -> None:
    with pytest.raises(errors.InvalidRequestError):
        _run(lambda _r: httpx.Response(400, text='no'), lambda c: europe_pmc.search('GENE1', 10, http_client=c))


def test_a_transient_failure_stays_retryable() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        _run(
            lambda _r: httpx.Response(503, text='busy'),
            lambda c: europe_pmc.records_by_pmid(['111'], http_client=c),
        )
