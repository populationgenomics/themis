"""Tests for the metadata resolver ladder (`themis.litcache.resolve`).

Drives the efetch → Crossref ladder offline with an httpx2 `MockTransport` over the
committed cassettes (`oa/efetch.xml`, `oa/crossref.json`, the synthetic
`bookshelf/efetch.xml`), proving each rung, the PMID-miss → DOI fallback, and the
fully-unknown fail-loud. No network.
"""

from __future__ import annotations

import asyncio
import functools
import json
import pathlib
from collections.abc import Callable

import httpx2
import litfetch
import pytest

from themis.litcache import efetch, mirror, paper_metadata, resolve
from themis.litcache.models import litcache_pb2

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_EFETCH_XML = (_FIXTURES / 'oa' / 'efetch.xml').read_bytes()
_BOOK_EFETCH_XML = (_FIXTURES / 'bookshelf' / 'efetch.xml').read_bytes()
_CROSSREF_JSON = (_FIXTURES / 'oa' / 'crossref.json').read_bytes()
_PMID = '29089047'
_BOOK_PMID = '30000010'
_DOI = '10.1186/s13073-017-0482-5'
_DOI_UNKNOWN = '10.9999/unknown'


def _oa_set_with_accessionless_book() -> bytes:
    """The recorded OA set with the synthetic book record beside it, its accession struck out."""
    start = _BOOK_EFETCH_XML.index(b'<PubmedBookArticle>')
    end = _BOOK_EFETCH_XML.index(b'</PubmedBookArticle>') + len(b'</PubmedBookArticle>')
    book = _BOOK_EFETCH_XML[start:end].replace(b'<ArticleId IdType="bookaccession">NBK900001</ArticleId>', b'')
    return _EFETCH_XML.replace(b'</PubmedArticleSet>', book + b'</PubmedArticleSet>')


_Handler = Callable[[httpx2.Request], httpx2.Response]


def _resolve(handler: _Handler, *, pmid: str | None, doi: str | None) -> resolve.ResolvedPaper:
    async def run() -> resolve.ResolvedPaper:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await resolve.resolve_metadata(pmid=pmid, doi=doi, http_client=client)

    return asyncio.run(run())


def _record(metadata: bytes) -> litcache_pb2.PaperMetadata:
    return litcache_pb2.PaperMetadata.FromString(metadata)


def test_pmid_resolves_via_efetch() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert 'efetch' in request.url.path
        return httpx2.Response(200, content=_EFETCH_XML)

    result = _resolve(handler, pmid=_PMID, doi=_DOI)
    # The PMID rung wins (efetch never even consults the DOI); cross-ids harvested.
    assert result.publisher is None
    assert result.external_ids.pmid == _PMID
    assert result.external_ids.doi == _DOI
    assert result.external_ids.pmcid == 'PMC5664429'
    assert _record(result.metadata).pubmed.WhichOneof('kind') == 'article'


def test_pmid_naming_a_book_record_resolves_via_efetch() -> None:
    # A Bookshelf chapter's PMID answers with PubMed's book record: the envelope's book arm, and the
    # accession harvested beside the PMID.
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert 'efetch' in request.url.path
        return httpx2.Response(200, content=_BOOK_EFETCH_XML)

    result = _resolve(handler, pmid=_BOOK_PMID, doi=None)
    assert result.publisher is None
    assert result.external_ids == litcache_pb2.ExternalIds(pmid=_BOOK_PMID, bookid='NBK900001')
    assert _record(result.metadata).pubmed.WhichOneof('kind') == 'book_article'


def test_pmid_naming_a_book_record_failing_the_precondition_raises_and_tries_no_other_rung() -> None:
    # efetch answers the PMID with a record that fails the store's precondition: the paper raises by
    # reason, and the DOI is not tried in its place — Crossref would put a journal-shaped record where
    # one already exists.
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert 'efetch' in request.url.path, f'Crossref must not be hit: {request.url}'
        return httpx2.Response(200, content=_oa_set_with_accessionless_book())

    with pytest.raises(efetch.RecordPreconditionError, match='no Bookshelf accession'):
        _resolve(handler, pmid=_BOOK_PMID, doi=_DOI)


