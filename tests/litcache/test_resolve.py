"""Tests for the metadata resolver ladder (`themis.litcache.resolve`).

Drives the efetch → Crossref ladder offline with an httpx2 `MockTransport` over the
committed cassettes (`oa/efetch.xml`, `oa/crossref.json`, `oa/openalex.json`, the synthetic
`bookshelf/efetch.xml`), proving each rung, the PMID-miss → DOI fallback, the
fully-unknown fail-loud, and the lookup of OpenAlex's work beside the resolving record,
OpenAlex answering from a fake of its works endpoint (`themis.testing.openalex`). No network.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import pathlib
import traceback
from collections.abc import Callable, Sequence

import httpx2
import litfetch
import pytest

from themis.litcache import efetch, openalex, resolve, sourced_metadata, upstream_record
from themis.litcache.models import litcache_pb2
from themis.testing import openalex as fake_openalex

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_EFETCH_XML = (_FIXTURES / 'oa' / 'efetch.xml').read_bytes()
_BOOK_EFETCH_XML = (_FIXTURES / 'bookshelf' / 'efetch.xml').read_bytes()
_CROSSREF_JSON = (_FIXTURES / 'oa' / 'crossref.json').read_bytes()
_PMID = '29089047'
_BOOK_PMID = '30000010'
_DOI = '10.1186/s13073-017-0482-5'
_DOI_UNKNOWN = '10.9999/unknown'
# OpenAlex's work of the OA paper, the one `_PMID` and `_DOI` name.
_OA_WORK = json.loads((_FIXTURES / 'oa' / 'openalex.json').read_bytes())['results'][0]


def _oa_set_with_accessionless_book() -> bytes:
    """The recorded OA set with the synthetic book record beside it, its accession struck out."""
    start = _BOOK_EFETCH_XML.index(b'<PubmedBookArticle>')
    end = _BOOK_EFETCH_XML.index(b'</PubmedBookArticle>') + len(b'</PubmedBookArticle>')
    book = _BOOK_EFETCH_XML[start:end].replace(b'<ArticleId IdType="bookaccession">NBK900001</ArticleId>', b'')
    return _EFETCH_XML.replace(b'</PubmedArticleSet>', book + b'</PubmedArticleSet>')


_Handler = Callable[[httpx2.Request], httpx2.Response]
_Works = Sequence[dict[str, object]]


def _routed(handler: _Handler, openalex_works: _Works) -> httpx2.MockTransport:
    """`handler` for every index but OpenAlex, which the fake answers from `openalex_works`."""

    def route(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == fake_openalex.HOST:
            return fake_openalex.answer(openalex_works, request)
        return handler(request)

    return httpx2.MockTransport(route)


def _resolve(
    handler: _Handler, *, pmid: str | None, doi: str | None, openalex_works: _Works = ()
) -> resolve.ResolvedPaper:
    async def run() -> resolve.ResolvedPaper:
        async with httpx2.AsyncClient(transport=_routed(handler, openalex_works)) as client:
            return await resolve.resolve_metadata(
                pmid=pmid, doi=doi, http_client=client, openalex_api_key=fake_openalex.API_KEY
            )

    return asyncio.run(run())


def _record(metadata: bytes) -> litcache_pb2.SourcedMetadata:
    return sourced_metadata.parse(metadata)


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
    assert _record(result.metadata).source.pubmed.WhichOneof('kind') == 'article'


def test_pmid_naming_a_book_record_resolves_via_efetch() -> None:
    # A Bookshelf chapter's PMID answers with PubMed's book record: the source record's book arm, and the
    # accession harvested beside the PMID.
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert 'efetch' in request.url.path
        return httpx2.Response(200, content=_BOOK_EFETCH_XML)

    result = _resolve(handler, pmid=_BOOK_PMID, doi=None)
    assert result.publisher is None
    assert result.external_ids == litcache_pb2.ExternalIds(pmid=_BOOK_PMID, bookid='NBK900001')
    assert _record(result.metadata).source.pubmed.WhichOneof('kind') == 'book_article'


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


def _resolve_batch(
    handler: _Handler, requests: list[resolve.ResolveRequest], *, openalex_works: _Works = ()
) -> dict[str, resolve.Outcome]:
    return _run_batch(_routed(handler, openalex_works), requests)


def _run_batch(transport: httpx2.MockTransport, requests: list[resolve.ResolveRequest]) -> dict[str, resolve.Outcome]:
    """Resolve `requests` through the batch, every request answered by `transport`, OpenAlex's included."""

    async def run() -> dict[str, resolve.Outcome]:
        async with (
            httpx2.AsyncClient(transport=transport) as client,
            litfetch.Session(client_factory=functools.partial(httpx2.AsyncClient, transport=transport)) as session,
        ):
            return await resolve.resolve_batch(
                requests, http_client=client, session=session, openalex_api_key=fake_openalex.API_KEY
            )

    return asyncio.run(run())


