"""Crossref → PubmedArticle mapping for DOI-only papers.

The pure mapping runs against a committed Crossref cassette (the OA paper's DOI —
Crossref metadata is CC0, so it is redistributable; its response carries no PMID, so
it exercises the DOI-only path). The fetch path is driven by an httpx `MockTransport`;
a live Crossref call is integration-gated on `LITCACHE_CROSSREF_LIVE_DOI`.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import pathlib

import httpx
import pubmed_proto
import pytest

from themis.litcache import crossref
from themis.litcache.models import litcache_pb2

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_CROSSREF_JSON = _FIXTURES / 'oa' / 'crossref.json'
_DOI = '10.1186/s13073-017-0482-5'


def _work() -> dict[str, object]:
    return json.loads(_CROSSREF_JSON.read_bytes())['message']


def test_from_crossref_work_maps_the_field_subset() -> None:
    result = crossref.from_crossref_work(_work())
    article = pubmed_proto.pubmed_pb2.PubmedArticle.FromString(result.metadata)

    cit = article.medline_citation
    # publisher-supplied, not MEDLINE-indexed; no PMID for a DOI-only paper.
    assert cit.status == pubmed_proto.pubmed_pb2.MedlineCitation.STATUS_PUBLISHER
    assert not cit.pmid.value
    assert 'Whole exome sequencing' in cit.article.article_title.value
    assert cit.article.journal.title == 'Genome Medicine'
    assert cit.article.journal.journal_issue.volume == '9'
    assert cit.article.journal.journal_issue.pub_date.ToDatetime() == datetime.datetime(2017, 10, 31)  # noqa: DTZ001 — naive-UTC
    authors = cit.article.author_list
    assert authors.author[0].last_name == 'Li'
    assert authors.author[0].fore_name == 'Alexander H.'
    # the DOI is the record's own id.
    assert [(a.id_type, a.value) for a in article.pubmed_data.article_id_list] == [
        (pubmed_proto.pubmed_pb2.ArticleId.ID_TYPE_DOI, _DOI)
    ]


def test_external_ids_and_publisher() -> None:
    result = crossref.from_crossref_work(_work())
    assert result.external_ids == litcache_pb2.ExternalIds(doi=_DOI)
    assert result.publisher == 'Springer Science and Business Media LLC'


def test_missing_doi_fails_loud() -> None:
    with pytest.raises(ValueError, match='no DOI'):
        crossref.from_crossref_work({'title': ['t']})


def test_missing_title_fails_loud() -> None:
    with pytest.raises(ValueError, match='no title'):
        crossref.from_crossref_work({'DOI': _DOI})


def test_missing_date_fails_loud() -> None:
    with pytest.raises(ValueError, match='no usable publication date'):
        crossref.from_crossref_work({'DOI': _DOI, 'title': ['t']})


def test_empty_title_fails_loud() -> None:
    # Crossref emits title: [''] for some records; an empty title is as degenerate
    # as an absent one, so it fails loud rather than mapping to an empty ArticleTitle.
    with pytest.raises(ValueError, match='no title'):
        crossref.from_crossref_work({'DOI': _DOI, 'title': ['']})


def test_collective_authors_are_preserved_and_empty_entries_dropped() -> None:
    work = {
        'DOI': _DOI,
        'title': ['T'],
        'issued': {'date-parts': [[2020, 1, 1]]},
        'author': [
            {'family': 'Li', 'given': 'Alexander H.'},
            {'name': 'Deciphering Developmental Disorders Study'},  # consortium: name-only
            {'affiliation': []},  # names nobody: dropped
        ],
    }
    article = pubmed_proto.pubmed_pb2.PubmedArticle.FromString(crossref.from_crossref_work(work).metadata)
    authors = article.medline_citation.article.author_list.author

    assert len(authors) == 2
    assert (authors[0].last_name, authors[0].fore_name) == ('Li', 'Alexander H.')
    assert authors[1].collective_name.value == 'Deciphering Developmental Disorders Study'
    assert not authors[1].last_name


def test_null_date_part_falls_through_to_the_next_field() -> None:
    # `issued` carries a null part (no known date); the fallback must reach `published`
    # rather than aborting on int(None).
    work = {
        'DOI': _DOI,
        'title': ['T'],
        'issued': {'date-parts': [[None]]},
        'published': {'date-parts': [[2019, 5]]},
    }
    article = pubmed_proto.pubmed_pb2.PubmedArticle.FromString(crossref.from_crossref_work(work).metadata)
    pub_date = article.medline_citation.article.journal.journal_issue.pub_date
    assert pub_date.ToDatetime() == datetime.datetime(2019, 5, 1)  # noqa: DTZ001 — naive-UTC


def test_only_a_null_date_fails_loud() -> None:
    work = {'DOI': _DOI, 'title': ['T'], 'issued': {'date-parts': [[None]]}}
    with pytest.raises(ValueError, match='no usable publication date'):
        crossref.from_crossref_work(work)


def test_resolve_drives_crossref_and_maps() -> None:
    body = _CROSSREF_JSON.read_bytes()
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen['path'] = request.url.path
        seen['mailto'] = request.url.params.get('mailto')
        return httpx.Response(200, content=body)

    async def run() -> crossref.CrossrefResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await crossref.resolve(_DOI, http_client=client)

    result = asyncio.run(run())

    assert seen['path'] == f'/works/{_DOI}'
    assert seen['mailto']
    assert result.external_ids.doi == _DOI


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_CROSSREF_LIVE_DOI'),
    reason='set LITCACHE_CROSSREF_LIVE_DOI to hit live Crossref',
)
def test_live_crossref() -> None:
    doi = os.environ['LITCACHE_CROSSREF_LIVE_DOI']

    async def run() -> crossref.CrossrefResult:
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await crossref.resolve(doi, http_client=client)

    result = asyncio.run(run())
    pubmed_proto.pubmed_pb2.PubmedArticle.FromString(result.metadata)
    assert result.external_ids.doi == doi
