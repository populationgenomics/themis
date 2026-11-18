"""The image's composition and entrypoint: every interface is registered and served behind the gate."""

from __future__ import annotations

import asyncio
import importlib
import json
import pathlib
from collections.abc import Awaitable, Callable

import grpc
import grpc.aio
import httpx2
import pytest
from google.cloud import storage
from grpc_health.v1 import health_pb2, health_pb2_grpc

from themis.rpc import gnomad_pb2, gnomad_pb2_grpc
from themis.services import evidence
from themis.services.evidence import __main__ as main_mod
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.literature import config as literature_config
from themis.services.evidence.tests import authz
from themis.services.evidence.upstreams import destinations
from themis.testing import auth as auth_fixture

_FIXTURE_INTERFACES: dict[str, tuple[str, dict[str, object]]] = {
    'THEMIS_LITERATURE_BACKEND': (
        'THEMIS_LITERATURE_FIXTURE',
        {'store': {}, 'discovery': {'records': [], 'entities': [], 'book_articles': []}},
    ),
    'THEMIS_VARIANT_BACKEND': ('THEMIS_VARIANT_FIXTURE', {'normalize': {}}),
    'THEMIS_VEP_BACKEND': ('THEMIS_VEP_FIXTURE', {'annotate': {}}),
    'THEMIS_GNOMAD_BACKEND': (
        'THEMIS_GNOMAD_FIXTURE',
        {'describe_variant': {'1-100-A-T': {'provenance': [{'source': 'gnomAD GraphQL'}]}}},
    ),
    'THEMIS_CLINVAR_BACKEND': ('THEMIS_CLINVAR_FIXTURE', {'describe_variant': {}, 'search_coding_span': {}}),
    'THEMIS_GENE_DISEASE_BACKEND': ('THEMIS_GENE_DISEASE_FIXTURE', {'describe_gene': {}}),
    'THEMIS_TRANSCRIPT_BACKEND': ('THEMIS_TRANSCRIPT_FIXTURE', {'get_structure': {}, 'assess_exon_relevance': {}}),
    'THEMIS_SPLICE_BACKEND': ('THEMIS_SPLICE_FIXTURE', {'predict_deltas': {}, 'predict_skip_outcome': {}}),
    'THEMIS_MAVEDB_BACKEND': ('THEMIS_MAVEDB_FIXTURE', {'describe_variant': {}}),
    'THEMIS_CSPEC_BACKEND': ('THEMIS_CSPEC_FIXTURE', {'list_specifications': {}}),
}

_OPTIONAL_LITERATURE_VARS = (
    'THEMIS_LITERATURE_CROSSWALK_INSTANCE',
    'THEMIS_LITERATURE_CROSSWALK_DATABASE',
    'THEMIS_LITERATURE_CROSSWALK_DB_USER',
    'THEMIS_LITERATURE_CONVERT_QUEUE',
    'THEMIS_LITERATURE_CONVERT_WORKER_URL',
    'THEMIS_LITERATURE_CONVERT_INVOKER_SA',
)


def _interface_names() -> set[str]:
    # Found by the file that registers, not by __init__.py: a subpackage without one still imports (an
    # implicit namespace package inside a regular parent) and would be invisible to a pkgutil scan.
    root = pathlib.Path(evidence.__path__[0])
    return {child.name for child in root.iterdir() if (child / 'interface.py').is_file()}


def _seed_entrypoint_env(monkeypatch: pytest.MonkeyPatch, *, project: str = auth_fixture.PROJECT) -> None:
    monkeypatch.setenv('PORT', '0')
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv(
        'THEMIS_EVIDENCE_FIXTURE_CONTEXTS',
        json.dumps({'good': {'analysis_id': 'ana', 'project': {'id': 'proj'}}}),
    )
    monkeypatch.setenv(
        'THEMIS_EVIDENCE_FIXTURE_CALLERS',
        json.dumps({'sandbox-job': auth_fixture.SANDBOX_JOB_EMAIL, 'web': auth_fixture.WEB_EMAIL}),
    )
    monkeypatch.setenv('THEMIS_GCP_PROJECT', project)
    for backend_var, (fixture_var, seed) in _FIXTURE_INTERFACES.items():
        monkeypatch.setenv(backend_var, 'fixture')
        monkeypatch.setenv(fixture_var, json.dumps(seed))


