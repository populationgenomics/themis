"""Tests for the metadata resolver ladder (`themis.litcache.resolve`).

Drives the efetch → Crossref ladder offline with an httpx `MockTransport` over the
committed cassettes (`oa/efetch.xml`, `oa/crossref.json`), proving each rung, the
PMID-miss → DOI fallback, and the fully-unknown fail-loud. No network.
"""

from __future__ import annotations

import asyncio
import functools
import json
import pathlib
from collections.abc import Callable

import httpx
import litfetch
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


def test_crossref_429_is_retried_then_resolves() -> None:
    # A transient rate response must not fail the paper: back off (Retry-After) and retry.
    calls = {'n': 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        if calls['n'] == 1:
            return httpx.Response(429, headers={'retry-after': '0'})
        return httpx.Response(200, content=_CROSSREF_JSON)

    result = _resolve(handler, pmid=None, doi=_DOI)
    assert result.external_ids.doi == _DOI
    assert calls['n'] == 2  # one 429, then the retry resolved


def test_crossref_persistent_429_propagates() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={'retry-after': '0'})

    with pytest.raises(httpx.HTTPStatusError):
        _resolve(handler, pmid=None, doi=_DOI)


def test_no_ids_is_fully_unknown() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError('no fetch should happen without ids')

    with pytest.raises(resolve.MetadataUnresolvedError) as excinfo:
        _resolve(handler, pmid=None, doi=None)
    assert excinfo.value.pmid is None
    assert excinfo.value.doi is None


def _idconv_json(doi: str, *, pmcid: str, pmid: str) -> bytes:
    return json.dumps({'status': 'ok', 'records': [{'doi': doi, 'pmcid': pmcid, 'pmid': pmid}]}).encode()


def _resolve_batch(handler: _Handler, requests: list[resolve.ResolveRequest]) -> dict[str, resolve.ResolvedPaper]:
    async def run() -> dict[str, resolve.ResolvedPaper]:
        transport = httpx.MockTransport(handler)
        async with (
            httpx.AsyncClient(transport=transport) as client,
            litfetch.Session(client_factory=functools.partial(httpx.AsyncClient, transport=transport)) as session,
        ):
            return await resolve.resolve_batch(requests, http_client=client, session=session)

    return asyncio.run(run())


def test_resolve_batch_resolves_a_pmid_via_efetch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert 'efetch' in request.url.path
        return httpx.Response(200, content=_EFETCH_XML)

    resolved = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='k1', pmid=_PMID, doi=_DOI)])
    assert set(resolved) == {'k1'}
    assert resolved['k1'].publisher is None
    assert resolved['k1'].external_ids.pmcid == 'PMC5664429'


def _openalex_json(records: list[dict[str, object]]) -> bytes:
    return json.dumps({'meta': {'count': len(records)}, 'results': records}).encode()


def _oa_work(doi: str, *, pmid: str | None, title: str = 'OA Title') -> dict[str, object]:
    return {
        'doi': f'https://doi.org/{doi}',
        'display_name': title,
        'publication_date': '2020-01-01',
        'ids': {'pmid': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}'} if pmid is not None else {},
        'type': 'article' if pmid is not None else 'preprint',
        'primary_location': {'source': {'display_name': 'Jrnl', 'host_organization_name': 'Pub X'}},
    }


def _doi_handler(*, idconv_pmc: bool, openalex: list[dict[str, object]]) -> _Handler:
    """A resolve-batch transport: idconv (PMC hit or error), OpenAlex, efetch. No Crossref."""

    def handler(request: httpx.Request) -> httpx.Response:
        if 'openalex' in request.url.host:
            return httpx.Response(200, content=_openalex_json(openalex))
        if 'idconv' in request.url.path:
            if idconv_pmc:
                return httpx.Response(200, content=_idconv_json(_DOI, pmcid='PMC5664429', pmid=_PMID))
            body = json.dumps({'status': 'ok', 'records': [{'doi': _DOI, 'status': 'error', 'errmsg': 'not in PMC'}]})
            return httpx.Response(200, content=body.encode())
        if 'efetch' in request.url.path:
            return httpx.Response(200, content=_EFETCH_XML)
        raise AssertionError(f'unexpected request (Crossref must not be hit): {request.url}')

    return handler


def test_resolve_batch_pmc_doi_routes_idconv_pmid_into_efetch() -> None:
    # In PMC: idconv gives the pmid → batched efetch yields the PubMed-native record.
    handler = _doi_handler(idconv_pmc=True, openalex=[])
    resolved = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='k', pmid=None, doi=_DOI)])
    assert set(resolved) == {'k'}
    assert resolved['k'].publisher is None  # efetch path, not OpenAlex
    assert resolved['k'].external_ids.pmid == _PMID
    assert resolved['k'].external_ids.pmcid == 'PMC5664429'


