"""LiveBackend composition: how `ListSpecifications` stamps the registry traversal it ran.

The upstream client function is replaced with a canned Result, so no test here touches the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest

from themis.rpc import cspec_pb2
from themis.services.evidence.cspec import backend as cspec_backend
from themis.services.evidence.upstreams import cspec


def _run[T](call: Callable[[cspec_backend.LiveBackend], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx.AsyncClient() as client:
            return await call(cspec_backend.LiveBackend(client))

    return asyncio.run(run())


def test_list_specifications_stamps_one_provenance_per_request_the_traversal_issued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry is reached in several hops, and each is a document a quote can rest on.

    One Provenance for the whole call would name a single URL for values read off three, and would
    lose which document version each specification's text came from.
    """
    captured: dict[str, object] = {}

    async def fake_fetch(gene: str, **_kwargs: object) -> cspec.CspecResult:
        captured['gene'] = gene
        return cspec.CspecResult(
            specifications=[cspec_pb2.VcepSpecification(id='GN101')],
            coverage=cspec_pb2.SPECIFICATION_COVERAGE_SPECIFIED,
            raw={'gene': {'symbol': gene}, 'candidate_specifications': [], 'rule_sets': {}},
            queries=[
                cspec.SourceQuery(source='ClinGen CSpec Registry', dataset_versions=(), query='gene-url'),
                cspec.SourceQuery(
                    source='ClinGen CSpec Registry', dataset_versions=('GN101 1.0',), query='document-url'
                ),
            ],
        )

    monkeypatch.setattr(cspec, 'fetch_criteria_specifications', fake_fetch)

    resp = _run(lambda be: be.list_specifications(cspec_pb2.ListSpecificationsRequest(gene='ACTC1')))
    assert captured['gene'] == 'ACTC1'
    assert [(list(p.dataset_versions), p.query) for p in resp.provenance] == [
        ([], 'gene-url'),
        (['GN101 1.0'], 'document-url'),
    ]
    assert all(p.HasField('retrieved_at') for p in resp.provenance)
    assert resp.coverage == cspec_pb2.SPECIFICATION_COVERAGE_SPECIFIED
    assert resp.raw['gene'] == {'symbol': 'ACTC1'}
