"""Tests for the metadata resolver ladder (`themis.litcache.resolve`).

Drives the efetch → Crossref ladder offline with an httpx `MockTransport` over the
committed cassettes (`oa/efetch.xml`, `oa/crossref.json`), proving each rung, the
PMID-miss → DOI fallback, and the fully-unknown fail-loud. No network.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Callable

import httpx
import pubmed_proto
import pytest

from themis.litcache import resolve

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_EFETCH_XML = (_FIXTURES / 'oa' / 'efetch.xml').read_bytes()
_CROSSREF_JSON = (_FIXTURES / 'oa' / 'crossref.json').read_bytes()
_PMID = '29089047'
_DOI = '10.1186/s13073-017-0482-5'


_Handler = Callable[[httpx.Request], httpx.Response]


def _resolve(handler: _Handler, *, pmid: str | None, doi: str | None) -> resolve.ResolvedPaper:
    async def run() -> resolve.ResolvedPaper:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await resolve.resolve_metadata(pmid=pmid, doi=doi, http_client=client)

    return asyncio.run(run())


def test_pmid_resolves_via_efetch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert 'efetch' in request.url.path
        return httpx.Response(200, content=_EFETCH_XML)

    result = _resolve(handler, pmid=_PMID, doi=_DOI)
    # The PMID rung wins (efetch never even consults the DOI); cross-ids harvested.
    assert result.publisher is None
    assert result.external_ids.pmid == _PMID
    assert result.external_ids.doi == _DOI
    assert result.external_ids.pmcid == 'PMC5664429'
    assert pubmed_proto.pubmed_pb2.PubmedArticle.FromString(result.metadata)


def test_doi_only_resolves_via_crossref() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f'/works/{_DOI}'
        return httpx.Response(200, content=_CROSSREF_JSON)

    result = _resolve(handler, pmid=None, doi=_DOI)
    assert result.external_ids.doi == _DOI
    assert result.publisher is not None  # Crossref supplies it; efetch would not


def test_pmid_miss_falls_back_to_crossref() -> None:
    # efetch returns an empty set (the PMID is unknown), so the DOI rung resolves.
    def handler(request: httpx.Request) -> httpx.Response:
        if 'efetch' in request.url.path:
            return httpx.Response(200, content=b'<PubmedArticleSet></PubmedArticleSet>')
        return httpx.Response(200, content=_CROSSREF_JSON)

    result = _resolve(handler, pmid='99999999', doi=_DOI)
    assert result.external_ids.doi == _DOI
    assert result.publisher is not None


def test_crossref_404_is_a_miss_not_a_failure() -> None:
    # A 404 from Crossref means "unknown DOI" → the paper is fully unknown.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(resolve.MetadataUnresolvedError):
        _resolve(handler, pmid=None, doi=_DOI)


def test_non_404_crossref_error_propagates() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(httpx.HTTPStatusError):
        _resolve(handler, pmid=None, doi=_DOI)


def test_no_ids_is_fully_unknown() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError('no fetch should happen without ids')

    with pytest.raises(resolve.MetadataUnresolvedError) as excinfo:
        _resolve(handler, pmid=None, doi=None)
    assert excinfo.value.pmid is None
    assert excinfo.value.doi is None