def _resolved(outcomes: dict[str, resolve.Outcome], join_key: str) -> resolve.ResolvedPaper:
    """The paper under `join_key`, which the test expects resolved."""
    outcome = outcomes[join_key]
    assert isinstance(outcome, resolve.ResolvedPaper), outcome
    return outcome


def test_resolve_batch_rejects_a_join_key_naming_two_identifier_sets() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f'resolved despite the collision: {request.url}')

    requests = [
        resolve.ResolveRequest(join_key='shared', pmid=None, doi=_DOI),
        resolve.ResolveRequest(join_key='shared', pmid=_PMID, doi=_DOI),
    ]
    with pytest.raises(ValueError, match='two identifier sets'):
        _resolve_batch(handler, requests)


def test_resolve_batch_resolves_a_pmid_via_efetch() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert 'efetch' in request.url.path
        return httpx2.Response(200, content=_EFETCH_XML)

    resolved = _resolve_batch(handler, [resolve.ResolveRequest(join_key='k1', pmid=_PMID, doi=_DOI)])
    assert set(resolved) == {'k1'}
    assert _resolved(resolved, 'k1').publisher is None
    assert _resolved(resolved, 'k1').external_ids.pmcid == 'PMC5664429'


def _oa_work(doi: str, *, pmid: str | None, title: str = 'OA Title', work_id: str = 'W1') -> dict[str, object]:
    return {
        'id': f'https://openalex.org/{work_id}',
        'doi': f'https://doi.org/{doi}',
        'title': title,
        'display_name': title,
        'publication_date': '2020-01-01',
        'ids': {'pmid': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}'} if pmid is not None else {},
        'type': 'article' if pmid is not None else 'preprint',
        'primary_location': {'source': {'display_name': 'Jrnl', 'host_organization_name': 'Pub X'}},
    }


def _doi_handler(*, idconv_pmc: bool) -> _Handler:
    """A resolve-batch transport: idconv (PMC hit or error), efetch. No Crossref."""

    def handler(request: httpx2.Request) -> httpx2.Response:
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
    handler = _doi_handler(idconv_pmc=True)
    resolved = _resolve_batch(handler, [resolve.ResolveRequest(join_key='k', pmid=None, doi=_DOI)], openalex_works=[])
    assert set(resolved) == {'k'}
    assert _resolved(resolved, 'k').publisher is None  # efetch path, not OpenAlex
    assert _resolved(resolved, 'k').external_ids.pmid == _PMID
    assert _resolved(resolved, 'k').external_ids.pmcid == 'PMC5664429'


def test_resolve_batch_pubmed_not_pmc_doi_routes_openalex_pmid_into_efetch() -> None:
    # Not in PMC, but in PubMed: OpenAlex supplies the pmid → efetch yields the record.
    handler = _doi_handler(idconv_pmc=False)
    resolved = _resolve_batch(
        handler,
        [resolve.ResolveRequest(join_key='k', pmid=None, doi=_DOI)],
        openalex_works=[_oa_work(_DOI, pmid=_PMID)],
    )
    assert set(resolved) == {'k'}
    assert _resolved(resolved, 'k').publisher is None  # PubMed-native via efetch, not the OpenAlex record
    assert _resolved(resolved, 'k').external_ids.pmid == _PMID


def test_resolve_batch_non_pubmed_doi_uses_the_openalex_record() -> None:
    # No pmid anywhere (a preprint): OpenAlex's own record becomes the metadata.
    handler = _doi_handler(idconv_pmc=False)
    resolved = _resolve_batch(
        handler,
        [resolve.ResolveRequest(join_key='k', pmid=None, doi=_DOI)],
        openalex_works=[_oa_work(_DOI, pmid=None, title='A Preprint')],
    )
    assert set(resolved) == {'k'}
    assert _resolved(resolved, 'k').publisher == 'Pub X'  # from the OpenAlex record
    paper = _record(_resolved(resolved, 'k').metadata)
    assert paper.source.HasField('openalex_json')
    assert not paper.source.HasField('pubmed')
    assert paper.metadata.title == 'A Preprint'


def test_resolve_batch_charges_a_drifted_openalex_record_to_its_paper_alone() -> None:
    # OpenAlex answers with a work whose year, a field the metadata reader reads, holds a value of another
    # shape: the paper is a SchemaDriftFailure naming the field, and the paper beside it resolves.
    drifted = _oa_work(_DOI_UNKNOWN, pmid=None, title='Drifted', work_id='W2') | {'publication_year': 'soon'}
    handler = _doi_handler(idconv_pmc=False)
    works = [_oa_work(_DOI, pmid=None, title='Fine'), drifted]
    resolved = _resolve_batch(
        handler,
        [
            resolve.ResolveRequest(join_key='fine', pmid=None, doi=_DOI),
            resolve.ResolveRequest(join_key='drifted', pmid=None, doi=_DOI_UNKNOWN),
        ],
        openalex_works=works,
    )
    assert set(resolved) == {'fine', 'drifted'}
    assert _record(_resolved(resolved, 'fine').metadata).metadata.title == 'Fine'
    outcome = resolved['drifted']
    assert isinstance(outcome, resolve.SchemaDriftFailure)
    assert 'publication_year' in outcome.reason


