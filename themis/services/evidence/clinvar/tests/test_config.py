"""The clinvar interface's env contract and gRPC registration seam."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import pathlib
from collections.abc import AsyncIterator, Awaitable, Callable

import clinvar_proto
import defusedxml.ElementTree
import grpc
import grpc.aio
import httpx2
import pytest
from google.protobuf import json_format

from themis.common import authored_status
from themis.rpc import clinvar_pb2, clinvar_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.clinvar import interface
from themis.services.evidence.tests import authz
from themis.services.evidence.upstreams import clinvar as clinvar_upstream
from themis.testing import in_process_grpc

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_UPSTREAM_FIXTURE = json.loads((_FIXTURES / 'clinvar.json').read_bytes())
_VCV_XML = (_FIXTURES / 'clinvar_vcv.xml').read_bytes()
_VCV = 'VCV001731988'


def _seeded_archive() -> dict[str, object]:
    """The committed VCV XML in the JSON form a seed carries, over the adapter's own converter path."""
    root = defusedxml.ElementTree.fromstring(_VCV_XML)
    archive = root.find('VariationArchive')
    assert archive is not None, 'the committed VCV fixture carries no VariationArchive'
    return json_format.MessageToDict(clinvar_proto.xml_converter.VariationArchiveType(archive))


_SEED = json.dumps(
    {
        'describe_variant': {
            f'{_VCV}:BRCA1': {'this_variant': {'clinvar_id': _VCV}, 'variationArchive': _seeded_archive()}
        },
        'search_coding_span': {'NM_007294.4:5074:5076': {'transcript': 'NM_007294.4'}},
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
async def _serving(http_client: httpx2.AsyncClient | None = None) -> AsyncIterator[clinvar_pb2_grpc.ClinVarAsyncStub]:
    async with _deps_context(http_client) as deps:
        register = functools.partial(interface.register, deps=deps)
        async with authz.gated(register) as channel:
            yield clinvar_pb2_grpc.ClinVarStub(channel)


def _start_server() -> None:
    async def run() -> None:
        async with _serving():
            pass

    asyncio.run(run())


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_CLINVAR_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_CLINVAR_BACKEND'):
        _start_server()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CLINVAR_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _start_server()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CLINVAR_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_CLINVAR_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_CLINVAR_FIXTURE'):
        _start_server()


def test_fixture_selector_serves_seeded_describe_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CLINVAR_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_CLINVAR_FIXTURE', _SEED)

    async def run() -> clinvar_pb2.DescribeVariantResponse:
        async with _serving() as stub:
            return await stub.DescribeVariant(
                clinvar_pb2.DescribeVariantRequest(vcv=_VCV, gene='BRCA1', max_pool_records=50)
            )

    described = asyncio.run(run())
    assert described.this_variant.clinvar_id == _VCV
    assert described.variation_archive.accession == _VCV
    assert len(described.variation_archive.classified_record.clinical_assertion_list) == 2


def test_fixture_selector_serves_seeded_search_coding_span(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CLINVAR_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_CLINVAR_FIXTURE', _SEED)

    async def run() -> clinvar_pb2.SearchCodingSpanResponse:
        async with _serving() as stub:
            return await stub.SearchCodingSpan(
                clinvar_pb2.SearchCodingSpanRequest(
                    transcript='NM_007294.4', cds_start=5074, cds_end=5076, max_records=50
                )
            )

    assert asyncio.run(run()).transcript == 'NM_007294.4'


@pytest.mark.parametrize(
    'call',
    [
        pytest.param(
            lambda s: s.DescribeVariant(
                clinvar_pb2.DescribeVariantRequest(vcv=_VCV, gene='BRCA2', max_pool_records=50)
            ),
            id='describe_variant',
        ),
        pytest.param(
            lambda s: s.SearchCodingSpan(
                clinvar_pb2.SearchCodingSpanRequest(
                    transcript='NM_000059.4', cds_start=5074, cds_end=5076, max_records=50
                )
            ),
            id='search_coding_span',
        ),
    ],
)
def test_fixture_selector_reports_unseeded_keys_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[clinvar_pb2_grpc.ClinVarAsyncStub], Awaitable[object]],
) -> None:
    monkeypatch.setenv('THEMIS_CLINVAR_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_CLINVAR_FIXTURE', _SEED)

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
            lambda s: s.DescribeVariant(
                clinvar_pb2.DescribeVariantRequest(vcv=_VCV, gene='BRCA1', max_pool_records=50)
            ),
            id='describe_variant',
        ),
        pytest.param(
            lambda s: s.SearchCodingSpan(
                clinvar_pb2.SearchCodingSpanRequest(
                    transcript='NM_007294.4', cds_start=5074, cds_end=5076, max_records=50
                )
            ),
            id='search_coding_span',
        ),
    ],
)
def test_register_denies_an_unadmitted_caller(
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[clinvar_pb2_grpc.ClinVarAsyncStub], Awaitable[object]],
) -> None:
    monkeypatch.setenv('THEMIS_CLINVAR_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_CLINVAR_FIXTURE', _SEED)

    async def run() -> None:
        async with (
            _deps_context() as deps,
            in_process_grpc.serving(
                functools.partial(interface.register, deps=deps),
                interceptors=(authz.InjectMetadata(authz.WEB),),
                server_interceptors=authz.server_interceptors(),
            ) as channel,
        ):
            await call(clinvar_pb2_grpc.ClinVarStub(channel))

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED


def test_live_selector_serves_over_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CLINVAR_BACKEND', 'live')
    monkeypatch.setattr(clinvar_upstream, '_RATE_LIMIT_DELAY_S', 0)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/efetch.fcgi'):
            return httpx2.Response(200, content=_VCV_XML)
        if request.url.path.endswith('/esearch.fcgi'):
            return httpx2.Response(200, json=_UPSTREAM_FIXTURE['esearch_gene'])
        return httpx2.Response(200, json=_UPSTREAM_FIXTURE['esummary_gene'])

    async def run() -> clinvar_pb2.DescribeVariantResponse:
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client,
            _serving(http_client) as stub,
        ):
            return await stub.DescribeVariant(
                clinvar_pb2.DescribeVariantRequest(vcv=_VCV, gene='NF1', max_pool_records=5)
            )

    reply = asyncio.run(run())
    assert reply.this_variant.clinvar_id == _VCV