def test_doi_only_resolves_via_crossref() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == f'/works/{_DOI}'
        return httpx2.Response(200, content=_CROSSREF_JSON)

    result = _resolve(handler, pmid=None, doi=_DOI)
    assert result.external_ids.doi == _DOI
    assert result.publisher is not None  # Crossref supplies it; efetch would not


def test_pmid_miss_falls_back_to_crossref() -> None:
    # efetch returns an empty set (the PMID is unknown), so the DOI rung resolves.
    def handler(request: httpx2.Request) -> httpx2.Response:
        if 'efetch' in request.url.path:
            return httpx2.Response(200, content=b'<PubmedArticleSet></PubmedArticleSet>')
        return httpx2.Response(200, content=_CROSSREF_JSON)

    result = _resolve(handler, pmid='99999999', doi=_DOI)
    assert result.external_ids.doi == _DOI
    assert result.publisher is not None


def test_crossref_404_is_a_miss_not_a_failure() -> None:
    # A 404 from Crossref means "unknown DOI" → the paper is fully unknown.
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404)

    with pytest.raises(resolve.MetadataUnresolvedError):
        _resolve(handler, pmid=None, doi=_DOI)


def test_non_404_crossref_error_propagates() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500)

    with pytest.raises(httpx2.HTTPStatusError):
        _resolve(handler, pmid=None, doi=_DOI)


def test_crossref_429_is_retried_then_resolves() -> None:
    # A transient rate response must not fail the paper: back off (Retry-After) and retry.
    calls = {'n': 0}

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls['n'] += 1
        if calls['n'] == 1:
            return httpx2.Response(429, headers={'retry-after': '0'})
        return httpx2.Response(200, content=_CROSSREF_JSON)

    result = _resolve(handler, pmid=None, doi=_DOI)
    assert result.external_ids.doi == _DOI
    assert calls['n'] == 2  # one 429, then the retry resolved


def test_crossref_persistent_429_propagates() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={'retry-after': '0'})

    with pytest.raises(httpx2.HTTPStatusError):
        _resolve(handler, pmid=None, doi=_DOI)


def test_no_ids_is_fully_unknown() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:  # pragma: no cover - never called
        raise AssertionError('no fetch should happen without ids')

    with pytest.raises(resolve.MetadataUnresolvedError) as excinfo:
        _resolve(handler, pmid=None, doi=None)
    assert excinfo.value.pmid is None
    assert excinfo.value.doi is None


def _idconv_json(doi: str, *, pmcid: str, pmid: str) -> bytes:
    return json.dumps({'status': 'ok', 'records': [{'doi': doi, 'pmcid': pmcid, 'pmid': pmid}]}).encode()


def _resolve_batch(handler: _Handler, requests: list[resolve.ResolveRequest]) -> dict[str, resolve.Outcome]:
    async def run() -> dict[str, resolve.Outcome]:
        transport = httpx2.MockTransport(handler)
        async with (
            httpx2.AsyncClient(transport=transport) as client,
            litfetch.Session(client_factory=functools.partial(httpx2.AsyncClient, transport=transport)) as session,
        ):
            return await resolve.resolve_batch(requests, http_client=client, session=session)

    return asyncio.run(run())


def _resolved(outcomes: dict[str, resolve.Outcome], claim_key: str) -> resolve.ResolvedPaper:
    """The paper under `claim_key`, which the test expects resolved."""
    outcome = outcomes[claim_key]
    assert isinstance(outcome, resolve.ResolvedPaper), outcome
    return outcome


