"""The variant interface's env contract and gRPC registration seam."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import pathlib
from collections.abc import AsyncIterator

import grpc
import grpc.aio
import httpx2
import pytest

from themis.common import authored_status
from themis.rpc import variant_pb2, variant_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.tests import authz
from themis.services.evidence.variant import interface
from themis.testing import in_process_grpc

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_ALLELE_FIXTURE = json.loads((_FIXTURES / 'allele_registry.json').read_bytes())
_VALIDATOR_FIXTURE = json.loads((_FIXTURES / 'variant_validator.json').read_bytes())
_VEP_FIXTURE = json.loads((_FIXTURES / 'vep.json').read_bytes())
_SEED = json.dumps({'normalize': {'NM_007294.4:c.5074G>C': {'caid': 'CA028800'}}})


def _no_network(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f'unexpected network call: {request.url}')


@contextlib.asynccontextmanager
async def _deps_context(http_client: httpx2.AsyncClient | None = None) -> AsyncIterator[deps_mod.Deps]:
    async with contextlib.AsyncExitStack() as stack:
        client = (
            http_client
            if http_client is not None
            else await stack.enter_async_context(httpx2.AsyncClient(transport=httpx2.MockTransport(_no_network)))
        )
        yield deps_mod.Deps(authorizer=authz.authorizer(), http_client=client, stack=stack)


@contextlib.asynccontextmanager
async def _serving(http_client: httpx2.AsyncClient | None = None) -> AsyncIterator[variant_pb2_grpc.VariantAsyncStub]:
    async with _deps_context(http_client) as deps:
        register = functools.partial(interface.register, deps=deps)
        async with authz.gated(register) as channel:
            yield variant_pb2_grpc.VariantStub(channel)


def _start_server() -> None:
    async def run() -> None:
        async with _serving():
            pass

    asyncio.run(run())


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_VARIANT_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_VARIANT_BACKEND'):
        _start_server()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VARIANT_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _start_server()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VARIANT_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_VARIANT_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_VARIANT_FIXTURE'):
        _start_server()


def test_fixture_selector_serves_seeded_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VARIANT_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_VARIANT_FIXTURE', _SEED)

    async def run() -> variant_pb2.NormalizeResponse:
        async with _serving() as stub:
            return await stub.Normalize(
                variant_pb2.NormalizeRequest(variant='NM_007294.4:c.5074G>C', genome_build='GRCh38')
            )

    assert asyncio.run(run()).caid == 'CA028800'


def test_fixture_selector_reports_unseeded_keys_as_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VARIANT_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_VARIANT_FIXTURE', _SEED)

    async def run() -> None:
        async with _serving() as stub:
            await stub.Normalize(variant_pb2.NormalizeRequest(variant='NM_007294.4:c.1A>G', genome_build='GRCh38'))

    with pytest.raises(grpc.aio.AioRpcError) as unseeded:
        asyncio.run(run())
    assert unseeded.value.code() is grpc.StatusCode.NOT_FOUND
    assert authored_status.authored(unseeded.value)
    assert 'seeded' in (unseeded.value.details() or '')


def test_register_denies_an_unadmitted_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VARIANT_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_VARIANT_FIXTURE', _SEED)

    async def run() -> None:
        async with (
            _deps_context() as deps,
            in_process_grpc.serving(
                functools.partial(interface.register, deps=deps),
                interceptors=(authz.InjectMetadata(authz.WEB),),
                server_interceptors=authz.server_interceptors(),
            ) as channel,
        ):
            await variant_pb2_grpc.VariantStub(channel).Normalize(
                variant_pb2.NormalizeRequest(variant='NM_007294.4:c.5074G>C', genome_build='GRCh38')
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED


def test_live_selector_serves_over_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VARIANT_BACKEND', 'live')

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == 'reg.clinicalgenome.org':
            return httpx2.Response(200, json=_ALLELE_FIXTURE)
        if request.url.host == 'rest.variantvalidator.org':
            return httpx2.Response(200, json=_VALIDATOR_FIXTURE)
        if request.url.path == '/info/software':
            return httpx2.Response(200, json={'release': 116})
        return httpx2.Response(200, json=_VEP_FIXTURE)

    async def run() -> variant_pb2.NormalizeResponse:
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client,
            _serving(http_client) as stub,
        ):
            return await stub.Normalize(
                variant_pb2.NormalizeRequest(variant='NM_000546.6:c.524G>A', genome_build='GRCh38')
            )

    reply = asyncio.run(run())
    assert reply.caid == 'CA000251'
    assert reply.gnomad_v4_id == '17-7675088-C-T'
