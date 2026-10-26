"""OpenAlex: the batched lookup by PMID or DOI, and the work record kept as text with its identifiers read.

Synthetic works against a fake works endpoint exercise the lookup (each identifier's one work, none, more than one, or
one that drifted; paging, chunking, and an outage);
committed OpenAlex responses — the OA paper's and a bioRxiv preprint's (OpenAlex data is CC0) —
exercise the record half. Live OpenAlex is integration-gated: `LITCACHE_OPENALEX_LIVE_DOI`
resolves one DOI, and `LITCACHE_OPENALEX_LIVE_SAMPLE` reads that many random works without drift,
into text that parses back to the work served — where a field Themis reads changing shape shows
first. The live gates send `LITCACHE_OPENALEX_LIVE_API_KEY` as the API key, and skip when it is unset.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import json
import logging
import os
import pathlib
import random
import re
import traceback
from collections.abc import Callable

import httpx2
import litfetch
import pytest

from themis.common import constants
from themis.litcache import openalex, resolve, sourced_metadata, upstream_record
from themis.litcache.models import litcache_pb2
from themis.testing import openalex as fake_openalex

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_DOI = '10.1186/s13073-017-0482-5'
_PREPRINT_DOI = '10.1101/2024.09.14.613029'
# Markup left in a plain-text field: an element with its start and end tag. A lone tag-shaped run can be text
# (`<x> carriers`, `</CD4>`), and markup the fill does not know still closes what it opens.
_TAG = re.compile(r'<([A-Za-z][\w.:-]*)(?:\s[^<>]*)?>.*?</\1\s*>', re.DOTALL | re.IGNORECASE)
# An ORCID iD as a record states it, captured in its bare form; and the NLM names the type table maps OpenAlex's onto.
_STATED_ORCID = re.compile(r'\s*(?:https?://orcid\.org/)?([0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9Xx])\s*')
_MAPPED_TYPES = frozenset({'Journal Article', 'Review', 'Preprint', 'Letter', 'Published Erratum', 'Retraction Notice'})
_DOI_A = '10.1/a'
_DOI_B = '10.1/b'


def _work(doi: str, **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        'id': f'https://openalex.org/{doi}',
        'doi': f'https://doi.org/{doi}',
        'title': 'Title A',
        'display_name': 'Title A',
        'publication_date': '2020-01-15',
        'ids': {'pmid': 'https://pubmed.ncbi.nlm.nih.gov/111'},
        'type': 'article',
        'primary_location': {'source': {'display_name': 'Journal A', 'host_organization_name': 'Pub A'}},
    }
    record.update(overrides)
    return record


def _fixture_work(fixture: str) -> dict[str, object]:
    return json.loads((_FIXTURES / fixture / 'openalex.json').read_bytes())['results'][0]


def _find(
    scheme: openalex.Scheme,
    identifiers: list[str],
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> openalex.Found:
    async def run() -> openalex.Found:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await openalex.find(scheme, identifiers, http_client=client, api_key=fake_openalex.API_KEY)

    return asyncio.run(run())


def _holding(*works: dict[str, object]) -> Callable[[httpx2.Request], httpx2.Response]:
    return functools.partial(fake_openalex.answer, works)


def test_find_reads_bare_ids_off_their_url_forms() -> None:
    works = [
        _work(
            _DOI_A,
            ids={
                'pmid': 'https://pubmed.ncbi.nlm.nih.gov/111',
                'pmcid': 'https://www.ncbi.nlm.nih.gov/pmc/articles/PMC222',
            },
        ),
        _work(
            _DOI_B,
            title='Preprint B',
            ids={},
            type='preprint',
            primary_location={'source': {'display_name': 'medRxiv'}},
        ),
    ]
    found = _find(openalex.Scheme.DOI, [_DOI_A, _DOI_B], _holding(*works))
    work_a, work_b = found.answer(_DOI_A), found.answer(_DOI_B)

    assert work_a is not None
    assert openalex.read_answer(work_a) == openalex.WorkFields(pmid='111', pmcid='PMC222', publisher='Pub A')
    assert work_b is not None
    assert openalex.read_answer(work_b) == openalex.WorkFields(pmid=None, pmcid=None, publisher=None)


@pytest.mark.parametrize(
    'url', ['https://www.ncbi.nlm.nih.gov/pmc/articles/222', 'https://www.ncbi.nlm.nih.gov/pmc/articles/PMC222/']
)
def test_a_pmcid_url_reads_in_its_canonical_form(url: str) -> None:
    assert openalex.read_work(_work(_DOI_A, ids={'pmcid': url})).pmcid == 'PMC222'


# Each scheme's two identifiers, and the work OpenAlex holds stating the first.
_BY_SCHEME = [
    pytest.param(openalex.Scheme.PMID, '111', '222', {'pmid': '111'}, id='pmid'),
    pytest.param(openalex.Scheme.DOI, _DOI_A, _DOI_B, {'doi': _DOI_A}, id='doi'),
]


@pytest.mark.parametrize(('scheme', 'held', 'unheld', 'stating'), _BY_SCHEME)
def test_find_answers_an_identifier_with_its_one_work_and_another_with_none(
    scheme: openalex.Scheme, held: str, unheld: str, stating: dict[str, str]
) -> None:
    source = fake_openalex.work(work_id='W1', **stating)
    found = _find(scheme, [held, unheld], _holding(source))

    answer = found.answer(held)
    assert answer is not None
    assert upstream_record.parse_text(answer.work.text) == source
    assert found.answer(unheld) is None


@pytest.mark.parametrize(('scheme', 'held', 'unheld', 'stating'), _BY_SCHEME)
def test_find_names_every_work_an_identifier_has_more_than_one_of(
    scheme: openalex.Scheme, held: str, unheld: str, stating: dict[str, str]
) -> None:
    other = {'pmid': unheld} if scheme is openalex.Scheme.PMID else {'doi': unheld}
    works = [fake_openalex.work(work_id=w, **stating) for w in ('W1', 'W2')]
    found = _find(scheme, [held, unheld], _holding(*works, fake_openalex.work(work_id='W3', **other)))

    with pytest.raises(openalex.AmbiguousWorkError, match=r'W1.*W2') as excinfo:
        found.answer(held)
    assert excinfo.value.work_ids == ('https://openalex.org/W1', 'https://openalex.org/W2')
    assert found.answer(unheld) is not None  # charged to its identifier alone


@pytest.mark.parametrize(('scheme', 'held', 'unheld', 'stating'), _BY_SCHEME)
def test_a_work_that_cannot_be_stored_is_charged_to_its_identifier_alone(
    scheme: openalex.Scheme, held: str, unheld: str, stating: dict[str, str]
) -> None:
    other = {'pmid': unheld} if scheme is openalex.Scheme.PMID else {'doi': unheld}
    drifted = fake_openalex.work(work_id='W1', fwci=float('nan'), **stating)
    found = _find(scheme, [held, unheld], _holding(drifted, fake_openalex.work(work_id='W2', **other)))

    with pytest.raises(upstream_record.SchemaDriftError, match='fwci'):
        found.answer(held)
    assert found.answer(unheld) is not None


def test_find_matches_a_doi_whatever_its_case() -> None:
    found = _find(openalex.Scheme.DOI, ['10.1/ABC'], _holding(fake_openalex.work(work_id='W1', doi='10.1/abc')))
    assert found.answer('10.1/AbC') is not None


_COMMA_DOI = '10.1/a,b'
_PIPE_DOI = '10.1/c|d'


@pytest.mark.parametrize(('held', 'unheld'), [(_COMMA_DOI, _PIPE_DOI), (_PIPE_DOI, _COMMA_DOI)])
def test_find_looks_up_a_doi_the_filter_would_split_on_its_own(held: str, unheld: str) -> None:
    alone = fake_openalex.work(work_id='W1', doi=held, authorships=fake_openalex.authorships(150))
    batched = fake_openalex.work(work_id='W2', doi=_DOI_A)
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return fake_openalex.answer([alone, batched], request)

    found = _find(openalex.Scheme.DOI, [_DOI_A, held.upper(), unheld, _DOI_B], handler)
    answer = found.answer(held)
    assert answer is not None
    assert (
        upstream_record.parse_text(answer.work.text) == alone
    )  # every authorship: a lookup alone serves the work whole
    assert found.holds_none(unheld)
    assert found.answer(_DOI_A) is not None
    assert found.holds_none(_DOI_B)
    filters = [request.url.params['filter'] for request in sent if request.url.path == '/works']
    assert filters, 'the ordinary DOIs were batched'  # rules out a vacuous pass
    assert not any(doi in f.casefold() for f in filters for doi in (held, unheld))
    assert [request.url.params.get('api_key') for request in sent] == [fake_openalex.API_KEY.value] * len(sent)


def test_a_doi_looked_up_alone_stays_one_segment_under_works_doi() -> None:
    doi = '10.1/a,b/../../../authors/A1'
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return fake_openalex.answer([fake_openalex.work(work_id='W1', doi=doi)], request)

    found = _find(openalex.Scheme.DOI, [doi], handler)
    assert found.answer(doi) is not None
    [request] = sent
    assert request.url.raw_path.partition(b'?')[0] == b'/works/doi:10.1%2Fa%2Cb%2F..%2F..%2F..%2Fauthors%2FA1'


def test_find_fails_when_a_doi_looked_up_alone_answers_with_another_work() -> None:
    other = fake_openalex.work(work_id='W1', doi=_DOI_A)
    with pytest.raises(ValueError, match='none of the identifiers asked'):
        _find(openalex.Scheme.DOI, [_COMMA_DOI], lambda _: httpx2.Response(200, json=other))


def test_find_propagates_an_outage_looking_up_a_doi_alone_without_showing_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openalex, '_RETRY_BASE_DELAY_SECONDS', 0)
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(503)

    with pytest.raises(httpx2.HTTPStatusError) as caught:
        _find(openalex.Scheme.DOI, [_COMMA_DOI], handler)
    assert len(calls) == openalex._MAX_FETCH_ATTEMPTS
    assert not _holds_the_key(caught.value)


@pytest.mark.parametrize('doi', [_COMMA_DOI, _PIPE_DOI])
def test_fetch_page_refuses_an_identifier_the_filter_would_split(doi: str) -> None:
    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(_holding())) as client:
            await openalex.fetch_page(
                openalex.Scheme.DOI, [_DOI_A, doi], 1, http_client=client, api_key=fake_openalex.API_KEY
            )

    with pytest.raises(ValueError, match='splits on'):
        asyncio.run(run())


def test_find_reads_every_page_of_a_long_answer() -> None:
    works = [fake_openalex.work(work_id=f'W{i}', pmid='111') for i in range(150)]
    found = _find(openalex.Scheme.PMID, ['111'], _holding(*works))

    with pytest.raises(openalex.AmbiguousWorkError) as excinfo:
        found.answer('111')
    assert len(excinfo.value.work_ids) == len(works)


def test_find_chunks_a_batch_over_the_cap() -> None:
    asked: list[int] = []
    pmids = [str(i) for i in range(1, 151)]

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.append(len(request.url.params['filter'].removeprefix('pmid:').split('|')))
        return fake_openalex.answer([fake_openalex.work(work_id=f'W{p}', pmid=p) for p in pmids], request)

    found = _find(openalex.Scheme.PMID, pmids, handler)
    assert sorted(asked) == [50, 100]
    assert all(found.answer(pmid) is not None for pmid in pmids)


def test_fetch_page_rejects_a_batch_over_the_cap() -> None:
    async def run() -> None:
        async with httpx2.AsyncClient() as client:
            await openalex.fetch_page(
                openalex.Scheme.DOI,
                [f'10.1/{i}' for i in range(101)],
                1,
                http_client=client,
                api_key=fake_openalex.API_KEY,
            )

    with pytest.raises(ValueError, match='caps at 100'):
        asyncio.run(run())


@pytest.mark.parametrize('count', [99, 100, 150])
def test_find_returns_every_authorship_of_a_work_a_list_cuts_short(count: int) -> None:
    source = fake_openalex.work(work_id='W1', pmid='111', authorships=fake_openalex.authorships(count))
    sent: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return fake_openalex.answer([source], request)

    answer = _find(openalex.Scheme.PMID, ['111'], handler).answer('111')
    assert answer is not None
    assert upstream_record.parse_text(answer.work.text) == source
    assert len(sent) == 1 + (count >= fake_openalex.LISTED_AUTHORSHIPS)  # the list, and a re-fetch only when cut
    assert [request.url.params.get('api_key') for request in sent] == [fake_openalex.API_KEY.value] * len(sent)


def test_find_propagates_an_outage_refetching_a_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openalex, '_RETRY_BASE_DELAY_SECONDS', 0)
    source = fake_openalex.work(work_id='W1', pmid='111', authorships=fake_openalex.authorships(150))

    def handler(request: httpx2.Request) -> httpx2.Response:
        return fake_openalex.answer([source], request) if request.url.path == '/works' else httpx2.Response(503)

    with pytest.raises(httpx2.HTTPStatusError):
        _find(openalex.Scheme.PMID, ['111'], handler)


def test_find_fails_when_a_refetch_answers_with_another_work() -> None:
    source = fake_openalex.work(work_id='W1', pmid='111', authorships=fake_openalex.authorships(150))
    other = fake_openalex.work(work_id='W2', pmid='111', authorships=fake_openalex.authorships(150))

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == '/works':
            return fake_openalex.answer([source], request)
        return httpx2.Response(200, json=other)

    with pytest.raises(ValueError, match='something other than that work'):
        _find(openalex.Scheme.PMID, ['111'], handler)


def _sici_doi(n: int) -> str:
    """A SICI-style DOI, about 60 characters, a dozen of them percent-encoded in a URL."""
    return f'10.1002/(SICI)1097-4636(199706)35:4<513::AID-JBM{n}>3.0.CO;2-L'


def test_find_chunks_long_dois_to_urls_openalex_accepts() -> None:
    dois = [_sici_doi(n) for n in range(100)]
    sent: list[httpx2.Request] = []
    works = [fake_openalex.work(work_id=f'W{n}', doi=doi) for n, doi in enumerate(dois)]

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return fake_openalex.answer(works, request)

    found = _find(openalex.Scheme.DOI, dois, handler)
    assert len(sent) > 1, 'one request would carry a URL OpenAlex refuses'  # rules out a vacuous pass
    assert all(found.answer(doi) is not None for doi in dois)


def test_fetch_page_refuses_a_url_openalex_would_refuse() -> None:
    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(_holding())) as client:
            await openalex.fetch_page(
                openalex.Scheme.DOI, ['10.1/' + 'x' * 9000], 1, http_client=client, api_key=fake_openalex.API_KEY
            )

    with pytest.raises(ValueError, match='OpenAlex refuses one over'):
        asyncio.run(run())


def test_find_fails_on_a_work_stating_none_of_the_identifiers_asked() -> None:
    with pytest.raises(ValueError, match='none of the identifiers asked'):
        _find(openalex.Scheme.PMID, ['111'], lambda _: _page([fake_openalex.work(work_id='W1', pmid='999')]))


def test_find_fails_when_the_pages_run_out_before_the_count() -> None:
    with pytest.raises(ValueError, match='counts 5 works'):
        _find(openalex.Scheme.PMID, ['111'], lambda _: _page([], matched=5))


@pytest.mark.parametrize(
    ('payload', 'match'),
    [
        ({'meta': {'count': 0}}, 'results'),
        ({'results': []}, 'meta.count'),
        ({'meta': {'count': 1}, 'results': [5]}, 'object'),
    ],
)
def test_parse_page_rejects_a_non_works_payload(payload: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        openalex.parse_page(json.dumps(payload).encode())


@pytest.mark.parametrize(
    'outage',
    [httpx2.Response(429), httpx2.Response(503), httpx2.ConnectError('unreachable')],
    ids=['rate-limited', 'server-error', 'unreachable'],
)
def test_find_propagates_an_outage_past_its_retries(
    outage: httpx2.Response | httpx2.ConnectError, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openalex, '_RETRY_BASE_DELAY_SECONDS', 0)
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        if isinstance(outage, Exception):
            raise outage
        return outage

    with pytest.raises(httpx2.HTTPError):
        _find(openalex.Scheme.PMID, ['111'], handler)
    assert len(calls) == openalex._MAX_FETCH_ATTEMPTS


def test_find_retries_a_transient_failure_then_reads_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openalex, '_RETRY_BASE_DELAY_SECONDS', 0)
    answers = iter([httpx2.Response(503), httpx2.Response(429)])
    held = _holding(fake_openalex.work(work_id='W1', pmid='111'))

    found = _find(openalex.Scheme.PMID, ['111'], lambda request: next(answers, None) or held(request))
    assert found.answer('111') is not None


def test_find_does_not_retry_a_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openalex, '_RETRY_BASE_DELAY_SECONDS', 0)
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(400)

    with pytest.raises(httpx2.HTTPStatusError):
        _find(openalex.Scheme.PMID, ['111'], handler)
    assert len(calls) == 1


def test_every_request_carries_the_api_key() -> None:
    sent: list[str | None] = []
    pmids = [str(i) for i in range(1, 151)]
    works = [fake_openalex.work(work_id=f'W{p}-{n}', pmid=p) for p in pmids[:2] for n in range(60)]

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.url.params.get('api_key'))
        return fake_openalex.answer(works, request)

    _find(openalex.Scheme.PMID, pmids, handler)
    assert len(sent) > 2, 'chunked and paged: more than one request'  # rules out a vacuous pass
    assert sent == [fake_openalex.API_KEY.value] * len(sent)


def _holds_the_key(error: BaseException) -> bool:
    """Whether the key is reachable from `error`: through its cause and context, or a local of an `openalex` frame."""
    key = fake_openalex.API_KEY.value
    pending: list[BaseException | None] = [error]
    seen: list[BaseException] = []
    while pending:
        current = pending.pop()
        if current is None or any(current is s for s in seen):
            continue
        seen.append(current)
        if key in str(current) or key in ''.join(traceback.format_exception(current, chain=False)):
            return True
        if isinstance(current, httpx2.HTTPStatusError):
            response = current.response
            if (
                key
                in str(current.request.url) + str(response.url) + str(response.headers.multi_items()) + response.text
            ):
                return True
        for frame, _ in traceback.walk_tb(current.__traceback__):
            if frame.f_code.co_filename == openalex.__file__ and key in repr(dict(frame.f_locals)):
                return True
        pending += [current.__cause__, current.__context__]
    return False


@pytest.mark.parametrize(
    'failure',
    [
        httpx2.Response(403, text='forbidden'),
        httpx2.Response(503),
        httpx2.Response(503, headers={'content-encoding': 'gzip'}, content=gzip.compress(b'busy')),
        httpx2.Response(
            301,
            headers={'location': f'https://api.openalex.org/works?api_key={fake_openalex.API_KEY.value}&page=1'},
            json={'error': 'moved', 'query': {'api_key': fake_openalex.API_KEY.value}},
        ),
    ],
    ids=['client-error', 'past-retries', 'compressed-body', 'redirect-echoing-the-key'],
)
def test_a_status_failure_does_not_show_the_key(failure: httpx2.Response, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openalex, '_RETRY_BASE_DELAY_SECONDS', 0)
    with pytest.raises(httpx2.HTTPStatusError) as caught:
        _find(openalex.Scheme.PMID, ['111'], lambda _: failure)
    error = caught.value
    assert 'api_key=REDACTED' in str(error)  # the URL is still there to read, its key replaced
    assert not _holds_the_key(error)
    assert fake_openalex.API_KEY.value not in str(error.request.url)
    assert fake_openalex.API_KEY.value not in str(error.response.request.url)
    assert error.response.status_code == failure.status_code


def test_a_transport_failure_quoting_the_url_does_not_show_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openalex, '_RETRY_BASE_DELAY_SECONDS', 0)

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f'cannot reach {request.url}')

    with pytest.raises(httpx2.ConnectError) as caught:
        _find(openalex.Scheme.PMID, ['111'], handler)
    assert 'api_key=REDACTED' in str(caught.value)
    assert not _holds_the_key(caught.value)
    assert fake_openalex.API_KEY.value not in str(caught.value.request.url)


def test_httpx2s_request_log_does_not_show_the_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger='httpx2'):
        _find(openalex.Scheme.PMID, ['111'], _holding(fake_openalex.work(work_id='W1', pmid='111')))
    logged = [record.getMessage() for record in caplog.records if record.name == 'httpx2']
    assert logged, 'httpx2 logged the request'  # rules out a vacuous pass
    assert all('api_key=REDACTED' in message for message in logged)
    assert fake_openalex.API_KEY.value not in caplog.text


@pytest.mark.parametrize('value', ['', 'key\n', 'a key'], ids=['empty', 'trailing-newline', 'inner-space'])
def test_an_api_key_that_is_not_a_token_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match='non-empty token'):
        openalex.ApiKey(value)


def test_an_api_keys_repr_does_not_show_it() -> None:
    assert fake_openalex.API_KEY.value not in repr(fake_openalex.API_KEY)


def test_find_fails_when_a_shifted_order_serves_a_work_twice_and_another_never() -> None:
    # OpenAlex's order shifted between the pages: page 2 repeats a work page 1 served and omits another.
    works = [fake_openalex.work(work_id=f'W{i}', pmid=str(i + 1)) for i in range(101)]

    def handler(request: httpx2.Request) -> httpx2.Response:
        page = works[:100] if request.url.params['page'] == '1' else works[:1]
        return _page(page, matched=len(works))

    with pytest.raises(ValueError, match='served 100 distinct'):
        _find(openalex.Scheme.PMID, [str(i + 1) for i in range(100)], handler)


def _page(results: list[dict[str, object]], *, matched: int | None = None) -> httpx2.Response:
    count = len(results) if matched is None else matched
    return httpx2.Response(200, content=json.dumps({'meta': {'count': count}, 'results': results}).encode())


def test_read_work_names_the_index_in_the_drift() -> None:
    with pytest.raises(upstream_record.SchemaDriftError) as excinfo:
        openalex.read_work(_work(_DOI_A, primary_location={'source': {'host_organization_name': 5}}))
    assert excinfo.value.index == 'openalex'


def test_parse_work_reads_no_field_and_refuses_a_number_json_text_cannot_carry() -> None:
    drifted = _work(
        _DOI_A, ids={'pmcid': {'drifted': True}}, primary_location={'source': {'host_organization_name': 5}}
    )
    assert upstream_record.parse_text(openalex.parse_work(drifted).text) == drifted
    with pytest.raises(upstream_record.SchemaDriftError) as excinfo:
        openalex.parse_work(_work(_DOI_A, fwci=float('nan')))
    assert excinfo.value.index == 'openalex'


def test_a_key_no_reader_reads_is_kept_in_the_text_and_fails_nothing() -> None:
    source = _work(_DOI_A, is_zpac=True, primary_location={'source': {'display_name': 'J'}, 'doi': 'x'})
    answer = _find(openalex.Scheme.DOI, [_DOI_A], _holding(source)).answer(_DOI_A)

    assert answer is not None
    assert upstream_record.parse_text(answer.work.text) == source


@pytest.mark.parametrize(
    ('fixture', 'pmid'),
    [('oa', '29089047'), ('preprint', None)],
)
def test_fixture_work_reads_whole(fixture: str, pmid: str | None) -> None:
    source = _fixture_work(fixture)
    work = openalex.parse_work(source)
    assert upstream_record.parse_text(work.text) == source
    assert openalex.read_work(work.record).pmid == pmid


def test_sourced_holds_the_record_alone_with_its_fields_filled() -> None:
    source = _fixture_work('preprint')
    sourced = openalex.sourced(openalex.parse_work(source))
    sourced_metadata.check(sourced)

    assert upstream_record.parse_text(sourced.source.openalex_json) == source
    assert not sourced.source.HasField('pubmed')
    assert sourced.metadata.title == source['title']


def test_paired_stores_the_work_beside_the_record_and_takes_its_authors() -> None:
    work = openalex.parse_work(_fixture_work('preprint'))
    resolved = litcache_pb2.SourcedMetadata(
        metadata=litcache_pb2.Metadata(title='T'),
        source=litcache_pb2.MetadataSource(crossref_json='{"title": ["T"], "author": [{"family": "Doe"}]}'),
    )
    sourced = openalex.paired(resolved, work)
    sourced_metadata.check(sourced)

    assert sourced.source.crossref_json == resolved.source.crossref_json
    assert sourced.source.openalex_json == work.text
    assert sourced.metadata.title == 'T'
    assert list(sourced.metadata.authors) == list(openalex.sourced(work).metadata.authors)


def test_paired_refuses_a_record_that_already_holds_openalexs() -> None:
    work = openalex.parse_work(_fixture_work('preprint'))
    with pytest.raises(ValueError, match="already holds OpenAlex's"):
        openalex.paired(openalex.sourced(work), work)


@pytest.fixture
def live_api_key() -> openalex.ApiKey:
    """The key a live gate sends: `LITCACHE_OPENALEX_LIVE_API_KEY`, the gate skipped when it is unset."""
    value = os.environ.get('LITCACHE_OPENALEX_LIVE_API_KEY')
    if value is None:
        pytest.skip('set LITCACHE_OPENALEX_LIVE_API_KEY: every OpenAlex request Themis sends carries a key')
    return openalex.ApiKey(value)


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_OPENALEX_LIVE_DOI'),
    reason='set LITCACHE_OPENALEX_LIVE_DOI to hit the live OpenAlex works API',
)
def test_live_openalex(live_api_key: openalex.ApiKey) -> None:
    doi = os.environ['LITCACHE_OPENALEX_LIVE_DOI']

    async def run() -> openalex.Found:
        async with httpx2.AsyncClient(timeout=30.0) as client:
            return await openalex.find(openalex.Scheme.DOI, [doi], http_client=client, api_key=live_api_key)

    assert asyncio.run(run()).answer(doi) is not None


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_OPENALEX_LIVE_SAMPLE'),
    reason='set LITCACHE_OPENALEX_LIVE_SAMPLE to a sample size to load that many live works',
)
@pytest.mark.parametrize('with_pmid', [False, True], ids=['any', 'with-pmid'])
def test_live_openalex_sample_loads(with_pmid: bool, live_api_key: openalex.ApiKey) -> None:
    # Only a work with a PMID carries MeSH, so the second sample is drawn from those to exercise the headings.
    size = int(os.environ['LITCACHE_OPENALEX_LIVE_SAMPLE'])
    params = {
        'sample': str(size),
        'seed': str(random.randint(1, 10**6)),  # noqa: S311 — a sampling seed, not a secret
        'per-page': str(size),
        'mailto': constants.CONTACT_EMAIL,
    }
    if with_pmid:
        params['filter'] = 'has_pmid:true'

    async def run() -> list[dict[str, object]]:
        async with httpx2.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                'https://api.openalex.org/works', params=params | {'api_key': live_api_key.value}
            )
            response.raise_for_status()
            return response.json()['results']

    orcids = headings = 0
    for source in asyncio.run(run()):
        work = openalex.parse_work(source)
        assert upstream_record.parse_text(work.text) == source
        sourced = openalex.sourced(work)
        sourced_metadata.check(sourced)
        metadata = sourced.metadata
        if source.get('title'):
            assert metadata.title, source.get('id')
        # A title is plain text, however the index escaped its markup.
        assert not _TAG.search(metadata.title), metadata.title
        _assert_abstract_whole(source, metadata)
        _assert_identifiers_hold(source, metadata)
        orcids += sum(1 for author in metadata.authors if author.orcid)
        headings += len(metadata.mesh_headings)
    assert orcids, 'no sampled work carries an ORCID iD the fill read'  # rules out a vacuous pass
    assert headings or not with_pmid, 'no sampled work with a PMID carries a MeSH heading'


# How many PubMed papers the live pairing gate resolves: a handful, as ingest would, PubMed and OpenAlex each once.
_LIVE_PAIRS = 20


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_OPENALEX_LIVE_SAMPLE'),
    reason='set LITCACHE_OPENALEX_LIVE_SAMPLE to resolve live PubMed papers with their OpenAlex works',
)
def test_live_pubmed_papers_take_their_authors_from_openalexs_work(live_api_key: openalex.ApiKey) -> None:
    """Bulk ingest's resolution of random PMIDs stores OpenAlex's work beside PubMed's record and fills from both."""
    params = {
        'sample': str(_LIVE_PAIRS),
        'seed': str(random.randint(1, 10**6)),  # noqa: S311 — a sampling seed, not a secret
        'per-page': str(_LIVE_PAIRS),
        'filter': 'has_pmid:true',
        'select': 'ids',
        'mailto': constants.CONTACT_EMAIL,
    }

    async def run() -> dict[str, resolve.Outcome]:
        async with (
            httpx2.AsyncClient(timeout=60.0) as client,
            litfetch.Session() as session,  # unused: every request carries its PMID
        ):
            response = await client.get(
                'https://api.openalex.org/works', params=params | {'api_key': live_api_key.value}
            )
            response.raise_for_status()
            stated = (openalex.read_work(work).pmid for work in response.json()['results'])
            requests = [resolve.ResolveRequest(join_key=p, pmid=p, doi=None) for p in sorted({p for p in stated if p})]
            return await resolve.resolve_batch(
                requests, http_client=client, session=session, openalex_api_key=live_api_key
            )

    paired = 0
    for pmid, outcome in asyncio.run(run()).items():
        if isinstance(outcome, resolve.AmbiguousWorkFailure):
            continue  # OpenAlex holding two works of a PMID is an anomaly a dead letter reports, not a gate failure
        assert isinstance(outcome, resolve.ResolvedPaper), (pmid, outcome)
        sourced = sourced_metadata.parse(outcome.metadata)
        assert sourced.source.HasField('pubmed'), pmid
        if not sourced.source.HasField('openalex_json'):
            continue
        paired += 1
        work = upstream_record.parse_text(sourced.source.openalex_json)
        authorships = work.get('authorships') or []
        assert isinstance(authorships, list)
        named = [a for a in authorships if ((a.get('author') or {}).get('display_name') or '').strip()]
        if not named:  # OpenAlex names no author: the resolving record's authors stand
            continue
        assert len(sourced.metadata.authors) == len(named), pmid
        assert [a.orcid for a in sourced.metadata.authors] == [_bare_orcid(a['author'].get('orcid')) for a in named]
    assert paired, 'no sampled PMID resolved with an OpenAlex work beside it'  # rules out a vacuous pass


