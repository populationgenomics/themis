"""Tests for the sheaf entrypoint: backend, limits, and gate driven over ``_running`` / ``_serve`` on loopback."""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import queue
import threading
import typing
import urllib.parse
from collections import abc

import grpc
import pytest
from google.auth import credentials
from google.cloud import storage
from google.protobuf import empty_pb2
from grpc_health.v1 import health_pb2, health_pb2_grpc
from opentelemetry.sdk import metrics as sdk_metrics

from themis import sheaf
from themis.rpc import sandbox_options_pb2, sheaf_pb2, sheaf_pb2_grpc
from themis.services import sheaf as sheaf_service
from themis.services.sheaf import __main__ as main_mod
from themis.services.sheaf import servicer as servicer_mod
from themis.services.sheaf.tests import conftest
from themis.sheaf.backends import gcs
from themis.telemetry import metrics as telemetry_metrics
from themis.telemetry import tracing
from themis.testing import auth as auth_fixture
from themis.testing import gate

_LIMIT_ENV = {
    'THEMIS_SHEAF_MAX_PUBLISH_BYTES': '1048576',
    'THEMIS_SHEAF_MAX_REFS': '256',
    'THEMIS_SHEAF_MAX_DOCUMENT_BYTES': '65536',
    'THEMIS_SHEAF_PACK_URL_LIFETIME_SECONDS': '300',
}


def _local_env(monkeypatch: pytest.MonkeyPatch, root: pathlib.Path) -> None:
    monkeypatch.setenv('PORT', '0')
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_GCP_PROJECT', auth_fixture.PROJECT)
    monkeypatch.setenv(
        'THEMIS_SHEAF_FIXTURE_CONTEXTS',
        json.dumps({'tok': {'analysis_id': 'ana-1', 'project': {'id': 'proj-1'}}}),
    )
    monkeypatch.setenv(
        'THEMIS_SHEAF_FIXTURE_CALLERS',
        json.dumps(
            {
                'sandbox-job': auth_fixture.SANDBOX_JOB_EMAIL,
                'web': auth_fixture.WEB_EMAIL,
            }
        ),
    )
    monkeypatch.setenv('THEMIS_SHEAF_BACKEND', 'local')
    monkeypatch.setenv('THEMIS_SHEAF_LOCAL_ROOT', str(root))
    for var, value in _LIMIT_ENV.items():
        monkeypatch.setenv(var, value)
    monkeypatch.delenv(tracing.SAMPLE_RATIO_VAR, raising=False)


@contextlib.contextmanager
def _serving() -> abc.Iterator[grpc.Channel]:
    """Run ``main_mod._running()`` on a background thread so stream-unary ``Publish`` uses the synchronous stub."""
    started: queue.Queue[int | BaseException] = queue.Queue(maxsize=1)
    stopping: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    async def serve() -> None:
        async with main_mod._running() as (_server, port):
            stopped = asyncio.Event()
            stopping.append((asyncio.get_running_loop(), stopped))
            started.put(port)
            await stopped.wait()

    def run_thread() -> None:
        try:
            asyncio.run(serve())
        except BaseException as exc:
            if stopping:
                raise
            started.put(exc)

    thread = threading.Thread(target=run_thread, name='sheaf-main-test', daemon=True)
    thread.start()
    outcome = started.get()
    if isinstance(outcome, BaseException):
        raise outcome
    try:
        with grpc.insecure_channel(f'127.0.0.1:{outcome}') as channel:
            yield channel
    finally:
        loop, stopped = stopping[0]
        if not loop.is_closed():
            loop.call_soon_threadsafe(stopped.set)
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError('the sheaf server thread did not stop within 10s')


def test_the_server_reports_serving_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    with _serving() as channel:
        response = health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest())  # pyright: ignore[reportAttributeAccessIssue]
    assert response.status == health_pb2.HealthCheckResponse.SERVING


def test_the_server_rejects_an_unauthenticated_rpc(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    with (
        _serving() as channel,
        pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.UNAUTHENTICATED)),
    ):
        sheaf_pb2_grpc.SheafStub(channel).ReadRefDoc(empty_pb2.Empty())


def test_the_server_denies_a_caller_the_contract_does_not_admit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    agent = (*auth_fixture.SANDBOX_JOB, *auth_fixture.claim(sandbox_options_pb2.CALLING_AS_AGENT_SESSION, 'tok'))
    with (
        _serving() as channel,
        pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.PERMISSION_DENIED)),
    ):
        sheaf_pb2_grpc.SheafStub(channel).ReadRefDoc(empty_pb2.Empty(), metadata=agent)