def _seed_live_literature_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bucket_name: str,
    gcs_endpoint: str,
) -> tuple[list[httpx2.AsyncClient], list[bool]]:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'live')
    monkeypatch.setenv('THEMIS_FULLTEXT_BUCKET', bucket_name)
    monkeypatch.setenv('THEMIS_LITERATURE_OPENALEX_API_KEY', 'an-openalex-key')
    monkeypatch.setenv('STORAGE_EMULATOR_HOST', gcs_endpoint)
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'themis-test')
    for var in _OPTIONAL_LITERATURE_VARS:
        monkeypatch.delenv(var, raising=False)

    clients: list[httpx2.AsyncClient] = []
    real_admitting_client = destinations.admitting_client

    def recording_admitting_client(*, timeout: httpx2.Timeout) -> httpx2.AsyncClient:
        client = real_admitting_client(timeout=timeout)
        clients.append(client)
        return client

    storage_closed: list[bool] = []
    real_storage_client = storage.Client

    class _TrackingStorageClient:
        def __init__(self) -> None:
            self._inner = real_storage_client()

        def bucket(self, name: str) -> storage.Bucket:
            return self._inner.bucket(name)

        def close(self) -> None:
            self._inner.close()
            storage_closed.append(True)

    monkeypatch.setattr(deps_mod.destinations, 'admitting_client', recording_admitting_client)
    monkeypatch.setattr(literature_config.storage, 'Client', _TrackingStorageClient)
    return clients, storage_closed


def _run_against_server[T](
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[grpc.aio.Channel], Awaitable[T]],
) -> T:
    bound_ports: list[int] = []
    real_bind_port = main_mod._bind_port

    def recording_bind_port(server: grpc.aio.Server) -> int:
        port = real_bind_port(server)
        bound_ports.append(port)
        return port

    results: list[T] = []

    async def verify_and_stop(server: grpc.aio.Server) -> None:
        try:
            async with grpc.aio.insecure_channel(f'[::1]:{bound_ports[0]}') as channel:
                results.append(await call(channel))
        finally:
            await server.stop(None)

    monkeypatch.setattr(main_mod, '_bind_port', recording_bind_port)
    monkeypatch.setattr(main_mod.serving, 'serve_until_stopped', verify_and_stop)
    asyncio.run(main_mod._serve())
    return results[0]


def test_every_interface_is_registered() -> None:
    # An interface absent from INTERFACES imports, type-checks and passes its own tests, yet answers
    # UNIMPLEMENTED in the deployed image.
    names = _interface_names()
    assert names  # a tree with no interface at all would pass vacuously
    missing = sorted(
        name
        for name in names
        if importlib.import_module(f'{evidence.__name__}.{name}.interface').register not in main_mod.INTERFACES
    )
    assert not missing, f'interfaces absent from the entrypoint registry: {missing}'


@pytest.mark.parametrize(
    'unset',
    [
        'THEMIS_AUTHORIZER_BACKEND',
        'THEMIS_EVIDENCE_FIXTURE_CONTEXTS',
        'THEMIS_EVIDENCE_FIXTURE_CALLERS',
        'THEMIS_GCP_PROJECT',
    ],
)
def test_serve_exits_when_an_authorizer_input_is_missing(monkeypatch: pytest.MonkeyPatch, unset: str) -> None:
    _seed_entrypoint_env(monkeypatch)
    monkeypatch.delenv(unset, raising=False)
    with pytest.raises(SystemExit, match=unset):
        asyncio.run(main_mod._serve())


def test_serve_exempts_health_checks_from_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_entrypoint_env(monkeypatch)

    async def check_health(channel: grpc.aio.Channel) -> health_pb2.HealthCheckResponse:
        # grpc_health ships no py.typed; dynamic RPC methods on HealthStub are invisible to pyright.
        return await health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest())  # pyright: ignore[reportAttributeAccessIssue]

    reply = _run_against_server(monkeypatch, check_health)
    assert reply.status == health_pb2.HealthCheckResponse.SERVING