# How many PubMed papers of more than 100 authors the live authorship gate resolves.
_LIVE_LONG_AUTHOR_LISTS = 3


@pytest.mark.skipif(
    not os.environ.get('LITCACHE_OPENALEX_LIVE_SAMPLE'),
    reason='set LITCACHE_OPENALEX_LIVE_SAMPLE to resolve live papers whose authors a works list cuts short',
)
def test_live_a_work_beside_holds_every_authorship(live_api_key: openalex.ApiKey) -> None:
    """A works list serves 100 authorships of a work; the work stored beside PubMed's record holds them all."""
    params = {
        'sample': str(_LIVE_LONG_AUTHOR_LISTS),
        'seed': str(random.randint(1, 10**6)),  # noqa: S311 — a sampling seed, not a secret
        'per-page': str(_LIVE_LONG_AUTHOR_LISTS),
        'filter': 'authors_count:>100,has_pmid:true',
        'select': 'ids',
        'mailto': constants.CONTACT_EMAIL,
        'api_key': live_api_key.value,
    }

    async def run() -> list[tuple[int, int]]:
        async with (
            httpx2.AsyncClient(timeout=60.0) as client,
            litfetch.Session() as session,  # unused: every request carries its PMID
        ):
            response = await client.get('https://api.openalex.org/works', params=params)
            response.raise_for_status()
            stated = (openalex.read_work(work).pmid for work in response.json()['results'])
            requests = [resolve.ResolveRequest(join_key=p, pmid=p, doi=None) for p in sorted({p for p in stated if p})]
            outcomes = await resolve.resolve_batch(
                requests, http_client=client, session=session, openalex_api_key=live_api_key
            )
            counts: list[tuple[int, int]] = []
            for outcome in outcomes.values():
                if not isinstance(outcome, resolve.ResolvedPaper):
                    continue
                sourced = sourced_metadata.parse(outcome.metadata)
                if not sourced.source.HasField('openalex_json'):
                    continue
                stored = upstream_record.parse_text(sourced.source.openalex_json)
                single = await client.get(
                    str(stored['id']).replace('https://openalex.org/', 'https://api.openalex.org/works/'),
                    params={'select': 'authorships', 'mailto': constants.CONTACT_EMAIL, 'api_key': live_api_key.value},
                )
                single.raise_for_status()
                authorships = stored['authorships']
                assert isinstance(authorships, list)
                counts.append((len(authorships), len(single.json()['authorships'])))
            return counts

    counts = asyncio.run(run())
    assert counts, 'no sampled paper resolved with an OpenAlex work beside it'  # rules out a vacuous pass
    assert all(stored == single > 100 for stored, single in counts), counts


