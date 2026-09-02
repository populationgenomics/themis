"""Europe PMC adapter: ranked keyword search, over recorded payloads.

Driven by an httpx2 `MockTransport`; no test hits the network. The recorded payload is Europe PMC's
own answer to one search, verbatim: a research article carrying an abstract and a comment carrying
none, so the parse is exercised against what the index actually states rather than what a hand-built
page assumes.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import Awaitable, Callable, Mapping, Sequence

import httpx2
import pytest

from themis.services.evidence import errors
from themis.services.evidence.upstreams import europe_pmc

_BATCH = json.loads((pathlib.Path(__file__).resolve().parent / 'fixtures' / 'europepmc_batch_records.json').read_text())


def _run[T](
    handler: Callable[[httpx2.Request], httpx2.Response], call: Callable[[httpx2.AsyncClient], Awaitable[T]]
) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await call(client)

    return asyncio.run(run())


def _page(results: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """A search page carrying `results` whole, with the count naming exactly them."""
    return {'hitCount': len(results), 'resultList': {'result': list(results)}}


def _search_one(result: Mapping[str, object]) -> europe_pmc.Record:
    """The one record a search answered with `result` parses to."""
    hits = _run(
        lambda _r: httpx2.Response(200, json=_page([result])),
        lambda c: europe_pmc.search('GENE1', 10, http_client=c),
    )
    (record,) = hits.records
    return record


def test_search_returns_the_indexs_records_in_its_own_order() -> None:
    hits = _run(
        lambda _r: httpx2.Response(200, json=_page([{'pmid': '111', 'title': 'A'}, {'pmid': '222', 'title': 'B'}])),
        lambda c: europe_pmc.search('GENE1 variant', 10, http_client=c),
    )
    assert [record.pmid for record in hits.records] == ['111', '222']


def test_a_search_record_carries_a_pmid_only_where_the_index_states_one() -> None:
    # A search reaches every source the index holds, so a preprint or a PMC-only deposit comes back
    # under the index's own id, which is no PubMed id. Carried as one, `PPR498243` would reach the
    # store's door as `pmid:PPR498243` — a PubMed record that does not exist. The doi is what names
    # such a record.
    hits = _run(
        lambda _r: httpx2.Response(
            200,
            json=_page(
                [
                    {'id': '24789688', 'source': 'MED', 'pmid': '24789688', 'doi': '10.1002/humu.22550'},
                    {'id': 'PPR498243', 'source': 'PPR', 'doi': '10.1101/2022.05.12.491690', 'title': 'A preprint'},
                ]
            ),
        ),
        lambda c: europe_pmc.search('GENE1 variant', 10, http_client=c),
    )

    medline, preprint = hits.records
    assert (medline.pmid, medline.doi) == ('24789688', '10.1002/humu.22550')
    assert preprint.pmid == ''
    assert (preprint.doi, preprint.title) == ('10.1101/2022.05.12.491690', 'A preprint')


def test_search_reports_the_indexs_hit_count_beside_the_page() -> None:
    # A page is what the budget bought; the count is what the query matched, and only the index
    # knows it — read off the page length it would say every search matched exactly one page.
    hits = _run(
        lambda _r: httpx2.Response(
            200, json={'hitCount': 4130, 'resultList': {'result': [{'pmid': '111', 'title': 'A'}]}}
        ),
        lambda c: europe_pmc.search('GENE1 variant', 1, http_client=c),
    )
    assert (len(hits.records), hits.total_matched) == (1, 4130)


def test_a_search_answered_without_a_hit_count_is_a_fault() -> None:
    with pytest.raises(ValueError, match='hit count'):
        _run(
            lambda _r: httpx2.Response(200, json={'resultList': {'result': [{'pmid': '111', 'title': 'A'}]}}),
            lambda c: europe_pmc.search('GENE1', 10, http_client=c),
        )


def test_search_asks_for_core_records_up_to_the_budget() -> None:
    asked: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.update(request.url.params)
        return httpx2.Response(200, json=_page([]))

    _run(handler, lambda c: europe_pmc.search('GENE1', 7, http_client=c))
    assert asked['query'] == 'GENE1'
    assert asked['resultType'] == 'core'  # the abstract and bibliography ride on this result type
    assert asked['pageSize'] == '7'


def test_search_parses_a_recorded_page_whole() -> None:
    hits = _run(
        lambda _r: httpx2.Response(200, json=_BATCH),
        lambda c: europe_pmc.search('GENE1', 10, http_client=c),
    )

    by_pmid = {record.pmid: record for record in hits.records}
    assert set(by_pmid) == {'24789688', '24789689'}
    record = by_pmid['24789688']
    assert record.abstract
    assert record.year == '2014'
    assert record.doi
    assert record.journal  # only the nested journalInfo states it on these records
    assert record.authors == ('Xu W', 'Yang X', 'Hu X', 'Li S')  # authorList's own order
    assert record.pmcid == 'PMC4072343'  # the door's key for a deposit no PubMed id names
    assert not by_pmid['24789689'].abstract  # a comment the index carries no abstract under
    assert not by_pmid['24789689'].pmcid  # empty where the index states none


def test_a_book_chapter_is_cited_by_its_own_work_and_date() -> None:
    # A chapter's pubYear dates the series, not the chapter, and no journal field names the book. Read
    # flat, a chapter revised this year cites to the year the series opened, with no source at all.
    record = _search_one(
        {
            'pmid': '20301288',
            'title': 'Neurofibromatosis 1',
            'pubYear': '1993',
            'firstPublicationDate': '2025-04-03',
            'bookOrReportDetails': {'comprisingTitle': 'GeneReviews', 'yearOfPublication': 1993},
        }
    )

    assert record.year == '2025'
    assert record.journal == 'GeneReviews'


def test_an_article_is_cited_by_its_publication_year() -> None:
    record = _search_one(
        {'pmid': '1', 'pubYear': '2014', 'firstPublicationDate': '2014-04-24', 'journalTitle': 'Hum Mutat'}
    )

    assert record.year == '2014'
    assert record.journal == 'Hum Mutat'


def test_a_refusal_reaches_the_caller_as_an_invalid_request() -> None:
    with pytest.raises(errors.InvalidRequestError):
        _run(lambda _r: httpx2.Response(400, text='no'), lambda c: europe_pmc.search('GENE1', 10, http_client=c))


def test_a_refusal_delivered_with_status_200_is_still_a_refusal() -> None:
    # The index answers an empty or over-long query with HTTP 200 and an error body — no hit count,
    # no result list. Read as a page it would raise a bare ValueError, which reaches the guest as an
    # UNKNOWN its retry helper reissues, for a request that cannot come back different.
    refusal = {'errCode': 404, 'errMsg': 'No search criteria provided.'}
    with pytest.raises(errors.InvalidRequestError, match='No search criteria'):
        _run(lambda _r: httpx2.Response(200, json=refusal), lambda c: europe_pmc.search('x' * 1501, 10, http_client=c))


def test_an_unreadable_result_list_is_a_fault_never_an_empty_page() -> None:
    with pytest.raises(ValueError, match='result list'):
        _run(
            lambda _r: httpx2.Response(200, json={'hitCount': 4130, 'resultList': 'gone'}),
            lambda c: europe_pmc.search('GENE1', 10, http_client=c),
        )


def test_a_transient_failure_stays_retryable() -> None:
    with pytest.raises(httpx2.HTTPStatusError):
        _run(lambda _r: httpx2.Response(503, text='busy'), lambda c: europe_pmc.search('GENE1', 10, http_client=c))


def test_authors_come_back_one_per_entry_with_a_group_author_among_them() -> None:
    # A record's byline is read off `authorList`, not the pre-joined `authorString`: a name may carry
    # the comma the join uses, so splitting it back apart invents boundaries. A consortium states a
    # `collectiveName` and no `fullName`, and reading only the latter would drop it from the byline.
    record = _search_one(
        {
            'pmid': '1',
            'authorString': 'Xu W, Smith Jones, A, PRACTICAL Consortium.',
            'authorList': {
                'author': [
                    {'fullName': 'Xu W', 'initials': 'W'},
                    {'fullName': 'Smith Jones, A'},
                    {'collectiveName': 'PRACTICAL Consortium'},
                    {'initials': 'Q'},
                ]
            },
        }
    )

    assert record.authors == ('Xu W', 'Smith Jones, A', 'PRACTICAL Consortium')


def test_a_record_naming_no_authors_carries_none() -> None:
    # Empty, never synthesised from `authorString`: an index that states no author list states no byline.
    record = _search_one({'pmid': '1', 'authorString': 'Xu W, Yang X.'})

    assert record.authors == ()