def test_resolve_batch_resolves_a_pmid_via_efetch() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert 'efetch' in request.url.path
        return httpx2.Response(200, content=_EFETCH_XML)

    resolved = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='k1', pmid=_PMID, doi=_DOI)])
    assert set(resolved) == {'k1'}
    assert _resolved(resolved, 'k1').publisher is None
    assert _resolved(resolved, 'k1').external_ids.pmcid == 'PMC5664429'


def _openalex_json(records: list[dict[str, object]]) -> bytes:
    return json.dumps({'meta': {'count': len(records)}, 'results': records}).encode()


def _oa_work(doi: str, *, pmid: str | None, title: str = 'OA Title') -> dict[str, object]:
    return {
        'doi': f'https://doi.org/{doi}',
        'title': title,
        'display_name': title,
        'publication_date': '2020-01-01',
        'ids': {'pmid': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}'} if pmid is not None else {},
        'type': 'article' if pmid is not None else 'preprint',
        'primary_location': {'source': {'display_name': 'Jrnl', 'host_organization_name': 'Pub X'}},
    }


def _doi_handler(*, idconv_pmc: bool, openalex: list[dict[str, object]]) -> _Handler:
    """A resolve-batch transport: idconv (PMC hit or error), OpenAlex, efetch. No Crossref."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if 'openalex' in request.url.host:
            return httpx2.Response(200, content=_openalex_json(openalex))
        if 'idconv' in request.url.path:
            if idconv_pmc:
                return httpx2.Response(200, content=_idconv_json(_DOI, pmcid='PMC5664429', pmid=_PMID))
            body = json.dumps({'status': 'ok', 'records': [{'doi': _DOI, 'status': 'error', 'errmsg': 'not in PMC'}]})
            return httpx2.Response(200, content=body.encode())
        if 'efetch' in request.url.path:
            return httpx2.Response(200, content=_EFETCH_XML)
        raise AssertionError(f'unexpected request (Crossref must not be hit): {request.url}')

    return handler


def test_resolve_batch_pmc_doi_routes_idconv_pmid_into_efetch() -> None:
    # In PMC: idconv gives the pmid → batched efetch yields the PubMed-native record.
    handler = _doi_handler(idconv_pmc=True, openalex=[])
    resolved = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='k', pmid=None, doi=_DOI)])
    assert set(resolved) == {'k'}
    assert _resolved(resolved, 'k').publisher is None  # efetch path, not OpenAlex
    assert _resolved(resolved, 'k').external_ids.pmid == _PMID
    assert _resolved(resolved, 'k').external_ids.pmcid == 'PMC5664429'


def test_resolve_batch_pubmed_not_pmc_doi_routes_openalex_pmid_into_efetch() -> None:
    # Not in PMC, but in PubMed: OpenAlex supplies the pmid → efetch yields the record.
    handler = _doi_handler(idconv_pmc=False, openalex=[_oa_work(_DOI, pmid=_PMID)])
    resolved = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='k', pmid=None, doi=_DOI)])
    assert set(resolved) == {'k'}
    assert _resolved(resolved, 'k').publisher is None  # PubMed-native via efetch, not the OpenAlex record
    assert _resolved(resolved, 'k').external_ids.pmid == _PMID


def test_resolve_batch_non_pubmed_doi_uses_the_openalex_record() -> None:
    # No pmid anywhere (a preprint): OpenAlex's own record becomes the metadata.
    handler = _doi_handler(idconv_pmc=False, openalex=[_oa_work(_DOI, pmid=None, title='A Preprint')])
    resolved = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='k', pmid=None, doi=_DOI)])
    assert set(resolved) == {'k'}
    assert _resolved(resolved, 'k').publisher == 'Pub X'  # from the OpenAlex record
    paper = _record(_resolved(resolved, 'k').metadata)
    assert paper.HasField('openalex')
    assert not paper.HasField('pubmed')
    assert paper_metadata.title(paper) == 'A Preprint'


def test_resolve_batch_charges_a_drifted_openalex_record_to_its_paper_alone() -> None:
    # OpenAlex answers with a work carrying a key the mirror lacks: the paper is a SchemaDriftFailure
    # naming the key, not a record stored thinned, and the paper beside it resolves.
    drifted = _oa_work(_DOI_UNKNOWN, pmid=None, title='Drifted') | {'is_zpac': True}
    handler = _doi_handler(idconv_pmc=False, openalex=[_oa_work(_DOI, pmid=None, title='Fine'), drifted])
    resolved = _resolve_batch(
        handler,
        [
            resolve.ResolveRequest(claim_key='fine', pmid=None, doi=_DOI),
            resolve.ResolveRequest(claim_key='drifted', pmid=None, doi=_DOI_UNKNOWN),
        ],
    )
    assert set(resolved) == {'fine', 'drifted'}
    assert paper_metadata.title(_record(_resolved(resolved, 'fine').metadata)) == 'Fine'
    outcome = resolved['drifted']
    assert isinstance(outcome, resolve.SchemaDriftFailure)
    assert 'is_zpac' in outcome.reason


def test_a_drifted_crossref_record_raises_schema_drift_naming_the_key() -> None:
    work = json.loads(_CROSSREF_JSON)
    work['message']['colour'] = 'blue'

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=json.dumps(work).encode())

    with pytest.raises(mirror.SchemaDriftError, match='colour'):
        _resolve(handler, pmid=None, doi=_DOI)


def test_resolve_batch_batches_pmids_into_one_efetch_call() -> None:
    efetch_calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal efetch_calls
        assert 'efetch' in request.url.path
        efetch_calls += 1
        return httpx2.Response(200, content=_EFETCH_XML)

    requests = [
        resolve.ResolveRequest(claim_key='a', pmid=_PMID, doi=None),
        resolve.ResolveRequest(claim_key='b', pmid='11111111', doi=None),
    ]
    resolved = _resolve_batch(handler, requests)
    assert efetch_calls == 1  # both PMIDs ride one efetch call
    assert set(resolved) == {'a'}  # 'b' is absent from the fixture set → unresolved, not raised


def test_resolve_batch_charges_a_failed_precondition_to_its_paper_alone() -> None:
    # One efetch chunk answers a journal record and a book record that fails the store's precondition:
    # the journal paper resolves, the book paper is a RecordPreconditionFailure carrying the reason, and — the
    # record existing — its DOI takes no fallback (idconv / OpenAlex are never hit).
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert 'efetch' in request.url.path, f'no DOI fallback for a record that exists: {request.url}'
        return httpx2.Response(200, content=_oa_set_with_accessionless_book())

    requests = [
        resolve.ResolveRequest(claim_key='journal', pmid=_PMID, doi=None),
        resolve.ResolveRequest(claim_key='chapter', pmid=_BOOK_PMID, doi='10.1234/synthetic.chapter'),
    ]
    outcomes = _resolve_batch(handler, requests)

    assert isinstance(outcomes['journal'], resolve.ResolvedPaper)
    chapter = outcomes['chapter']
    assert isinstance(chapter, resolve.RecordPreconditionFailure)
    assert 'no Bookshelf accession' in chapter.reason


def test_resolve_batch_doi_whose_discovered_pmid_fails_the_precondition_fails_it_too() -> None:
    # idconv maps the DOI to the book PMID, and efetch answers that PMID with a record that fails the
    # store's precondition: the failure surfaces through the DOI path as the paper's outcome.
    def handler(request: httpx2.Request) -> httpx2.Response:
        path, host = request.url.path, request.url.host
        if 'idconv' in path:
            record = {'doi': _DOI, 'pmid': int(_BOOK_PMID), 'pmcid': 'PMC900001'}
            return httpx2.Response(200, content=json.dumps({'records': [record]}).encode())
        if 'efetch' in path:
            return httpx2.Response(200, content=_oa_set_with_accessionless_book())
        if 'openalex' in host:
            return httpx2.Response(200, content=_openalex_json([]))
        if 'ebi.ac.uk' in host:
            return httpx2.Response(200, content=json.dumps({'resultList': {'result': []}}).encode())
        raise AssertionError(f'unexpected request: {request.url}')

    outcomes = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='k', pmid=None, doi=_DOI)])
    assert isinstance(outcomes['k'], resolve.RecordPreconditionFailure)


def test_resolve_batch_omits_the_unresolvable() -> None:
    # idconv errors (not in PMC) and OpenAlex knows nothing: a full miss, not raised.
    handler = _doi_handler(idconv_pmc=False, openalex=[])
    resolved = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='gone', pmid=None, doi=_DOI)])
    assert resolved == {}


def test_resolve_batch_partial_one_doi_resolves_one_absent() -> None:
    # Two DOIs through the batched resolver: the first resolves (idconv gives its pmid →
    # efetch), the second is unknown to every source. A batch is not failed by the miss,
    # and each DOI maps to its own record — no positional misalignment.
    def handler(request: httpx2.Request) -> httpx2.Response:
        path, host = request.url.path, request.url.host
        if 'idconv' in path:
            records = [
                {'doi': _DOI, 'pmid': int(_PMID), 'pmcid': 'PMC5664429'},
                {'doi': _DOI_UNKNOWN, 'status': 'error'},
            ]
            return httpx2.Response(200, content=json.dumps({'records': records}).encode())
        if 'openalex' in host:
            return httpx2.Response(200, content=_openalex_json([]))
        if 'efetch' in path:
            return httpx2.Response(200, content=_EFETCH_XML)
        if 'ebi.ac.uk' in host:
            return httpx2.Response(200, content=json.dumps({'resultList': {'result': []}}).encode())
        raise AssertionError(f'unexpected request: {request.url}')

    resolved = _resolve_batch(
        handler,
        [
            resolve.ResolveRequest(claim_key='hit', pmid=None, doi=_DOI),
            resolve.ResolveRequest(claim_key='miss', pmid=None, doi=_DOI_UNKNOWN),
        ],
    )
    assert set(resolved) == {'hit'}  # the unknown DOI is absent, not a raised failure
    assert _resolved(resolved, 'hit').external_ids.pmid == _PMID  # mapped to its own record, not the other DOI's


def test_resolve_batch_pmid_miss_then_doi_resolves_via_batch() -> None:
    # A request carrying both a PMID (that efetch misses) and a DOI: the PMID miss falls
    # through to the batched DOI path, which resolves it.
    def handler(request: httpx2.Request) -> httpx2.Response:
        path, host = request.url.path, request.url.host
        if 'efetch' in path:
            # The DOI-discovered PMID returns the record; the original (missing) PMID does not.
            if _PMID in request.content.decode() or _PMID in request.url.params.get('id', ''):
                return httpx2.Response(200, content=_EFETCH_XML)
            return httpx2.Response(200, content=b'<PubmedArticleSet></PubmedArticleSet>')
        if 'idconv' in path:
            record = {'doi': _DOI, 'pmid': int(_PMID), 'pmcid': 'PMC5664429'}
            return httpx2.Response(200, content=json.dumps({'records': [record]}).encode())
        if 'openalex' in host:
            return httpx2.Response(200, content=_openalex_json([]))
        if 'ebi.ac.uk' in host:
            return httpx2.Response(200, content=json.dumps({'resultList': {'result': []}}).encode())
        raise AssertionError(f'unexpected request: {request.url}')

    resolved = _resolve_batch(handler, [resolve.ResolveRequest(claim_key='k', pmid='99999999', doi=_DOI)])
    assert set(resolved) == {'k'}
    assert _resolved(resolved, 'k').external_ids.pmid == _PMID  # resolved via the DOI batch after the PMID miss