def test_the_server_routes_a_seeded_session_to_its_repository_under_the_local_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # Offline, THEMIS_TRACE_SAMPLE_RATIO is unset; THEMIS_SHEAF_FIXTURE_CONTEXTS and _CALLERS seed the gate,
    # THEMIS_SHEAF_LOCAL_ROOT roots the local backend, and a pack under THEMIS_SHEAF_MAX_PUBLISH_BYTES lands.
    root = tmp_path / 'sheaf'
    _local_env(monkeypatch, root)
    worker = (*auth_fixture.SANDBOX_JOB, *auth_fixture.claim(sandbox_options_pb2.CALLING_AS_WORKER_SESSION, 'tok'))
    messages = conftest.stream(
        conftest.intent(0, {conftest.REF: (None, conftest.SHA_A)}, packs=[conftest.PACK_1]),
        [conftest.PACK_1],
    )

    with _serving() as channel:
        sheaf_pb2_grpc.SheafStub(channel).Publish(iter(messages), metadata=worker)

    store = sheaf.Store(sheaf.LocalBackend(root), 'ana-1')
    assert store.read().tip(conftest.REF) == conftest.SHA_A
    assert store.fetch_pack(sheaf.pack_id(conftest.PACK_1)) == conftest.PACK_1


@pytest.mark.parametrize(
    ('var', 'ceiling', 'packs'),
    [
        pytest.param('THEMIS_SHEAF_MAX_PUBLISH_BYTES', '10', [conftest.PACK_1], id='publish-bytes'),
        pytest.param('THEMIS_SHEAF_MAX_REFS', '1', [], id='refs'),
        pytest.param('THEMIS_SHEAF_MAX_DOCUMENT_BYTES', '10', [], id='document-bytes'),
    ],
)
def test_the_server_enforces_each_publish_ceiling_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    var: str,
    ceiling: str,
    packs: list[bytes],
) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    monkeypatch.setenv(var, ceiling)
    worker = (*auth_fixture.SANDBOX_JOB, *auth_fixture.claim(sandbox_options_pb2.CALLING_AS_WORKER_SESSION, 'tok'))
    messages = conftest.stream(
        conftest.intent(0, {conftest.REF: (None, conftest.SHA_A)}, packs=packs),
        packs,
    )

    with (
        _serving() as channel,
        pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.RESOURCE_EXHAUSTED)),
    ):
        sheaf_pb2_grpc.SheafStub(channel).Publish(iter(messages), metadata=worker)


class _OfflineIamSigner(gcs.IamSigner):
    """Signs deterministically offline so V4 URL generation exercises account and lifetime wiring without IAM."""

    @typing.override
    def sign_bytes(self, message: bytes) -> bytes:
        del message
        return b'offline-signature'


