"""Tests for `themis.litcache.openalex` — the batch DOI resolver + PubmedArticle build."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
from pubmed_proto import pubmed_pb2

from themis.litcache import openalex

_DOI_A = '10.1/a'
_DOI_B = '10.1/b'


def _work(doi: str, **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        'doi': f'https://doi.org/{doi}',
        'display_name': 'Title A',
        'publication_date': '2020-01-15',
        'ids': {'pmid': 'https://pubmed.ncbi.nlm.nih.gov/111'},
        'type': 'article',
        'primary_location': {'source': {'display_name': 'Journal A', 'host_organization_name': 'Pub A'}},
    }
    record.update(overrides)
    return record


def _response(records: list[dict[str, object]]) -> bytes:
    return json.dumps({'meta': {'count': len(records)}, 'results': records}).encode()


def test_parse_strips_id_urls_and_flags_preprint() -> None:
    records = [
        _work(_DOI_A),
        _work(
            _DOI_B,
            display_name='Preprint B',
            ids={},
            type='preprint',
            primary_location={'source': {'display_name': 'medRxiv'}},
        ),
    ]
    works = openalex.parse_response(_response(records))

    assert set(works) == {_DOI_A, _DOI_B}
    assert works[_DOI_A].pmid == '111'  # stripped from the pubmed URL
    assert works[_DOI_A].journal == 'Journal A'
    assert works[_DOI_A].is_preprint is False
    assert works[_DOI_B].pmid is None
    assert works[_DOI_B].is_preprint is True


def test_parse_skips_records_without_a_doi() -> None:
    works = openalex.parse_response(_response([{'display_name': 'No DOI', 'ids': {}}]))
    assert works == {}


def test_parse_rejects_a_non_works_payload() -> None:
    with pytest.raises(ValueError, match='results'):
        openalex.parse_response(json.dumps({'meta': {'count': 0}}).encode())


def test_fetch_rejects_a_batch_over_the_cap() -> None:
    async def run() -> None:
        async with httpx.AsyncClient() as client:
            await openalex.fetch([f'10.1/{i}' for i in range(51)], http_client=client)

    with pytest.raises(ValueError, match='caps at 50'):
        asyncio.run(run())


def test_resolve_drives_fetch_with_a_doi_filter() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, content=_response([_work(_DOI_A)]))

    async def run() -> dict[str, openalex.OpenAlexWork]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await openalex.resolve([_DOI_A], http_client=client)

    works = asyncio.run(run())
    assert seen['filter'] == f'doi:{_DOI_A}'
    assert works[_DOI_A].title == 'Title A'


def test_to_pubmed_article_builds_a_valid_record() -> None:
    work = openalex.parse_response(_response([_work(_DOI_A)]))[_DOI_A]
    article = pubmed_pb2.PubmedArticle.FromString(openalex.to_pubmed_article(work))

    assert article.medline_citation.article.article_title.value == 'Title A'
    assert article.medline_citation.status == pubmed_pb2.MedlineCitation.STATUS_PUBLISHER
    assert article.medline_citation.article.journal.title == 'Journal A'
    doi_ids = [
        aid.value for aid in article.pubmed_data.article_id_list if aid.id_type == pubmed_pb2.ArticleId.ID_TYPE_DOI
    ]
    assert doi_ids == [_DOI_A]


def test_to_pubmed_article_fails_loud_without_a_date() -> None:
    work = openalex.parse_response(_response([_work(_DOI_A, publication_date=None)]))[_DOI_A]
    with pytest.raises(ValueError, match='no publication date'):
        openalex.to_pubmed_article(work)


def test_to_pubmed_article_fails_loud_without_a_title() -> None:
    work = openalex.parse_response(_response([_work(_DOI_A, display_name=None)]))[_DOI_A]
    with pytest.raises(ValueError, match='no title'):
        openalex.to_pubmed_article(work)


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_OPENALEX_LIVE_DOI'),
    reason='set LITCACHE_OPENALEX_LIVE_DOI to hit the live OpenAlex works API',
)
def test_live_openalex() -> None:
    doi = os.environ['LITCACHE_OPENALEX_LIVE_DOI']

    async def run() -> dict[str, openalex.OpenAlexWork]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await openalex.resolve([doi], http_client=client)

    asyncio.run(run())
