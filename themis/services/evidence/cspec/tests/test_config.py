"""The cspec interface's env contract and gRPC registration seam."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
from collections.abc import AsyncIterator

import grpc
import grpc.aio
import httpx2
import pytest

from themis.common import authored_status
from themis.rpc import cspec_pb2, cspec_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.cspec import interface
from themis.services.evidence.tests import authz
from themis.testing import in_process_grpc

_SEED = json.dumps({'list_specifications': {'BRCA1': {'specifications': [{'id': 'GN101'}]}}})


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
async def _serving(http_client: httpx2.AsyncClient | None = None) -> AsyncIterator[cspec_pb2_grpc.CspecAsyncStub]:
    async with _deps_context(http_client) as deps:
        register = functools.partial(interface.register, deps=deps)
        async with authz.gated(register) as channel:
            yield cspec_pb2_grpc.CspecStub(channel)


def _start_server() -> None:
    async def run() -> None:
        async with _serving():
            pass

    asyncio.run(run())


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_CSPEC_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_CSPEC_BACKEND'):
        _start_server()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CSPEC_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _start_server()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CSPEC_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_CSPEC_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_CSPEC_FIXTURE'):
        _start_server()


def test_fixture_selector_serves_seeded_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CSPEC_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_CSPEC_FIXTURE', _SEED)

    async def run() -> cspec_pb2.ListSpecificationsResponse:
        async with _serving() as stub:
            return await stub.ListSpecifications(cspec_pb2.ListSpecificationsRequest(gene='BRCA1'))

    assert [s.id for s in asyncio.run(run()).specifications] == ['GN101']


def test_fixture_selector_reports_unseeded_keys_as_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CSPEC_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_CSPEC_FIXTURE', _SEED)

    async def run() -> None:
        async with _serving() as stub:
            await stub.ListSpecifications(cspec_pb2.ListSpecificationsRequest(gene='BRCA2'))

    with pytest.raises(grpc.aio.AioRpcError) as unseeded:
        asyncio.run(run())
    assert unseeded.value.code() is grpc.StatusCode.NOT_FOUND
    assert authored_status.authored(unseeded.value)
    assert 'seeded' in (unseeded.value.details() or '')


def test_register_denies_an_unadmitted_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CSPEC_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_CSPEC_FIXTURE', _SEED)

    async def run() -> None:
        async with (
            _deps_context() as deps,
            in_process_grpc.serving(
                functools.partial(interface.register, deps=deps),
                interceptors=(authz.InjectMetadata(authz.WEB),),
                server_interceptors=authz.server_interceptors(),
            ) as channel,
        ):
            await cspec_pb2_grpc.CspecStub(channel).ListSpecifications(
                cspec_pb2.ListSpecificationsRequest(gene='BRCA1')
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED


def test_live_selector_serves_over_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CSPEC_BACKEND', 'live')
    missing = {'status': {'code': 404, 'name': 'Not Found', 'msg': "Bad Entity - No 'Gene' entity found"}}

    async def run() -> cspec_pb2.ListSpecificationsResponse:
        transport = httpx2.MockTransport(lambda _r: httpx2.Response(404, json=missing))
        async with httpx2.AsyncClient(transport=transport) as http_client, _serving(http_client) as stub:
            return await stub.ListSpecifications(cspec_pb2.ListSpecificationsRequest(gene='UNSPECIFIED1'))

    reply = asyncio.run(run())
    assert reply.coverage == cspec_pb2.SPECIFICATION_COVERAGE_GENE_ABSENT
    assert reply.provenance[0].source == 'ClinGen CSpec Registry'