# What the fill may drop from the front of a rebuilt abstract, whitespace removed: its "Abstract" heading.
_ABSTRACT_HEADINGS = frozenset({'', 'Abstract', 'ABSTRACT', 'Abstract:', 'ABSTRACT:', 'Abstract.', 'ABSTRACT.'})


def _assert_abstract_whole(source: dict[str, object], metadata: litcache_pb2.Metadata) -> None:
    """Assert the fill kept every character of a work's abstract but its whitespace and an "Abstract" heading.

    The comparison drops whitespace on both sides, because the fill collapses it and an index key can hold some of its
    own (a line break, a thin space in `P < 0.01`); a word holding `<` or `>` must survive as text either way.
    """
    words = source.get('abstract_inverted_index') or {}
    assert isinstance(words, dict)
    placed = sorted((position, word) for word, positions in words.items() for position in positions)
    whole = ''.join(''.join(word for _, word in placed).split())
    kept = ''.join(metadata.abstract.split())
    assert whole.endswith(kept), source['id']
    assert whole[: len(whole) - len(kept)] in _ABSTRACT_HEADINGS, source['id']


@pytest.mark.parametrize(
    'index',
    [
        pytest.param({'Results:': [0], '(P\u2009<\u20090.01)': [1], 'held.': [2]}, id='thin-spaces-around-a-bracket'),
        pytest.param({'(P<0.001).\n\nConclusions': [0], 'follow.': [1]}, id='line-break-inside-a-key'),
        pytest.param({'Abstract:<x': [0], 'rest.': [1]}, id='heading-glued-to-a-bracket'),
        pytest.param({'p': [0], '<': [1], '0.05': [2]}, id='a-lone-bracket'),
    ],
)
def test_a_rebuilt_abstract_keeps_every_character_but_whitespace(index: dict[str, list[int]]) -> None:
    source = _work('10.1/abstract', abstract_inverted_index=index)
    metadata = openalex.sourced(openalex.parse_work(source)).metadata
    assert metadata.abstract
    _assert_abstract_whole(source, metadata)


