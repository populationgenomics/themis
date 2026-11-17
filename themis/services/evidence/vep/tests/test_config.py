"""The vep interface's env contract and gRPC registration seam."""

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
from themis.evidence.models import evidence_pb2
from themis.rpc import vep_pb2, vep_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.tests import authz
from themis.services.evidence.vep import interface
from themis.testing import in_process_grpc

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_VEP_FIXTURE = json.loads((_FIXTURES / 'vep.json').read_bytes())
_SEED = json.dumps({'annotate': {'NM_001042492.3:c.3496G>C': {'provenance': [{'source': 'Ensembl VEP REST'}]}}})


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
async def _serving(http_client: httpx2.AsyncClient | None = None) -> AsyncIterator[vep_pb2_grpc.VepAsyncStub]:
    async with _deps_context(http_client) as deps:
        register = functools.partial(interface.register, deps=deps)
        async with authz.gated(register) as channel:
            yield vep_pb2_grpc.VepStub(channel)


def _start_server() -> None:
    async def run() -> None:
        async with _serving():
            pass

    asyncio.run(run())


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_VEP_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_VEP_BACKEND'):
        _start_server()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VEP_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _start_server()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VEP_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_VEP_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_VEP_FIXTURE'):
        _start_server()


def test_fixture_selector_serves_seeded_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VEP_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_VEP_FIXTURE', _SEED)

    async def run() -> vep_pb2.AnnotateResponse:
        async with _serving() as stub:
            return await stub.Annotate(vep_pb2.AnnotateRequest(variant='NM_001042492.3:c.3496G>C'))

    assert asyncio.run(run()).provenance[0].source == 'Ensembl VEP REST'


def test_fixture_selector_reports_unseeded_keys_as_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VEP_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_VEP_FIXTURE', _SEED)

    async def run() -> None:
        async with _serving() as stub:
            await stub.Annotate(vep_pb2.AnnotateRequest(variant='NM_001042492.3:c.1A>G'))

    with pytest.raises(grpc.aio.AioRpcError) as unseeded:
        asyncio.run(run())
    assert unseeded.value.code() is grpc.StatusCode.NOT_FOUND
    assert authored_status.authored(unseeded.value)
    assert 'seeded' in (unseeded.value.details() or '')


def test_register_denies_an_unadmitted_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VEP_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_VEP_FIXTURE', _SEED)

    async def run() -> None:
        async with (
            _deps_context() as deps,
            in_process_grpc.serving(
                functools.partial(interface.register, deps=deps),
                interceptors=(authz.InjectMetadata(authz.WEB),),
                server_interceptors=authz.server_interceptors(),
            ) as channel,
        ):
            await vep_pb2_grpc.VepStub(channel).Annotate(vep_pb2.AnnotateRequest(variant='NM_001042492.3:c.3496G>C'))

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED


def test_live_selector_serves_over_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VEP_BACKEND', 'live')

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == '/info/software':
            return httpx2.Response(200, json={'release': 116})
        return httpx2.Response(200, json=_VEP_FIXTURE)

    async def run() -> vep_pb2.AnnotateResponse:
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client,
            _serving(http_client) as stub,
        ):
            return await stub.Annotate(vep_pb2.AnnotateRequest(variant='NM_001042492.3:c.3496G>C'))

    reply = asyncio.run(run())
    assert reply.most_severe_consequence == evidence_pb2.CONSEQUENCE_MISSENSE
    assert [t.mane_select for t in reply.transcripts if t.mane_select] == ['NM_001042492.3']
    assert reply.provenance[0].source == 'Ensembl VEP REST'
