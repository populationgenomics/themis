"""The gnomad interface's env contract and gRPC registration seam."""

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
from themis.rpc import gnomad_pb2, gnomad_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.gnomad import interface
from themis.services.evidence.tests import authz
from themis.testing import in_process_grpc

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_UPSTREAM_VARIANT = json.loads((_FIXTURES / 'gnomad.json').read_bytes())['variant']
_SEED = json.dumps({'describe_variant': {'17-43093464-C-G': {'provenance': [{'source': 'gnomAD GraphQL'}]}}})


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
async def _serving(http_client: httpx2.AsyncClient | None = None) -> AsyncIterator[gnomad_pb2_grpc.GnomadAsyncStub]:
    async with _deps_context(http_client) as deps:
        register = functools.partial(interface.register, deps=deps)
        async with authz.gated(register) as channel:
            yield gnomad_pb2_grpc.GnomadStub(channel)


def _start_server() -> None:
    async def run() -> None:
        async with _serving():
            pass

    asyncio.run(run())


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_GNOMAD_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_GNOMAD_BACKEND'):
        _start_server()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GNOMAD_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _start_server()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GNOMAD_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_GNOMAD_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_GNOMAD_FIXTURE'):
        _start_server()


def test_fixture_selector_serves_seeded_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GNOMAD_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_GNOMAD_FIXTURE', _SEED)

    async def run() -> gnomad_pb2.DescribeVariantResponse:
        async with _serving() as stub:
            return await stub.DescribeVariant(
                gnomad_pb2.DescribeVariantRequest(gnomad_id='17-43093464-C-G', dataset='gnomad_r4')
            )

    assert asyncio.run(run()).provenance[0].source == 'gnomAD GraphQL'


def test_fixture_selector_reports_unseeded_keys_as_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GNOMAD_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_GNOMAD_FIXTURE', _SEED)

    async def run() -> None:
        async with _serving() as stub:
            await stub.DescribeVariant(
                gnomad_pb2.DescribeVariantRequest(gnomad_id='17-43093464-C-A', dataset='gnomad_r4')
            )

    with pytest.raises(grpc.aio.AioRpcError) as unseeded:
        asyncio.run(run())
    assert unseeded.value.code() is grpc.StatusCode.NOT_FOUND
    assert authored_status.authored(unseeded.value)
    assert 'seeded' in (unseeded.value.details() or '')


def test_register_denies_an_unadmitted_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GNOMAD_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_GNOMAD_FIXTURE', _SEED)

    async def run() -> None:
        async with (
            _deps_context() as deps,
            in_process_grpc.serving(
                functools.partial(interface.register, deps=deps),
                interceptors=(authz.InjectMetadata(authz.WEB),),
                server_interceptors=authz.server_interceptors(),
            ) as channel,
        ):
            await gnomad_pb2_grpc.GnomadStub(channel).DescribeVariant(
                gnomad_pb2.DescribeVariantRequest(gnomad_id='17-43093464-C-G', dataset='gnomad_r4')
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED


def test_live_selector_serves_over_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GNOMAD_BACKEND', 'live')

    async def run() -> gnomad_pb2.DescribeVariantResponse:
        transport = httpx2.MockTransport(lambda _r: httpx2.Response(200, json=_UPSTREAM_VARIANT))
        async with httpx2.AsyncClient(transport=transport) as http_client, _serving(http_client) as stub:
            return await stub.DescribeVariant(
                gnomad_pb2.DescribeVariantRequest(gnomad_id='1-55051215-G-A', dataset='gnomad_r4')
            )

    reply = asyncio.run(run())
    assert reply.provenance[0].source == 'gnomAD GraphQL'
    assert list(reply.provenance[0].dataset_versions) == ['gnomad_r4']