def test_resolve_batch_charges_a_record_its_metadata_reader_cannot_read_to_its_paper_alone() -> None:
    # The identifiers read, but the metadata reader needs the `title` key every OpenAlex work states: that
    # paper is a SchemaDriftFailure naming it, and the paper beside it resolves.
    unreadable = {
        k: v for k, v in _oa_work(_DOI_UNKNOWN, pmid=None, title='Unreadable', work_id='W2').items() if k != 'title'
    }
    handler = _doi_handler(idconv_pmc=False)
    works = [_oa_work(_DOI, pmid=None, title='Fine'), unreadable]
    resolved = _resolve_batch(
        handler,
        [
            resolve.ResolveRequest(join_key='fine', pmid=None, doi=_DOI),
            resolve.ResolveRequest(join_key='unreadable', pmid=None, doi=_DOI_UNKNOWN),
        ],
        openalex_works=works,
    )
    assert _record(_resolved(resolved, 'fine').metadata).metadata.title == 'Fine'
    outcome = resolved['unreadable']
    assert isinstance(outcome, resolve.SchemaDriftFailure)
    assert 'title is missing' in outcome.reason


def test_a_drifted_crossref_record_raises_schema_drift_naming_the_key() -> None:
    work = json.loads(_CROSSREF_JSON)
    work['message']['title'] = 'not an array'

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=json.dumps(work).encode())

    with pytest.raises(upstream_record.SchemaDriftError, match='title'):
        _resolve(handler, pmid=None, doi=_DOI)


def test_a_crossref_key_no_reader_reads_is_stored_in_the_text() -> None:
    work = json.loads(_CROSSREF_JSON)
    work['message']['colour'] = 'blue'

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=json.dumps(work).encode())

    sourced = _record(_resolve(handler, pmid=None, doi=_DOI).metadata)
    assert upstream_record.parse_text(sourced.source.crossref_json)['colour'] == 'blue'