def test_gcs_backend_keys_repositories_at_the_bucket_root_with_configured_signing(
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    assert gcs_bucket.name is not None
    _local_env(monkeypatch, tmp_path / 'unused')
    monkeypatch.setenv('THEMIS_SHEAF_BACKEND', 'gcs')
    monkeypatch.setenv('THEMIS_SHEAF_BUCKET', gcs_bucket.name)
    monkeypatch.setenv('THEMIS_SHEAF_SIGNING_ACCOUNT', 'themis-sheaf@example.iam.gserviceaccount.com')
    monkeypatch.setenv('THEMIS_SHEAF_PACK_URL_LIFETIME_SECONDS', '420')
    monkeypatch.setenv(tracing.SAMPLE_RATIO_VAR, '0')
    real_client = storage.Client
    monkeypatch.setattr(
        storage,
        'Client',
        lambda: real_client(
            project='themis-test',
            credentials=credentials.AnonymousCredentials(),
            client_options={'api_endpoint': gcs_endpoint},
        ),
    )
    monkeypatch.setattr(gcs, 'IamSigner', _OfflineIamSigner)
    monkeypatch.setattr(
        telemetry_metrics,
        'service_meter_provider',
        lambda _service: sdk_metrics.MeterProvider(shutdown_on_exit=False),
    )
    worker = (*auth_fixture.SANDBOX_JOB, *auth_fixture.claim(sandbox_options_pb2.CALLING_AS_WORKER_SESSION, 'tok'))
    web = (*auth_fixture.WEB, *auth_fixture.claim(sandbox_options_pb2.CALLING_AS_SELF, 'tok'))
    pack_id = sheaf.pack_id(conftest.PACK_1)
    messages = conftest.stream(
        conftest.intent(0, {conftest.REF: (None, conftest.SHA_A)}, packs=[conftest.PACK_1]),
        [conftest.PACK_1],
    )

    with _serving() as channel:
        stub = sheaf_pb2_grpc.SheafStub(channel)
        stub.Publish(iter(messages), metadata=worker)
        signed = stub.SignPackUrls(sheaf_pb2.SignPackUrlsRequest(pack_ids=[pack_id]), metadata=web)

    [pack] = signed.packs
    parsed = urllib.parse.urlsplit(pack.url)
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == f'/{gcs_bucket.name}/ana-1/packs/{pack_id}.pack'
    assert query['X-Goog-Expires'] == ['420']
    assert query['X-Goog-Credential'][0].startswith('themis-sheaf@example.iam.gserviceaccount.com/')


def test_no_sheaf_module_constructs_a_server() -> None:
    gate.assert_no_module_constructs_a_server(sheaf_service)


def test_the_sheaf_servicer_reads_no_call_metadata() -> None:
    gate.assert_servicer_reads_no_call_metadata(servicer_mod)


def test_backend_requires_a_selector(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    monkeypatch.delenv('THEMIS_SHEAF_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_SHEAF_BACKEND'):
        asyncio.run(main_mod._serve())


def test_backend_rejects_an_unknown_selector(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    monkeypatch.setenv('THEMIS_SHEAF_BACKEND', 'memory')
    with pytest.raises(SystemExit, match='memory'):
        asyncio.run(main_mod._serve())


def test_local_backend_requires_its_root(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    monkeypatch.delenv('THEMIS_SHEAF_LOCAL_ROOT', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_SHEAF_LOCAL_ROOT'):
        asyncio.run(main_mod._serve())


def test_gcs_backend_requires_its_bucket(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    monkeypatch.setenv('THEMIS_SHEAF_BACKEND', 'gcs')
    monkeypatch.delenv('THEMIS_SHEAF_BUCKET', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_SHEAF_BUCKET'):
        asyncio.run(main_mod._serve())


@pytest.mark.parametrize('missing', sorted(_LIMIT_ENV))
def test_every_limit_is_required(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, missing: str) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit, match=missing):
        asyncio.run(main_mod._serve())


@pytest.mark.parametrize('bad', ['0', '-1', 'lots', '1.5', ''])
def test_a_limit_that_is_not_a_positive_integer_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, bad: str
) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    monkeypatch.setenv('THEMIS_SHEAF_MAX_REFS', bad)
    with pytest.raises(SystemExit, match='THEMIS_SHEAF_MAX_REFS'):
        asyncio.run(main_mod._serve())


def test_a_url_lifetime_past_what_a_v4_signature_carries_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    monkeypatch.setenv('THEMIS_SHEAF_PACK_URL_LIFETIME_SECONDS', str(7 * 24 * 60 * 60 + 1))
    with pytest.raises(SystemExit, match='pack_url_lifetime_seconds'):
        asyncio.run(main_mod._serve())


def test_gcs_backend_requires_the_account_it_signs_as(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    monkeypatch.setenv('THEMIS_SHEAF_BACKEND', 'gcs')
    monkeypatch.setenv('THEMIS_SHEAF_BUCKET', 'a-bucket')
    monkeypatch.delenv('THEMIS_SHEAF_SIGNING_ACCOUNT', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_SHEAF_SIGNING_ACCOUNT'):
        asyncio.run(main_mod._serve())


def test_the_gcs_backend_requires_a_sample_ratio(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    _local_env(monkeypatch, tmp_path / 'sheaf')
    monkeypatch.setenv('THEMIS_SHEAF_BACKEND', 'gcs')
    monkeypatch.setenv('THEMIS_SHEAF_BUCKET', 'a-bucket')
    monkeypatch.setenv('THEMIS_SHEAF_SIGNING_ACCOUNT', 'themis-sheaf@example.iam.gserviceaccount.com')
    monkeypatch.setenv('STORAGE_EMULATOR_HOST', 'http://127.0.0.1:1')
    monkeypatch.delenv(tracing.SAMPLE_RATIO_VAR, raising=False)
    with pytest.raises(SystemExit, match=tracing.SAMPLE_RATIO_VAR):
        asyncio.run(main_mod._serve())