def _assert_identifiers_hold(source: dict[str, object], metadata: litcache_pb2.Metadata) -> None:
    """Assert the fill's ORCID iDs, MeSH headings and publication type are the ones a live work states.

    Each named author's iD is the bare form of their profile's, never the one deposited with the authorship, or "" where
    none is or it fails its check. The headings name each descriptor the rows name, in first-seen order; a
    descriptor is a major topic exactly when one of its rows is, and no qualifier is. A type is one the table maps to.
    """
    authorships = source.get('authorships') or []
    assert isinstance(authorships, list)
    named = [a for a in authorships if ((a.get('author') or {}).get('display_name') or '').strip()]
    profiles = [_bare_orcid(a['author'].get('orcid')) for a in named]
    assert [author.orcid for author in metadata.authors] == profiles, source['id']
    rows = source.get('mesh') or []
    assert isinstance(rows, list)
    assert [h.descriptor.ui for h in metadata.mesh_headings] == list(dict.fromkeys(r['descriptor_ui'] for r in rows))
    for heading in metadata.mesh_headings:
        marked = any(r['is_major_topic'] for r in rows if r['descriptor_ui'] == heading.descriptor.ui)
        assert heading.descriptor.major_topic == marked, (source['id'], heading)
        assert not any(q.major_topic for q in heading.qualifiers), (source['id'], heading)
    assert len(metadata.publication_types) <= 1
    assert set(metadata.publication_types) <= _MAPPED_TYPES, metadata.publication_types


