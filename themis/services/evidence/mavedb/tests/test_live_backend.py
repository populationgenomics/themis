"""Live `MaveDb.DescribeVariant` over in-process gRPC and `httpx2.MockTransport`."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import pathlib
import urllib.parse

import httpx2
import pytest

from themis.rpc import mavedb_pb2, mavedb_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.mavedb import interface
from themis.services.evidence.tests import authz
from themis.services.evidence.upstreams import destinations

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_ALLELE_FIXTURE = json.loads((_FIXTURES / 'allele_registry.json').read_bytes())
_MAVEDB_FIXTURE = json.loads((_FIXTURES / 'mavedb.json').read_bytes())
_VARIANT = 'NM_000546.6:c.524G>A'
_MISSES: dict[str, object] = {'exactMatch': None, 'equivalentNt': [], 'equivalentAa': []}


def test_mavedb_asks_mavedb_about_the_alleles_the_variant_registers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry resolves the expression to allele ids; MaveDB is asked about those, not the text.

    Both upstreams answered, so both are stamped: a caller reading the score can see which allele the
    deposit was found under, and reproduce the join.
    """
    monkeypatch.setenv('THEMIS_MAVEDB_BACKEND', 'live')
    asked_alleles: list[list[str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == 'reg.clinicalgenome.org':
            if request.url.params.get('hgvs') != _VARIANT:
                return httpx2.Response(400, json={'errorType': 'HgvsParsingError', 'description': 'unexpected hgvs'})
            return httpx2.Response(200, json=_ALLELE_FIXTURE)
        if request.method == 'POST' and request.url.path.endswith('/clingen-allele-id-lookups'):
            allele_ids = json.loads(request.content)['clingenAlleleIds']
            asked_alleles.append(allele_ids)
            held = {'PA106629': _MAVEDB_FIXTURE['lookup'][0] | {'clingenAlleleId': 'PA106629'}}
            return httpx2.Response(
                200, json=[held.get(allele_id, _MISSES | {'clingenAlleleId': allele_id}) for allele_id in allele_ids]
            )
        assert '/variants/' in request.url.path
        urn = request.url.path.rsplit('/', 1)[-1]
        return httpx2.Response(200, json=_MAVEDB_FIXTURE['variants'][urn])

    async def run() -> mavedb_pb2.DescribeVariantResponse:
        async with (
            contextlib.AsyncExitStack() as stack,
            destinations.admitting_client(
                timeout=httpx2.Timeout(10.0), transport=httpx2.MockTransport(handler)
            ) as http_client,
        ):
            deps = deps_mod.Deps(authorizer=authz.authorizer(), http_client=http_client, stack=stack)
            async with authz.gated(functools.partial(interface.register, deps=deps)) as channel:
                return await mavedb_pb2_grpc.MaveDbStub(channel).DescribeVariant(
                    mavedb_pb2.DescribeVariantRequest(variant=_VARIANT)
                )

    resp = asyncio.run(run())
    assert len(asked_alleles) == 1
    assert asked_alleles[0][:2] == ['CA000251', 'PA106629']
    assert [p.source for p in resp.provenance] == ['ClinGen Allele Registry', 'MaveDB']
    assert _VARIANT in urllib.parse.unquote(resp.provenance[0].query)
    assert 'PA106629' in resp.provenance[1].query
    assert resp.acmg_criterion == 'BS3'
    assert resp.acmg_strength == 'STRONG'
    assert resp.HasField('oddspath_ratio')
    assert resp.oddspath_ratio == 0.0381
    assert resp.HasField('score')
    assert resp.score == _MAVEDB_FIXTURE['expected_score']