def test_resolve_batch_pubmed_not_pmc_doi_routes_openalex_pmid_into_efetch() -> None:
    # Not in PMC, but in PubMed: OpenAlex supplies the pmid → efetch yields the record.
    handler = _doi_handler(idconv_pmc=False, openalex=[_oa_work(_DOI, pmid=_PMID)])
    resolved = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='k', pmid=None, doi=_DOI)])
    assert set(resolved) == {'k'}
    assert resolved['k'].publisher is None  # PubMed-native via efetch, not the OpenAlex record
    assert resolved['k'].external_ids.pmid == _PMID


def test_resolve_batch_non_pubmed_doi_uses_the_openalex_record() -> None:
    # No pmid anywhere (a preprint): OpenAlex's own record becomes the metadata.
    handler = _doi_handler(idconv_pmc=False, openalex=[_oa_work(_DOI, pmid=None, title='A Preprint')])
    resolved = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='k', pmid=None, doi=_DOI)])
    assert set(resolved) == {'k'}
    assert resolved['k'].publisher == 'Pub X'  # from the OpenAlex record
    assert (
        pubmed_proto.pubmed_pb2.PubmedArticle.FromString(
            resolved['k'].metadata
        ).medline_citation.article.article_title.value
        == 'A Preprint'
    )


def test_resolve_batch_batches_pmids_into_one_efetch_call() -> None:
    efetch_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal efetch_calls
        assert 'efetch' in request.url.path
        efetch_calls += 1
        return httpx.Response(200, content=_EFETCH_XML)

    requests = [
        resolve.ResolveRequest(claim_key='a', pmid=_PMID, doi=None),
        resolve.ResolveRequest(claim_key='b', pmid='11111111', doi=None),
    ]
    resolved = _resolve_batch(handler, requests)
    assert efetch_calls == 1  # both PMIDs ride one efetch call
    assert set(resolved) == {'a'}  # 'b' is absent from the fixture set → unresolved, not raised


def test_resolve_batch_omits_the_unresolvable() -> None:
    # idconv errors (not in PMC) and OpenAlex knows nothing: a full miss, not raised.
    handler = _doi_handler(idconv_pmc=False, openalex=[])
    resolved = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='gone', pmid=None, doi=_DOI)])
    assert resolved == {}


_DOI_UNKNOWN = '10.9999/unknown'


def test_resolve_batch_partial_one_doi_resolves_one_absent() -> None:
    # Two DOIs through the batched resolver: the first resolves (idconv gives its pmid →
    # efetch), the second is unknown to every source. A batch is not failed by the miss,
    # and each DOI maps to its own record — no positional misalignment.
    def handler(request: httpx.Request) -> httpx.Response:
        path, host = request.url.path, request.url.host
        if 'idconv' in path:
            records = [
                {'doi': _DOI, 'pmid': int(_PMID), 'pmcid': 'PMC5664429'},
                {'doi': _DOI_UNKNOWN, 'status': 'error'},
            ]
            return httpx.Response(200, content=json.dumps({'records': records}).encode())
        if 'openalex' in host:
            return httpx.Response(200, content=_openalex_json([]))
        if 'efetch' in path:
            return httpx.Response(200, content=_EFETCH_XML)
        if 'ebi.ac.uk' in host:
            return httpx.Response(200, content=json.dumps({'resultList': {'result': []}}).encode())
        raise AssertionError(f'unexpected request: {request.url}')

    resolved = _resolve_batch(
        handler,
        [
            resolve.ResolveRequest(claim_key='hit', pmid=None, doi=_DOI),
            resolve.ResolveRequest(claim_key='miss', pmid=None, doi=_DOI_UNKNOWN),
        ],
    )
    assert set(resolved) == {'hit'}  # the unknown DOI is absent, not a raised failure
    assert resolved['hit'].external_ids.pmid == _PMID  # mapped to its own record, not the other DOI's


def test_resolve_batch_pmid_miss_then_doi_resolves_via_batch() -> None:
    # A request carrying both a PMID (that efetch misses) and a DOI: the PMID miss falls
    # through to the batched DOI path, which resolves it.
    def handler(request: httpx.Request) -> httpx.Response:
        path, host = request.url.path, request.url.host
        if 'efetch' in path:
            # The DOI-discovered PMID returns the record; the original (missing) PMID does not.
            if _PMID in request.content.decode() or _PMID in request.url.params.get('id', ''):
                return httpx.Response(200, content=_EFETCH_XML)
            return httpx.Response(200, content=b'<PubmedArticleSet></PubmedArticleSet>')
        if 'idconv' in path:
            record = {'doi': _DOI, 'pmid': int(_PMID), 'pmcid': 'PMC5664429'}
            return httpx.Response(200, content=json.dumps({'records': [record]}).encode())
        if 'openalex' in host:
            return httpx.Response(200, content=_openalex_json([]))
        if 'ebi.ac.uk' in host:
            return httpx.Response(200, content=json.dumps({'resultList': {'result': []}}).encode())
        raise AssertionError(f'unexpected request: {request.url}')

    resolved = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='k', pmid='99999999', doi=_DOI)])
    assert set(resolved) == {'k'}
    assert resolved['k'].external_ids.pmid == _PMID  # resolved via the DOI batch after the PMID miss
