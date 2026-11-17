"""The transcript interface's env contract and gRPC registration seam."""

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
from themis.rpc import transcript_pb2, transcript_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.tests import authz
from themis.services.evidence.transcript import interface
from themis.testing import in_process_grpc

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_STRUCTURE_FIXTURE = json.loads((_FIXTURES / 'transcript_structure.json').read_bytes())
_SEED = json.dumps(
    {
        'get_structure': {'NM_000059.4:GRCh38': {'gene': 'BRCA2'}},
        'assess_exon_relevance': {'BRCA2:NM_000059.4:11': {'in_mane_select': True}},
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
async def _serving(
    http_client: httpx2.AsyncClient | None = None,
) -> AsyncIterator[transcript_pb2_grpc.TranscriptAsyncStub]:
    async with _deps_context(http_client) as deps:
        register = functools.partial(interface.register, deps=deps)
        async with authz.gated(register) as channel:
            yield transcript_pb2_grpc.TranscriptStub(channel)


def _start_server() -> None:
    async def run() -> None:
        async with _serving():
            pass

    asyncio.run(run())


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_TRANSCRIPT_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_TRANSCRIPT_BACKEND'):
        _start_server()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_TRANSCRIPT_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _start_server()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_TRANSCRIPT_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_TRANSCRIPT_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_TRANSCRIPT_FIXTURE'):
        _start_server()


def test_fixture_selector_serves_seeded_get_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_TRANSCRIPT_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_TRANSCRIPT_FIXTURE', _SEED)

    async def run() -> transcript_pb2.GetStructureResponse:
        async with _serving() as stub:
            return await stub.GetStructure(
                transcript_pb2.GetStructureRequest(transcript='NM_000059.4', genome_build='GRCh38')
            )

    assert asyncio.run(run()).gene == 'BRCA2'


def test_fixture_selector_serves_seeded_assess_exon_relevance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_TRANSCRIPT_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_TRANSCRIPT_FIXTURE', _SEED)

    async def run() -> transcript_pb2.AssessExonRelevanceResponse:
        async with _serving() as stub:
            return await stub.AssessExonRelevance(
                transcript_pb2.AssessExonRelevanceRequest(
                    gene='BRCA2',
                    transcript='NM_000059.4',
                    exon=11,
                    in_mane_select=True,
                    in_mane_plus_clinical=False,
                )
            )

    assert asyncio.run(run()).in_mane_select is True


@pytest.mark.parametrize(
    'call',
    [
        pytest.param(
            lambda s: s.GetStructure(
                transcript_pb2.GetStructureRequest(transcript='NM_007294.4', genome_build='GRCh38')
            ),
            id='get_structure',
        ),
        pytest.param(
            lambda s: s.AssessExonRelevance(
                transcript_pb2.AssessExonRelevanceRequest(
                    gene='BRCA2',
                    transcript='NM_000059.4',
                    exon=12,
                    in_mane_select=True,
                    in_mane_plus_clinical=False,
                )
            ),
            id='assess_exon_relevance',
        ),
    ],
)
def test_fixture_selector_reports_unseeded_keys_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[transcript_pb2_grpc.TranscriptAsyncStub], Awaitable[object]],
) -> None:
    monkeypatch.setenv('THEMIS_TRANSCRIPT_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_TRANSCRIPT_FIXTURE', _SEED)

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
            lambda s: s.GetStructure(
                transcript_pb2.GetStructureRequest(transcript='NM_000059.4', genome_build='GRCh38')
            ),
            id='get_structure',
        ),
        pytest.param(
            lambda s: s.AssessExonRelevance(
                transcript_pb2.AssessExonRelevanceRequest(
                    gene='BRCA2',
                    transcript='NM_000059.4',
                    exon=11,
                    in_mane_select=True,
                    in_mane_plus_clinical=False,
                )
            ),
            id='assess_exon_relevance',
        ),
    ],
)
def test_register_denies_an_unadmitted_caller(
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[transcript_pb2_grpc.TranscriptAsyncStub], Awaitable[object]],
) -> None:
    monkeypatch.setenv('THEMIS_TRANSCRIPT_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_TRANSCRIPT_FIXTURE', _SEED)

    async def run() -> None:
        async with (
            _deps_context() as deps,
            in_process_grpc.serving(
                functools.partial(interface.register, deps=deps),
                interceptors=(authz.InjectMetadata(authz.WEB),),
                server_interceptors=authz.server_interceptors(),
            ) as channel,
        ):
            await call(transcript_pb2_grpc.TranscriptStub(channel))

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED


def test_live_selector_serves_over_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_TRANSCRIPT_BACKEND', 'live')
    metadata = {
        'metadata': {
            'variantvalidator_version': '4.0.1.dev7+gbdab9c72f',
            'vvdb_version': 'vvdb_2025_3',
            'vvta_version': 'vvta_2025_02',
        }
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == '/hello/':
            return httpx2.Response(200, json=metadata)
        return httpx2.Response(200, json=_STRUCTURE_FIXTURE)

    async def run() -> transcript_pb2.GetStructureResponse:
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client,
            _serving(http_client) as stub,
        ):
            return await stub.GetStructure(
                transcript_pb2.GetStructureRequest(transcript='NM_001042492.3', genome_build='GRCh38')
            )

    reply = asyncio.run(run())
    assert reply.gene == 'NF1'
