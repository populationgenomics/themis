"""PubMed efetch → canonical metadata.pb + harvested cross-ids.

The pure parse is exercised against a committed efetch fixture (the OA paper, PMID
29089047 — CC-BY, so its record is redistributable on the public mirror); the fetch
path is driven by an httpx `MockTransport` so the offline suite stays deterministic.
A live efetch is integration-gated on `LITCACHE_EFETCH_LIVE_PMID`.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import urllib.parse

import httpx
import pubmed_proto
import pytest

from themis.litcache import efetch
from themis.litcache.models import litcache_pb2

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_EFETCH_XML = _FIXTURES / 'oa' / 'efetch.xml'
_PMID = '29089047'


def test_parse_response_validates_and_keys_by_pmid() -> None:
    resolved = efetch.parse_response(_EFETCH_XML.read_bytes())

    assert set(resolved) == {_PMID}
    # the metadata.pb bytes parse straight back to a PubmedArticle.
    article = pubmed_proto.pubmed_pb2.PubmedArticle.FromString(resolved[_PMID].metadata)
    assert article.medline_citation.pmid.value == _PMID
    title = article.medline_citation.article.article_title.value
    assert 'Whole exome sequencing' in title


def test_cross_ids_harvested_from_own_id_list() -> None:
    resolved = efetch.parse_response(_EFETCH_XML.read_bytes())
    # DOI + PMCID from PubmedData.ArticleIdList, PMID from MedlineCitation; the
    # reference-list citation ids in the record are not harvested.
    assert resolved[_PMID].external_ids == litcache_pb2.ExternalIds(
        doi='10.1186/s13073-017-0482-5',
        pmid=_PMID,
        pmcid='PMC5664429',
    )


def test_empty_set_yields_no_record() -> None:
    # efetch returns an empty set for an unknown PMID — the caller's `unknown`.
    assert efetch.parse_response(b'<PubmedArticleSet></PubmedArticleSet>') == {}


def test_unexpected_root_fails_loud() -> None:
    with pytest.raises(ValueError, match='PubmedArticleSet'):
        efetch.parse_response(b'<eFetchResult><ERROR>bad id</ERROR></eFetchResult>')


def test_fetch_requires_a_pmid() -> None:
    async def run() -> None:
        async with httpx.AsyncClient() as client:
            await efetch.fetch([], http_client=client)

    with pytest.raises(ValueError, match='at least one PMID'):
        asyncio.run(run())


def test_fetch_posts_the_id_list_in_the_body() -> None:
    # efetch always POSTs: the id list rides the body (no GET inline-id ceiling), so a
    # batch of any size takes one path and the URL carries no `id=`.
    pmids = [str(i) for i in range(250)]
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen['method'] = request.method
        seen['body'] = request.content.decode()
        seen['query_id'] = request.url.params.get('id', '')
        return httpx.Response(200, content=b'<PubmedArticleSet></PubmedArticleSet>')

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await efetch.fetch(pmids, http_client=client)

    asyncio.run(run())

    assert seen['method'] == 'POST'
    assert 'id=' in seen['body']
    assert seen['query_id'] == ''  # the id list is in the body, not the URL


def test_resolve_drives_efetch_and_parses() -> None:
    body = _EFETCH_XML.read_bytes()
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # efetch POSTs, so the query rides the form body, not the URL.
        seen.update(dict(urllib.parse.parse_qsl(request.content.decode())))
        return httpx.Response(200, content=body)

    async def run() -> dict[str, efetch.ResolvedMetadata]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await efetch.resolve([_PMID], http_client=client)

    resolved = asyncio.run(run())

    assert seen['db'] == 'pubmed'
    assert seen['id'] == _PMID
    assert set(resolved) == {_PMID}
    assert resolved[_PMID].external_ids.pmid == _PMID


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_EFETCH_LIVE_PMID'),
    reason='set LITCACHE_EFETCH_LIVE_PMID to hit live NCBI efetch',
)
def test_live_efetch() -> None:
    pmid = os.environ['LITCACHE_EFETCH_LIVE_PMID']

    async def run() -> dict[str, efetch.ResolvedMetadata]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await efetch.resolve([pmid], http_client=client)

    resolved = asyncio.run(run())
    assert pmid in resolved
    pubmed_proto.pubmed_pb2.PubmedArticle.FromString(resolved[pmid].metadata)
