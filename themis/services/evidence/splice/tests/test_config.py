"""The splice interface's env contract and gRPC registration seam."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import pathlib
from collections.abc import AsyncIterator, Awaitable, Callable

import grpc
import grpc.aio
import httpx2
import pytest

from themis.common import authored_status
from themis.rpc import splice_pb2, splice_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.splice import interface
from themis.services.evidence.tests import authz
from themis.testing import in_process_grpc

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_SPLICE_FIXTURE = json.loads((_FIXTURES / 'spliceai.json').read_bytes())
_SEED = json.dumps(
    {
        'predict_deltas': {'13-32332343-A-G': {'spliceai_loss': 0.5}},
        'predict_skip_outcome': {'NM_000059.4:GRCh38:exon:11': {'gene': 'BRCA2'}},
    }
)


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
async def _serving(http_client: httpx2.AsyncClient | None = None) -> AsyncIterator[splice_pb2_grpc.SpliceAsyncStub]:
    async with _deps_context(http_client) as deps:
        register = functools.partial(interface.register, deps=deps)
        async with authz.gated(register) as channel:
            yield splice_pb2_grpc.SpliceStub(channel)


def _start_server() -> None:
    async def run() -> None:
        async with _serving():
            pass

    asyncio.run(run())


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_SPLICE_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_SPLICE_BACKEND'):
        _start_server()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_SPLICE_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _start_server()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_SPLICE_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_SPLICE_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_SPLICE_FIXTURE'):
        _start_server()


def test_fixture_selector_serves_seeded_predict_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_SPLICE_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_SPLICE_FIXTURE', _SEED)

    async def run() -> splice_pb2.PredictDeltasResponse:
        async with _serving() as stub:
            return await stub.PredictDeltas(splice_pb2.PredictDeltasRequest(variant='13-32332343-A-G'))

    assert asyncio.run(run()).spliceai_loss == 0.5


def test_fixture_selector_serves_seeded_predict_skip_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_SPLICE_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_SPLICE_FIXTURE', _SEED)

    async def run() -> splice_pb2.PredictSkipOutcomeResponse:
        async with _serving() as stub:
            return await stub.PredictSkipOutcome(
                splice_pb2.PredictSkipOutcomeRequest(transcript='NM_000059.4', genome_build='GRCh38', exon=11)
            )

    assert asyncio.run(run()).gene == 'BRCA2'


@pytest.mark.parametrize(
    'call',
    [
        pytest.param(
            lambda s: s.PredictDeltas(splice_pb2.PredictDeltasRequest(variant='13-32332343-A-T')),
            id='predict_deltas',
        ),
        pytest.param(
            lambda s: s.PredictSkipOutcome(
                splice_pb2.PredictSkipOutcomeRequest(transcript='NM_000059.4', genome_build='GRCh38', exon=12)
            ),
            id='predict_skip_outcome',
        ),
    ],
)
def test_fixture_selector_reports_unseeded_keys_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[splice_pb2_grpc.SpliceAsyncStub], Awaitable[object]],
) -> None:
    monkeypatch.setenv('THEMIS_SPLICE_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_SPLICE_FIXTURE', _SEED)

    async def run() -> None:
        async with _serving() as stub:
            await call(stub)

    with pytest.raises(grpc.aio.AioRpcError) as unseeded:
        asyncio.run(run())
    assert unseeded.value.code() is grpc.StatusCode.NOT_FOUND
    assert authored_status.authored(unseeded.value)
    assert 'seeded' in (unseeded.value.details() or '')


@pytest.mark.parametrize(
    'call',
    [
        pytest.param(
            lambda s: s.PredictDeltas(splice_pb2.PredictDeltasRequest(variant='13-32332343-A-G')),
            id='predict_deltas',
        ),
        pytest.param(
            lambda s: s.PredictSkipOutcome(
                splice_pb2.PredictSkipOutcomeRequest(transcript='NM_000059.4', genome_build='GRCh38', exon=11)
            ),
            id='predict_skip_outcome',
        ),
    ],
)
def test_register_denies_an_unadmitted_caller(
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[splice_pb2_grpc.SpliceAsyncStub], Awaitable[object]],
) -> None:
    monkeypatch.setenv('THEMIS_SPLICE_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_SPLICE_FIXTURE', _SEED)

    async def run() -> None:
        async with (
            _deps_context() as deps,
            in_process_grpc.serving(
                functools.partial(interface.register, deps=deps),
                interceptors=(authz.InjectMetadata(authz.WEB),),
                server_interceptors=authz.server_interceptors(),
            ) as channel,
        ):
            await call(splice_pb2_grpc.SpliceStub(channel))

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED


def test_live_selector_serves_over_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_SPLICE_BACKEND', 'live')

    def handler(request: httpx2.Request) -> httpx2.Response:
        if '/spliceai/' in request.url.path:
            return httpx2.Response(200, json=_SPLICE_FIXTURE['spliceai'])
        return httpx2.Response(200, json=_SPLICE_FIXTURE['pangolin'])

    async def run() -> splice_pb2.PredictDeltasResponse:
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client,
            _serving(http_client) as stub,
        ):
            return await stub.PredictDeltas(splice_pb2.PredictDeltasRequest(variant='8-140300616-T-G'))

    reply = asyncio.run(run())
    assert reply.spliceai_loss == 0.83
    assert reply.pangolin_loss == 0.85