def _bare_orcid(stated: object) -> str:
    """The bare form of a stated ORCID iD whose ISO 7064 MOD 11-2 check digit holds, else ""."""
    match = _STATED_ORCID.fullmatch(stated) if isinstance(stated, str) else None
    if match is None:
        return ''
    orcid = match[1].upper()
    total = 0
    for digit in orcid[:-1].replace('-', ''):
        total = (total + int(digit)) * 2
    check = (12 - total % 11) % 11
    return orcid if orcid[-1] == '0123456789X'[check] else ''


def _merged(published: str, preprint: str) -> dict[str, object]:
    """A work OpenAlex merged from a preprint into its published version: the preprint's DOI is on a location alone."""
    return _work(
        published,
        locations=[{'doi': f'https://doi.org/{published}'}, {'doi': f'https://doi.org/{preprint}'}],
    )


_PUBLISHED = '10.1002/ajmg.b.70032'
_PREPRINT = '10.64898/2026.03.03.26347377'


def test_a_doi_lookup_answers_a_preprint_merged_into_its_published_version_by_location() -> None:
    found = openalex.read_results(openalex.Scheme.DOI, [_PREPRINT], [_merged(_PUBLISHED, _PREPRINT)])
    answer = found.answer(_PREPRINT)
    assert answer is not None
    assert answer.by_location