def test_serve_answers_an_admitted_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_entrypoint_env(monkeypatch)

    async def call_admitted(channel: grpc.aio.Channel) -> gnomad_pb2.DescribeVariantResponse:
        request = gnomad_pb2.DescribeVariantRequest(gnomad_id='1-100-A-T', dataset='gnomad_r4')
        return await gnomad_pb2_grpc.GnomadStub(channel).DescribeVariant(request, metadata=authz.AGENT)

    reply = _run_against_server(monkeypatch, call_admitted)
    assert reply.provenance[0].source == 'gnomAD GraphQL'


def test_serve_rejects_an_unauthenticated_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_entrypoint_env(monkeypatch)

    async def call_unauthenticated(channel: grpc.aio.Channel) -> grpc.StatusCode:
        request = gnomad_pb2.DescribeVariantRequest(gnomad_id='1-100-A-T', dataset='gnomad_r4')
        with pytest.raises(grpc.aio.AioRpcError) as denied:
            await gnomad_pb2_grpc.GnomadStub(channel).DescribeVariant(request)
        return denied.value.code()

    assert _run_against_server(monkeypatch, call_unauthenticated) is grpc.StatusCode.UNAUTHENTICATED


def test_serve_denies_an_unadmitted_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_entrypoint_env(monkeypatch)

    async def call_unadmitted(channel: grpc.aio.Channel) -> grpc.StatusCode:
        request = gnomad_pb2.DescribeVariantRequest(gnomad_id='1-100-A-T', dataset='gnomad_r4')
        with pytest.raises(grpc.aio.AioRpcError) as denied:
            await gnomad_pb2_grpc.GnomadStub(channel).DescribeVariant(request, metadata=authz.WEB)
        return denied.value.code()

    assert _run_against_server(monkeypatch, call_unadmitted) is grpc.StatusCode.PERMISSION_DENIED


def test_serve_completes_caller_identities_against_the_configured_project(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_entrypoint_env(monkeypatch, project='another-project')

    async def call_wrong_project(channel: grpc.aio.Channel) -> grpc.StatusCode:
        request = gnomad_pb2.DescribeVariantRequest(gnomad_id='1-100-A-T', dataset='gnomad_r4')
        with pytest.raises(grpc.aio.AioRpcError) as denied:
            await gnomad_pb2_grpc.GnomadStub(channel).DescribeVariant(request, metadata=authz.AGENT)
        return denied.value.code()

    assert _run_against_server(monkeypatch, call_wrong_project) is grpc.StatusCode.PERMISSION_DENIED


def test_serve_closes_the_shared_client_and_interface_stack_resources_on_stop(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
) -> None:
    assert gcs_bucket.name is not None
    _seed_entrypoint_env(monkeypatch)
    clients, storage_closed = _seed_live_literature_env(
        monkeypatch, bucket_name=gcs_bucket.name, gcs_endpoint=gcs_endpoint
    )

    async def observe_open(_channel: grpc.aio.Channel) -> None:
        assert len(clients) == 1
        assert clients[0].is_closed is False
        assert storage_closed == []

    _run_against_server(monkeypatch, observe_open)
    assert clients[0].is_closed is True
    assert storage_closed == [True]


def test_serve_unwinds_earlier_interface_resources_when_a_later_interface_exits(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
) -> None:
    assert gcs_bucket.name is not None
    _seed_entrypoint_env(monkeypatch)
    clients, storage_closed = _seed_live_literature_env(
        monkeypatch, bucket_name=gcs_bucket.name, gcs_endpoint=gcs_endpoint
    )
    monkeypatch.setenv('THEMIS_CSPEC_BACKEND', 'bogus')

    with pytest.raises(SystemExit, match='bogus'):
        asyncio.run(main_mod._serve())
    assert clients[0].is_closed is True
    assert storage_closed == [True]
