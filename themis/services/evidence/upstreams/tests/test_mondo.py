"""MONDO subclass closure: the OLS4 responses it accepts, and the ones it refuses to read.

Every response is served by an httpx MockTransport; no test touches the network. The refusals are
the point: a truncated or short collection read as complete would answer "your term is not above
this curation" from a response that never said so.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import httpx
import pytest

from themis.services.evidence.upstreams import mondo

_VERSION_PATH = '/ols4/api/ontologies/mondo'


def _term(obo_id: str) -> dict[str, object]:
    return {'obo_id': obo_id, 'label': f'label of {obo_id}'}


def _collection(*terms: Mapping[str, object], total_pages: int = 1) -> dict[str, object]:
    return {
        '_embedded': {'terms': list(terms)},
        'page': {'totalPages': total_pages, 'totalElements': len(terms)},
    }


def _client(routes: Mapping[str, object], *, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _VERSION_PATH:
            return httpx.Response(200, json={'config': {'version': '2026-07-01'}})
        for fragment, payload in routes.items():
            if fragment in str(request.url):
                return httpx.Response(status, json=payload)
        return httpx.Response(404, text=f'no route for {request.url}')

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _closure(routes: Mapping[str, object], terms: list[str], *, status: int = 200) -> mondo.MondoClosureResult:
    async def run() -> mondo.MondoClosureResult:
        async with _client(routes, status=status) as client:
            return await mondo.fetch_subclass_closure(terms, http_client=client)

    return asyncio.run(run())


def test_closure_carries_each_term_ancestry_and_the_release_it_was_read_from() -> None:
    routes = {
        'MONDO_0000101': _collection(_term('MONDO:0000100'), _term('MONDO:0000001')),
        'MONDO_0000102': _collection(_term('MONDO:0000100')),
    }
    result = _closure(routes, ['MONDO:0000101', 'MONDO:0000102'])
    assert result.ancestors == {
        'MONDO:0000101': ('MONDO:0000001', 'MONDO:0000100'),
        'MONDO:0000102': ('MONDO:0000100',),
    }
    assert result.dataset_versions == ('MONDO 2026-07-01',)
    assert 'MONDO:0000101' in result.query


def test_non_mondo_ancestors_are_dropped_from_the_closure() -> None:
    # OLS4 mixes upper-ontology terms into a MONDO closure; they are not disease terms to match on.
    routes = {'MONDO_0000101': _collection(_term('MONDO:0000100'), _term('BFO:0000016'), {'label': 'no id'})}
    assert _closure(routes, ['MONDO:0000101']).ancestors == {'MONDO:0000101': ('MONDO:0000100',)}


def test_a_paginated_closure_is_refused_rather_than_cut() -> None:
    routes = {'MONDO_0000101': _collection(_term('MONDO:0000100'), total_pages=2)}
    with pytest.raises(ValueError, match='truncated'):
        _closure(routes, ['MONDO:0000101'])


def test_a_collection_shorter_than_it_promises_is_refused() -> None:
    payload = _collection(_term('MONDO:0000100'))
    payload['page'] = {'totalPages': 1, 'totalElements': 7}
    with pytest.raises(ValueError, match='promises 7 terms'):
        _closure({'MONDO_0000101': payload}, ['MONDO:0000101'])


def test_a_term_with_no_mondo_ancestor_is_refused() -> None:
    with pytest.raises(ValueError, match='every disease term has at least one'):
        _closure({'MONDO_0000101': _collection()}, ['MONDO:0000101'])


def test_a_term_ols4_rejects_is_not_reported_as_the_caller_request() -> None:
    # The term asked about is a curated one, so a refusal is a stale reference table or a retired
    # MONDO term — never an INVALID_ARGUMENT about the request the caller issued.
    with pytest.raises(ValueError, match='rejected'):
        _closure({'MONDO_0000101': {'error': 'Not Found'}}, ['MONDO:0000101'], status=404)


def test_a_rate_limited_or_failing_ols4_stays_retryable() -> None:
    for status in (429, 503):
        with pytest.raises(httpx.HTTPStatusError):
            _closure({'MONDO_0000101': {'error': 'later'}}, ['MONDO:0000101'], status=status)


def test_an_undated_ontology_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'config': {}} if request.url.path == _VERSION_PATH else {})

    async def run() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await mondo.fetch_mondo_version(http_client=client)

    with pytest.raises(ValueError, match='states no version'):
        asyncio.run(run())


def test_the_term_iri_is_encoded_the_way_ols4_addresses_it() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == _VERSION_PATH:
            return httpx.Response(200, json={'config': {'version': '2026-07-01'}})
        return httpx.Response(200, json=_collection(_term('MONDO:0000100')))

    async def run() -> mondo.MondoClosureResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await mondo.fetch_subclass_closure(['MONDO:0000101'], http_client=client)

    asyncio.run(run())
    ancestors_url = next(url for url in seen if 'ancestors' in url)
    # OLS4 takes the OBO IRI percent-encoded twice inside the path; once resolves to a 404.
    assert 'http%253A%252F%252Fpurl.obolibrary.org%252Fobo%252FMONDO_0000101' in ancestors_url