def test_a_doi_lookup_answers_both_dois_of_a_merged_work_asked_together() -> None:
    found = openalex.read_results(
        openalex.Scheme.DOI, [_PUBLISHED.upper(), _PREPRINT], [_merged(_PUBLISHED, _PREPRINT)]
    )
    published, preprint = found.answer(_PUBLISHED), found.answer(_PREPRINT)
    assert published is not None
    assert not published.by_location
    assert preprint is not None
    assert preprint.by_location


def test_a_work_answering_only_by_location_gives_neither_identifiers_nor_publisher() -> None:
    answer = openalex.read_results(openalex.Scheme.DOI, [_PREPRINT], [_merged(_PUBLISHED, _PREPRINT)]).answer(_PREPRINT)
    assert answer is not None
    assert openalex.read_work(answer.work.record).pmid == '111'  # the published version's, stated on the work
    assert openalex.stated_pmid(answer) is None
    assert openalex.read_answer(answer) == openalex.WorkFields(pmid=None, pmcid=None, publisher=None)


def test_a_doi_looked_up_alone_is_answered_by_a_work_stating_it_on_a_location() -> None:
    found = _find(openalex.Scheme.DOI, [_COMMA_DOI], _holding(_merged('10.1/published', _COMMA_DOI)))
    answer = found.answer(_COMMA_DOI)
    assert answer is not None
    assert answer.by_location


