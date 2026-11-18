"""Live `Gnomad.DescribeVariant` over in-process gRPC and `httpx2.MockTransport`."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import pathlib

import httpx2
import pytest

from themis.rpc import gnomad_pb2, gnomad_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.gnomad import interface
from themis.services.evidence.tests import authz
from themis.services.evidence.upstreams import destinations

_VARIANT_FIXTURE = json.loads(
    (pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures' / 'gnomad.json').read_bytes()
)['variant']


def test_gnomad_passthrough_maps_empty_cooccurrence_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GNOMAD_BACKEND', 'live')
    seen_queries: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        assert body['variables'] == {'id': '1-55051215-G-A', 'ds': 'gnomad_r4'}
        seen_queries.append(str(body['query']))
        return httpx2.Response(200, json=_VARIANT_FIXTURE)

    async def run() -> gnomad_pb2.DescribeVariantResponse:
        async with (
            contextlib.AsyncExitStack() as stack,
            destinations.admitting_client(
                timeout=httpx2.Timeout(10.0), transport=httpx2.MockTransport(handler)
            ) as http_client,
        ):
            deps = deps_mod.Deps(authorizer=authz.authorizer(), http_client=http_client, stack=stack)
            async with authz.gated(functools.partial(interface.register, deps=deps)) as channel:
                return await gnomad_pb2_grpc.GnomadStub(channel).DescribeVariant(
                    gnomad_pb2.DescribeVariantRequest(gnomad_id='1-55051215-G-A', dataset='gnomad_r4')
                )

    resp = asyncio.run(run())
    assert len(seen_queries) == 1
    assert 'variant_cooccurrence' not in seen_queries[0]
    variant = resp.raw.fields['variant'].struct_value
    assert variant.fields['exome'].struct_value.fields['homozygote_count'].number_value == 1
    assert [p.source for p in resp.provenance] == ['gnomAD GraphQL']
    assert list(resp.provenance[0].dataset_versions) == ['gnomad_r4']
    assert '1-55051215-G-A' in resp.provenance[0].query