def test_resolve_batch_batches_pmids_into_one_efetch_call() -> None:
    efetch_calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal efetch_calls
        assert 'efetch' in request.url.path
        efetch_calls += 1
        return httpx2.Response(200, content=_EFETCH_XML)

    requests = [
        resolve.ResolveRequest(join_key='a', pmid=_PMID, doi=None),
        resolve.ResolveRequest(join_key='b', pmid='11111111', doi=None),
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
        resolve.ResolveRequest(join_key='journal', pmid=_PMID, doi=None),
        resolve.ResolveRequest(join_key='chapter', pmid=_BOOK_PMID, doi='10.1234/synthetic.chapter'),
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
        if 'ebi.ac.uk' in host:
            return httpx2.Response(200, content=json.dumps({'resultList': {'result': []}}).encode())
        raise AssertionError(f'unexpected request: {request.url}')

    outcomes = _resolve_batch(handler, [resolve.ResolveRequest(join_key='k', pmid=None, doi=_DOI)])
    assert isinstance(outcomes['k'], resolve.RecordPreconditionFailure)


def test_resolve_batch_omits_the_unresolvable() -> None:
    # idconv errors (not in PMC) and OpenAlex knows nothing: a full miss, not raised.
    handler = _doi_handler(idconv_pmc=False)
    resolved = _resolve_batch(
        handler, [resolve.ResolveRequest(join_key='gone', pmid=None, doi=_DOI)], openalex_works=[]
    )
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
        if 'efetch' in path:
            return httpx2.Response(200, content=_EFETCH_XML)
        if 'ebi.ac.uk' in host:
            return httpx2.Response(200, content=json.dumps({'resultList': {'result': []}}).encode())
        raise AssertionError(f'unexpected request: {request.url}')

    resolved = _resolve_batch(
        handler,
        [
            resolve.ResolveRequest(join_key='hit', pmid=None, doi=_DOI),
            resolve.ResolveRequest(join_key='miss', pmid=None, doi=_DOI_UNKNOWN),
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
        if 'ebi.ac.uk' in host:
            return httpx2.Response(200, content=json.dumps({'resultList': {'result': []}}).encode())
        raise AssertionError(f'unexpected request: {request.url}')

    resolved = _resolve_batch(handler, [resolve.ResolveRequest(join_key='k', pmid='99999999', doi=_DOI)])
    assert set(resolved) == {'k'}
    assert _resolved(resolved, 'k').external_ids.pmid == _PMID  # resolved via the DOI batch after the PMID miss


def _efetch_or_crossref(request: httpx2.Request) -> httpx2.Response:
    """PubMed answers `_PMID` with the OA paper's record; Crossref answers `_DOI` with the same paper's."""
    if 'efetch' in request.url.path:
        return httpx2.Response(200, content=_EFETCH_XML)
    if request.url.host == 'api.crossref.org':
        return httpx2.Response(200, content=_CROSSREF_JSON)
    raise AssertionError(f'unexpected request: {request.url}')


def _single(*, pmid: str | None, doi: str | None) -> Callable[[_Works], resolve.ResolvedPaper]:
    return lambda works: _resolve(_efetch_or_crossref, pmid=pmid, doi=doi, openalex_works=works)


def _batched(works: _Works) -> resolve.ResolvedPaper:
    outcome = _resolve_batch(
        _efetch_or_crossref, [resolve.ResolveRequest(join_key='k', pmid=_PMID, doi=None)], openalex_works=works
    )['k']
    if isinstance(outcome, resolve.AmbiguousWorkFailure):
        raise openalex.AmbiguousWorkError('(as the batch charged it)', [outcome.reason])
    if isinstance(outcome, resolve.SchemaDriftFailure):
        raise upstream_record.SchemaDriftError(upstream_record.Index.OPENALEX, outcome.reason)
    assert isinstance(outcome, resolve.ResolvedPaper), outcome
    return outcome


# Each writer's path through the ladder, and the record that resolves the paper on it.
_PATHS = [
    pytest.param(_single(pmid=_PMID, doi=None), 'pubmed', id='on-demand-pubmed'),
    pytest.param(_single(pmid=None, doi=_DOI), 'crossref_json', id='on-demand-crossref'),
    pytest.param(_batched, 'pubmed', id='batch-pubmed'),
]
_Path = Callable[[_Works], resolve.ResolvedPaper]


def _work_beside(pmid: str = _PMID, doi: str = _DOI, *, work_id: str = 'W2765366353') -> dict[str, object]:
    """OpenAlex's work of the OA paper, under `work_id`, stating `pmid` and `doi`."""
    return _OA_WORK | {
        'id': f'https://openalex.org/{work_id}',
        'doi': f'https://doi.org/{doi}',
        'ids': {'openalex': f'https://openalex.org/{work_id}', 'pmid': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}'},
    }


@pytest.mark.parametrize(('path', 'resolving'), _PATHS)
def test_openalexs_one_work_of_the_paper_is_stored_beside_the_resolving_record(path: _Path, resolving: str) -> None:
    work = _work_beside()
    paper = _record(path([work]).metadata)

    assert paper.source.HasField(resolving)
    assert upstream_record.parse_text(paper.source.openalex_json) == work
    assert list(paper.metadata.authors) == list(openalex.sourced(openalex.parse_work(work)).metadata.authors)


def _openalex_resolved(works: _Works) -> resolve.ResolvedPaper:
    outcome = _resolve_batch(
        _doi_handler(idconv_pmc=False),
        [resolve.ResolveRequest(join_key='k', pmid=None, doi=_DOI)],
        openalex_works=works,
    )['k']
    assert isinstance(outcome, resolve.ResolvedPaper), outcome
    return outcome


@pytest.mark.parametrize(
    ('path', 'work'),
    [
        *[pytest.param(path, _work_beside(), id=param.id) for param in _PATHS for path in [param.values[0]]],
        pytest.param(_openalex_resolved, _oa_work(_DOI, pmid=None), id='batch-openalex'),
    ],
)
def test_a_work_with_more_authorships_than_a_list_serves_is_stored_whole(path: _Path, work: dict[str, object]) -> None:
    whole = work | {'authorships': fake_openalex.authorships(150)}
    paper = _record(path([whole]).metadata)

    assert upstream_record.parse_text(paper.source.openalex_json) == whole
    assert len(paper.metadata.authors) == 150


@pytest.mark.parametrize(('path', 'resolving'), _PATHS)
def test_a_paper_openalex_holds_no_work_of_is_stored_with_its_resolving_record_alone(
    path: _Path, resolving: str
) -> None:
    unrelated = _work_beside(pmid='1', doi='10.1/unrelated')
    paper = _record(path([unrelated]).metadata)

    assert paper.source.HasField(resolving)
    assert not paper.source.HasField('openalex_json')


@pytest.mark.parametrize(('path', 'resolving'), _PATHS)
def test_a_paper_openalex_holds_two_works_of_is_not_stored_and_names_both(path: _Path, resolving: str) -> None:
    del resolving
    works = [_work_beside(work_id='W1'), _work_beside(work_id='W2')]
    with pytest.raises(openalex.AmbiguousWorkError, match=r'openalex\.org/W1.*openalex\.org/W2'):
        path(works)


@pytest.mark.parametrize(('path', 'resolving'), _PATHS)
def test_a_drifted_openalex_work_of_the_paper_is_charged_to_it_as_drift(path: _Path, resolving: str) -> None:
    del resolving
    with pytest.raises(upstream_record.SchemaDriftError, match=r'authorships\[0\]\.author\.display_name'):
        path([_work_beside() | {'authorships': [{'author': {'display_name': 5}}]}])


# Fields the reader of a paper OpenAlex resolved reads, holding values of another shape.
_DRIFTED_RESOLVING_FIELDS = [
    pytest.param({'ids': {'pmcid': {'drifted': True}}}, 'ids.pmcid', id='pmcid'),
    pytest.param(
        {'primary_location': {'source': {'host_organization_name': 5}}}, 'host_organization_name', id='publisher'
    ),
]


@pytest.mark.parametrize(('path', 'resolving'), _PATHS)
def test_a_work_beside_is_read_for_its_identifier_and_authors_alone(path: _Path, resolving: str) -> None:
    # The identifiers and publisher of a paper OpenAlex resolved, drifted, in a work held beside another record.
    work = _work_beside() | {
        'ids': {
            'openalex': 'https://openalex.org/W2765366353',
            'pmid': f'https://pubmed.ncbi.nlm.nih.gov/{_PMID}',
            'pmcid': {'drifted': True},
        },
        'primary_location': {'source': {'host_organization_name': 5}},
    }
    paper = _record(path([work]).metadata)

    assert paper.source.HasField(resolving)
    assert upstream_record.parse_text(paper.source.openalex_json) == work


@pytest.mark.parametrize(('drift', 'field'), _DRIFTED_RESOLVING_FIELDS)
def test_a_paper_openalex_resolved_is_charged_a_drifted_identifier_or_publisher(
    drift: dict[str, object], field: str
) -> None:
    resolved = _resolve_batch(
        _doi_handler(idconv_pmc=False),
        [resolve.ResolveRequest(join_key='k', pmid=None, doi=_DOI)],
        openalex_works=[_oa_work(_DOI, pmid=None) | drift],
    )
    outcome = resolved['k']
    assert isinstance(outcome, resolve.SchemaDriftFailure), outcome
    assert field in outcome.reason


# The paths on which PubMed resolves the paper, so OpenAlex is asked by its PMID and then its DOI.
_PUBMED_PATHS = [param for param in _PATHS if param.values[1] == 'pubmed']


def _stating_no_pmid(work: dict[str, object]) -> dict[str, object]:
    return work | {'ids': {'openalex': work['id']}}


@pytest.mark.parametrize(('path', 'resolving'), _PUBMED_PATHS)
def test_a_pubmed_paper_openalex_holds_no_work_of_by_pmid_takes_the_work_of_its_doi(
    path: _Path, resolving: str
) -> None:
    work = _stating_no_pmid(_work_beside())
    paper = _record(path([work]).metadata)

    assert paper.source.HasField(resolving)
    assert upstream_record.parse_text(paper.source.openalex_json) == work


@pytest.mark.parametrize(('path', 'resolving'), _PUBMED_PATHS)
def test_a_pubmed_paper_whose_doi_names_two_works_is_not_stored(path: _Path, resolving: str) -> None:
    del resolving
    works = [_stating_no_pmid(_work_beside(work_id='W1')), _stating_no_pmid(_work_beside(work_id='W2'))]
    with pytest.raises(openalex.AmbiguousWorkError, match=r'doi:.*W1.*W2'):
        path(works)


@pytest.mark.parametrize(('path', 'resolving'), _PUBMED_PATHS)
def test_a_work_of_the_doi_whose_pmid_cannot_be_read_is_charged_to_the_paper_as_drift(
    path: _Path, resolving: str
) -> None:
    del resolving
    work = _work_beside() | {'ids': {'openalex': 'https://openalex.org/W2765366353', 'pmid': 'PMID 29089047'}}
    with pytest.raises(upstream_record.SchemaDriftError, match=r'ids\.pmid'):
        path([work])


@pytest.mark.parametrize(('path', 'resolving'), _PUBMED_PATHS)
def test_a_work_of_the_doi_stating_another_pmid_is_not_stored_beside(path: _Path, resolving: str) -> None:
    paper = _record(path([_work_beside(pmid='11111111')]).metadata)

    assert paper.source.HasField(resolving)
    assert not paper.source.HasField('openalex_json')


def _second_paper(efetch_xml: bytes) -> bytes:
    """`efetch_xml` with a copy of its one article under PMID `22222222` and DOI `10.1/second` beside it."""
    start, end = efetch_xml.index(b'<PubmedArticle>'), efetch_xml.index(b'</PubmedArticleSet>')
    copy = efetch_xml[start:end].replace(_PMID.encode(), b'22222222').replace(_DOI.encode(), b'10.1/second')
    return efetch_xml[:end] + copy + efetch_xml[end:]


def test_resolve_batch_asks_every_doi_after_a_pmid_miss_in_one_openalex_call() -> None:
    asked: list[str] = []
    works = [_stating_no_pmid(_work_beside()), _stating_no_pmid(_work_beside(doi='10.1/second', work_id='W2'))]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == fake_openalex.HOST:
            asked.append(request.url.params['filter'])
            return fake_openalex.answer(works, request)
        return httpx2.Response(200, content=_second_paper(_EFETCH_XML))

    outcomes = _run_batch(
        httpx2.MockTransport(handler),
        [resolve.ResolveRequest(join_key=p, pmid=p, doi=None) for p in (_PMID, '22222222')],
    )
    assert asked == [f'pmid:22222222|{_PMID}', f'doi:10.1/second|{_DOI}']
    assert all(_record(_resolved(outcomes, p).metadata).source.HasField('openalex_json') for p in (_PMID, '22222222'))


@pytest.mark.parametrize(
    ('path', 'pmid', 'doi'),
    [
        pytest.param('single', _PMID, None, id='on-demand-pubmed'),
        pytest.param('single', None, _DOI, id='on-demand-crossref'),
        pytest.param('batch', _PMID, None, id='batch-pubmed'),
    ],
)
@pytest.mark.parametrize(
    'outage',
    [httpx2.Response(429), httpx2.Response(503), httpx2.ConnectError('unreachable')],
    ids=['rate-limited', 'server-error', 'unreachable'],
)
@pytest.mark.parametrize('doi_only', [False, True], ids=['every-lookup', 'the-doi-lookup'])
def test_openalex_unreachable_fails_the_write_as_the_resolving_index_would(
    path: str,
    pmid: str | None,
    doi: str | None,
    outage: httpx2.Response | httpx2.ConnectError,
    doi_only: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openalex, '_RETRY_BASE_DELAY_SECONDS', 0)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == fake_openalex.HOST:
            if doi_only and not request.url.params['filter'].startswith('doi:'):
                return fake_openalex.answer([], request)  # OpenAlex holds no work for the PMID; its DOI is asked next
            if isinstance(outage, Exception):
                raise outage
            return outage
        return _efetch_or_crossref(request)

    async def run() -> object:
        transport = httpx2.MockTransport(handler)
        async with (
            httpx2.AsyncClient(transport=transport) as client,
            litfetch.Session(client_factory=functools.partial(httpx2.AsyncClient, transport=transport)) as session,
        ):
            if path == 'batch':
                requests = [resolve.ResolveRequest(join_key='k', pmid=pmid, doi=doi)]
                return await resolve.resolve_batch(
                    requests, http_client=client, session=session, openalex_api_key=fake_openalex.API_KEY
                )
            return await resolve.resolve_metadata(
                pmid=pmid, doi=doi, http_client=client, openalex_api_key=fake_openalex.API_KEY
            )

    with pytest.raises(httpx2.HTTPError):
        asyncio.run(run())


def test_resolve_batch_looks_up_every_pubmed_paper_in_one_openalex_call() -> None:
    asked: list[str] = []
    works = [_work_beside(), _work_beside(pmid='11111111', doi='10.1/other', work_id='W2')]

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == fake_openalex.HOST:
            asked.append(request.url.params['filter'])
            return fake_openalex.answer(works, request)
        return httpx2.Response(200, content=_EFETCH_XML)

    async def run() -> dict[str, resolve.Outcome]:
        transport = httpx2.MockTransport(handler)
        async with (
            httpx2.AsyncClient(transport=transport) as client,
            litfetch.Session(client_factory=functools.partial(httpx2.AsyncClient, transport=transport)) as session,
        ):
            requests = [resolve.ResolveRequest(join_key=p, pmid=p, doi=None) for p in (_PMID, '22222222')]
            return await resolve.resolve_batch(
                requests, http_client=client, session=session, openalex_api_key=fake_openalex.API_KEY
            )

    outcomes = asyncio.run(run())
    assert asked == [f'pmid:{_PMID}']  # one call, for the PMIDs PubMed resolved; '22222222' it did not
    assert _record(_resolved(outcomes, _PMID).metadata).source.HasField('openalex_json')


def _litfetch_abandons(
    works: _Works,
    *,
    efetch_xml: bytes = _EFETCH_XML,
    doi: str = _DOI,
    asked: list[str] | None = None,
    litfetch_keys: list[str | None] | None = None,
) -> dict[str, resolve.Outcome]:
    """Resolve `doi` through the batch, litfetch abandoning it: its OpenAlex lookup is refused (429).

    Args:
        works: The works OpenAlex holds.
        efetch_xml: What efetch answers.
        doi: The DOI resolved.
        asked: Where the filter of each of Themis's own OpenAlex requests is recorded, when given.
        litfetch_keys: Where the `api_key` of each of litfetch's OpenAlex requests is recorded, when given.
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == fake_openalex.HOST:
            if asked is not None:
                asked.append(request.url.params['filter'])
            return fake_openalex.answer(works, request)
        if 'efetch' in request.url.path:
            return httpx2.Response(200, content=efetch_xml)
        return _doi_handler(idconv_pmc=False)(request)

    def litfetch_handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == fake_openalex.HOST:
            if litfetch_keys is not None:
                litfetch_keys.append(request.url.params.get('api_key'))
            return httpx2.Response(429)
        return handler(request)

    async def run() -> dict[str, resolve.Outcome]:
        transport = httpx2.MockTransport(handler)
        async with (
            httpx2.AsyncClient(transport=transport) as client,
            litfetch.Session(
                client_factory=functools.partial(httpx2.AsyncClient, transport=httpx2.MockTransport(litfetch_handler)),
                retry=litfetch.RetryPolicy(max_attempts=1),
            ) as session,
        ):
            requests = [resolve.ResolveRequest(join_key='k', pmid=None, doi=doi)]
            return await resolve.resolve_batch(
                requests, http_client=client, session=session, openalex_api_key=fake_openalex.API_KEY
            )

    return asyncio.run(run())


def test_litfetch_asks_openalex_with_the_key() -> None:
    keys: list[str | None] = []
    _litfetch_abandons([_work_beside()], litfetch_keys=keys)

    assert keys  # rules out a vacuous pass: litfetch did ask OpenAlex
    assert set(keys) == {fake_openalex.API_KEY.value}


def _texts_reachable_from(error: BaseException) -> list[str]:
    """Every string reachable from `error`: through its cause and context, and the request and response it carries."""
    texts: list[str] = []
    pending: list[BaseException | None] = [error]
    seen: list[BaseException] = []
    while pending:
        current = pending.pop()
        if current is None or any(current is s for s in seen):
            continue
        seen.append(current)
        texts += [str(current), repr(current), ''.join(traceback.format_exception(current, chain=False))]
        if isinstance(current, httpx2.HTTPStatusError):
            response = current.response
            texts += [str(current.request.url), str(response.url), str(response.headers.multi_items()), response.text]
        elif isinstance(current, httpx2.RequestError):
            with contextlib.suppress(RuntimeError):  # httpx2 raises this for a `.request` never set
                texts.append(str(current.request.url))
        pending += [current.__cause__, current.__context__]
    return texts


@pytest.mark.parametrize(
    'litfetch_answer',
    [httpx2.Response(401), httpx2.Response(503), httpx2.ConnectError('unreachable')],
    ids=['key-refused', 'server-error', 'unreachable'],
)
@pytest.mark.parametrize('themis_answers', [True, False], ids=['themis-answered', 'themis-refused'])
def test_litfetch_failing_with_the_key_leaks_it_nowhere(
    litfetch_answer: httpx2.Response | httpx2.ConnectError,
    themis_answers: bool,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openalex, '_RETRY_BASE_DELAY_SECONDS', 0)
    key = fake_openalex.API_KEY.value
    litfetch_asked: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == fake_openalex.HOST:
            if themis_answers:
                return fake_openalex.answer([_work_beside()], request)
            return httpx2.Response(401, text=f'refused {request.url}')
        if 'efetch' in request.url.path:
            return httpx2.Response(200, content=_EFETCH_XML)
        return _doi_handler(idconv_pmc=False)(request)

    def litfetch_handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host != fake_openalex.HOST:
            return handler(request)
        litfetch_asked.append(request)
        if isinstance(litfetch_answer, httpx2.ConnectError):
            raise httpx2.ConnectError(f'cannot reach {request.url}', request=request)
        # The body echoes the URL, as an error page may.
        return httpx2.Response(litfetch_answer.status_code, text=f'failed {request.url}')

    async def run() -> dict[str, resolve.Outcome]:
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client,
            litfetch.Session(
                client_factory=functools.partial(httpx2.AsyncClient, transport=httpx2.MockTransport(litfetch_handler)),
                retry=litfetch.RetryPolicy(max_attempts=1),
            ) as session,
        ):
            requests = [resolve.ResolveRequest(join_key='k', pmid=None, doi=_DOI)]
            return await resolve.resolve_batch(
                requests, http_client=client, session=session, openalex_api_key=fake_openalex.API_KEY
            )

    with caplog.at_level(logging.DEBUG):
        try:
            reachable = [repr(asyncio.run(run()))]
        except Exception as e:  # noqa: BLE001 -- whatever escapes is searched for the key
            reachable = _texts_reachable_from(e)

    assert litfetch_asked, 'litfetch asked OpenAlex'  # rules out a vacuous pass
    assert all(key in str(request.url) for request in litfetch_asked)
    assert not any(key in text for text in reachable)
    assert caplog.records, 'something was logged'  # rules out a vacuous pass
    assert not any(key in record.getMessage() or key in (record.exc_text or '') for record in caplog.records)
    assert key not in caplog.text


def test_a_doi_litfetch_abandoned_whose_openalex_work_states_a_pmid_resolves_through_pubmed() -> None:
    work = _work_beside()
    asked: list[str] = []
    paper = _resolved(_litfetch_abandons([work], asked=asked), 'k')

    source = _record(paper.metadata).source
    assert source.HasField('pubmed')
    assert upstream_record.parse_text(source.openalex_json) == work  # the work that sent it to PubMed
    assert paper.external_ids.pmid == _PMID
    assert asked == [f'doi:{_DOI}']  # that work is not looked up again by the PMID


_PUBMED_DOI_ELEMENT = f'<ArticleId IdType="doi">{_DOI}</ArticleId>'.encode()


@pytest.mark.parametrize(
    ('efetch_xml', 'doi'),
    [
        pytest.param(_EFETCH_XML.replace(_PUBMED_DOI_ELEMENT, b''), _DOI, id='pubmed-states-no-doi'),
        pytest.param(_EFETCH_XML, _DOI.upper(), id='pubmed-states-the-doi-in-another-case'),
    ],
)
def test_a_pmid_routes_the_doi_to_pubmed_when_pubmeds_record_states_no_other_doi(efetch_xml: bytes, doi: str) -> None:
    paper = _resolved(_litfetch_abandons([_work_beside()], efetch_xml=efetch_xml, doi=doi), 'k')

    assert _record(paper.metadata).source.HasField('pubmed')
    assert paper.external_ids.pmid == _PMID


def test_a_pmid_whose_pubmed_record_states_another_doi_leaves_the_doi_openalex_resolved() -> None:
    efetch_xml = _EFETCH_XML.replace(_PUBMED_DOI_ELEMENT, b'<ArticleId IdType="doi">10.1/another</ArticleId>')
    work = _work_beside()
    paper = _resolved(_litfetch_abandons([work], efetch_xml=efetch_xml), 'k')

    source = _record(paper.metadata).source
    assert not source.HasField('pubmed')
    assert upstream_record.parse_text(source.openalex_json) == work
    assert not paper.external_ids.pmid
    assert paper.external_ids.doi == _DOI


def test_a_doi_whose_openalex_work_states_a_pmid_pubmed_does_not_answer_is_not_resolved() -> None:
    empty = b'<PubmedArticleSet></PubmedArticleSet>'
    assert _litfetch_abandons([_work_beside()], efetch_xml=empty) == {}


def test_a_doi_litfetch_abandoned_whose_openalex_work_states_no_pmid_is_openalex_resolved() -> None:
    work = _oa_work(_DOI, pmid=None, title='A Preprint')
    paper = _resolved(_litfetch_abandons([work]), 'k')

    source = _record(paper.metadata).source
    assert not source.HasField('pubmed')
    assert upstream_record.parse_text(source.openalex_json) == work


def test_a_paper_openalex_resolved_holds_its_record_alone() -> None:
    handler = _doi_handler(idconv_pmc=False)
    resolved = _resolve_batch(
        handler,
        [resolve.ResolveRequest(join_key='k', pmid=None, doi=_DOI)],
        openalex_works=[_oa_work(_DOI, pmid=None, title='A Preprint')],
    )
    source = _record(_resolved(resolved, 'k').metadata).source
    assert source.HasField('openalex_json')
    assert not source.HasField('pubmed')
    assert not source.HasField('crossref_json')


_MERGED_PREPRINT_DOI = '10.1101/2017.01.01.000001'


def _merged_into_the_oa_paper(preprint: str) -> dict[str, object]:
    """The OA paper's work as OpenAlex merges a preprint into it: the preprint's DOI on a location alone."""
    work = _work_beside()
    ids = work['ids']
    assert isinstance(ids, dict)
    return work | {
        'ids': ids | {'pmcid': 'https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5664429'},
        'locations': [{'doi': f'https://doi.org/{_DOI}'}, {'doi': f'https://doi.org/{preprint}'}],
    }


@pytest.mark.parametrize(
    'efetch_xml',
    [
        pytest.param(_EFETCH_XML, id='pubmed-states-the-published-doi'),
        pytest.param(_EFETCH_XML.replace(_PUBMED_DOI_ELEMENT, b''), id='pubmed-states-no-doi'),
    ],
)
def test_a_preprint_merged_into_its_published_version_is_openalex_resolved_taking_only_its_record(
    efetch_xml: bytes,
) -> None:
    work = _merged_into_the_oa_paper(_MERGED_PREPRINT_DOI)
    paper = _resolved(_litfetch_abandons([work], efetch_xml=efetch_xml, doi=_MERGED_PREPRINT_DOI), 'k')

    source = _record(paper.metadata).source
    assert not source.HasField('pubmed')  # the published version's PMID did not route it to PubMed
    assert upstream_record.parse_text(source.openalex_json) == work
    assert paper.external_ids == litcache_pb2.ExternalIds(doi=_MERGED_PREPRINT_DOI)
    assert paper.publisher is None  # the published version's would license the preprint under its journal


@pytest.mark.parametrize(('path', 'resolving'), _PATHS)
def test_a_work_stating_the_doi_only_on_a_location_is_stored_beside_for_its_authors(
    path: _Path, resolving: str
) -> None:
    published = _work_beside(pmid='11111111', doi='10.1/published-version')
    work = published | {'locations': [{'doi': f'https://doi.org/{_DOI}'}]}
    paper = _record(path([work]).metadata)

    assert paper.source.HasField(resolving)
    assert upstream_record.parse_text(paper.source.openalex_json) == work
    assert list(paper.metadata.authors) == list(openalex.sourced(openalex.parse_work(work)).metadata.authors)