def test_a_work_stating_a_doi_as_its_own_answers_it_ahead_of_one_stating_it_on_a_location() -> None:
    own = _work(_PREPRINT)
    found = openalex.read_results(openalex.Scheme.DOI, [_PREPRINT], [_merged(_PUBLISHED, _PREPRINT), own])
    answer = found.answer(_PREPRINT)
    assert answer is not None
    assert not answer.by_location
    assert answer.work.record == own


@pytest.mark.parametrize(
    'works',
    [
        pytest.param([_work(_PREPRINT, id='https://openalex.org/W1'), _work(_PREPRINT)], id='both-own'),
        pytest.param([_merged(_PUBLISHED, _PREPRINT), _merged('10.1/other', _PREPRINT)], id='both-by-location'),
    ],
)
def test_two_works_stating_a_doi_alike_make_it_ambiguous(works: list[dict[str, object]]) -> None:
    found = openalex.read_results(openalex.Scheme.DOI, [_PREPRINT], works)
    with pytest.raises(openalex.AmbiguousWorkError):
        found.answer(_PREPRINT)


@pytest.mark.parametrize('locations', [None, []], ids=['null', 'empty'])
def test_a_doi_lookup_reads_a_work_without_locations_by_its_own_doi(locations: list[object] | None) -> None:
    found = openalex.read_results(openalex.Scheme.DOI, ['10.1/a'], [_work('10.1/a', locations=locations)])
    assert found.answer('10.1/a') is not None


