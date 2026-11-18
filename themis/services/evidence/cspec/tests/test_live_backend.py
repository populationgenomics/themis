"""Live `Cspec.ListSpecifications` over in-process gRPC and `httpx2.MockTransport`."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import pathlib
from collections.abc import Callable, Mapping

import httpx2
import pytest

from themis.rpc import cspec_pb2, cspec_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.cspec import interface
from themis.services.evidence.tests import authz
from themis.services.evidence.upstreams import destinations

_FIXTURE = json.loads(
    (pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures' / 'cspec.json').read_bytes()
)
_MISSING = {'status': {'code': 404, 'name': 'Not Found', 'msg': "Bad Entity - No 'Gene' entity found"}}


def _registry_records() -> dict[tuple[str, str], dict[str, object]]:
    specification = _FIXTURE['specification']
    held: dict[tuple[str, str], dict[str, object]] = {
        ('Gene', 'ACTC1'): _FIXTURE['gene'],
        ('SequenceVariantInterpretation', 'GN101'): specification,
    }
    for embedded in specification['ld']['RuleSet']:
        held[('RuleSet', embedded['ldhId'])] = _FIXTURE['rule_set']
    return held


def _handler(records: Mapping[tuple[str, str], dict[str, object]]) -> Callable[[httpx2.Request], httpx2.Response]:
    def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params.get('detail') == 'high'
        entity_type, _, identifier = request.url.path.removeprefix('/cspec/').partition('/id/')
        record = records.get((entity_type, identifier))
        if record is None:
            return httpx2.Response(404, json=_MISSING)
        return httpx2.Response(200, json={'data': record, 'status': {'code': 200, 'name': 'OK'}})

    return handle


def test_list_specifications_stamps_one_provenance_per_request_the_traversal_issued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry is reached in several hops, and each is a document a quote can rest on.

    One Provenance for the whole call would name a single URL for values read off three, and would
    lose which document version each specification's text came from.
    """
    monkeypatch.setenv('THEMIS_CSPEC_BACKEND', 'live')
    records = _registry_records()

    async def run() -> cspec_pb2.ListSpecificationsResponse:
        async with (
            contextlib.AsyncExitStack() as stack,
            destinations.admitting_client(
                timeout=httpx2.Timeout(10.0), transport=httpx2.MockTransport(_handler(records))
            ) as http_client,
        ):
            deps = deps_mod.Deps(authorizer=authz.authorizer(), http_client=http_client, stack=stack)
            async with authz.gated(functools.partial(interface.register, deps=deps)) as channel:
                return await cspec_pb2_grpc.CspecStub(channel).ListSpecifications(
                    cspec_pb2.ListSpecificationsRequest(gene='ACTC1')
                )

    resp = asyncio.run(run())
    assert resp.coverage == cspec_pb2.SPECIFICATION_COVERAGE_SPECIFIED
    assert [s.id for s in resp.specifications] == ['GN101']
    assert len(resp.provenance) == 3
    assert all(p.source == 'ClinGen CSpec Registry' and p.HasField('retrieved_at') for p in resp.provenance)
    assert [list(p.dataset_versions) for p in resp.provenance] == [[], ['GN101 1.0'], ['GN101 1.0']]
    assert '/cspec/Gene/id/ACTC1' in resp.provenance[0].query
    assert '/cspec/SequenceVariantInterpretation/id/GN101' in resp.provenance[1].query
    assert '/cspec/RuleSet/id/1535829283' in resp.provenance[2].query
    assert 'GN101' in resp.raw.fields['specifications'].struct_value.fields