_UNREADABLE_LOCATIONS = [pytest.param([{'doi': 5}], id='a-location-doi'), pytest.param({'doi': 'x'}, id='not-a-list')]


@pytest.mark.parametrize('locations', _UNREADABLE_LOCATIONS)
def test_unreadable_locations_leave_the_own_doi_answered_and_charge_the_dois_they_may_state(locations: object) -> None:
    unreadable = _work(_PUBLISHED, locations=locations)
    held, unheld = '10.1/held', '10.1/unheld'
    works = [unreadable, _merged('10.1/other', held)]
    found = openalex.read_results(openalex.Scheme.DOI, [_PUBLISHED, held, unheld], works)

    answer = found.answer(_PUBLISHED)
    assert answer is not None
    assert not answer.by_location
    for doi in (held, unheld):  # its locations may hide a second work for one, the only work for the other
        with pytest.raises(upstream_record.SchemaDriftError, match='locations'):
            found.answer(doi)


def test_unreadable_locations_of_a_work_answering_a_doi_alone_are_charged_to_it() -> None:
    found = openalex.read_results(openalex.Scheme.DOI, [_PREPRINT], [_work(_PUBLISHED, locations=[{'doi': 5}])])
    with pytest.raises(upstream_record.SchemaDriftError, match='locations'):
        found.answer(_PREPRINT)


def test_unreadable_locations_fail_the_lookup_when_no_doi_asked_can_be_charged() -> None:
    works = [_work(_DOI_A), _work(_PUBLISHED, locations=[{'doi': 5}])]
    with pytest.raises(upstream_record.SchemaDriftError, match='locations'):
        openalex.read_results(openalex.Scheme.DOI, [_DOI_A], works)


def test_a_doi_lookup_fails_on_a_work_stating_none_of_the_dois_asked() -> None:
    work = _merged('10.1/published', '10.1/preprint')
    with pytest.raises(ValueError, match='none of the identifiers asked'):
        openalex.read_results(openalex.Scheme.DOI, ['10.1/other'], [work])
