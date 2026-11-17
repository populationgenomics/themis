"""The server entrypoint: backend, staleness limit, and gate driven over ``_running`` / ``_serve`` on loopback."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import abc

import grpc
import grpc.aio
import pytest
from google.cloud import secretmanager
from google.cloud.sql import connector
from grpc_health.v1 import health_pb2, health_pb2_grpc

from themis.clients.auth import claim as claim_mod
from themis.rpc import auth_pb2, auth_pb2_grpc, sandbox_options_pb2
from themis.services import auth as auth_service
from themis.services.auth import __main__ as main_mod
from themis.services.auth import acl_holder, fixture
from themis.services.auth import backend as auth_backend
from themis.services.auth import servicer as servicer_mod
from themis.services.auth.tests import harness
from themis.telemetry import startup, tracing
from themis.testing import auth as auth_fixture
from themis.testing import gate

_BINDING = {'analysis_id': 'a1', 'project_id': 'asd'}
_LIVE_ENV = {
    'THEMIS_BACKEND': 'cloudsql',
    'THEMIS_SQL_CONNECTION_NAME': 'proj:region:instance',
    'THEMIS_SQL_DATABASE': 'themis',
    'THEMIS_DB_USER': 'themis-auth@proj.iam',
    'THEMIS_ACL_SECRET': 'projects/themis-proj/secrets/acl',
}


def _fixture_env(monkeypatch: pytest.MonkeyPatch, bindings: object = None) -> None:
    monkeypatch.setenv('PORT', '0')
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    seeded = {} if bindings is None else bindings
    monkeypatch.setenv(fixture.BINDINGS_VAR, seeded if isinstance(seeded, str) else json.dumps(seeded))
    monkeypatch.setenv(fixture.ACL_VAR, harness.acl_version().decode())
    monkeypatch.setenv(main_mod.STALENESS_LIMIT_VAR, '1800')
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_GCP_PROJECT', auth_fixture.PROJECT)
    monkeypatch.setenv(
        'THEMIS_AUTH_FIXTURE_CALLERS',
        json.dumps(
            {
                'clu': auth_fixture.CLU_EMAIL,
                'web': auth_fixture.WEB_EMAIL,
                'sandbox-job': auth_fixture.SANDBOX_JOB_EMAIL,
            }
        ),
    )
    monkeypatch.delenv('THEMIS_AUTH_URL', raising=False)
    monkeypatch.delenv(tracing.SAMPLE_RATIO_VAR, raising=False)


def _live_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    # Neither client may reach Google Cloud: the connector dials and the Secret Manager client finds credentials.
    class _Connector:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

    class _SecretManagerClient:
        pass

    monkeypatch.setattr(connector, 'Connector', _Connector)
    monkeypatch.setattr(secretmanager, 'SecretManagerServiceClient', _SecretManagerClient)
    monkeypatch.setenv(main_mod.STALENESS_LIMIT_VAR, '1800')
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_GCP_PROJECT', auth_fixture.PROJECT)
    monkeypatch.setenv('THEMIS_AUTH_FIXTURE_CALLERS', '{}')
    for name, value in {**_LIVE_ENV, **overrides}.items():
        monkeypatch.setenv(name, value)


@contextlib.asynccontextmanager
async def _serving(*, monotonic: abc.Callable[[], float] = time.monotonic) -> abc.AsyncIterator[grpc.aio.Channel]:
    async with (
        main_mod._running(startup.StartupClock(), monotonic=monotonic) as (_server, port, _acl),
        grpc.aio.insecure_channel(f'127.0.0.1:{port}') as channel,
    ):
        yield channel


def test_the_server_reports_serving_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(monkeypatch)

    async def run() -> health_pb2.HealthCheckResponse.ServingStatus:
        async with _serving() as channel:
            response = await health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest())  # pyright: ignore[reportAttributeAccessIssue]
            return response.status

    assert asyncio.run(run()) == health_pb2.HealthCheckResponse.SERVING


def test_the_server_rejects_an_unauthenticated_get_access_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(monkeypatch)

    async def run() -> grpc.StatusCode:
        async with _serving() as channel:
            try:
                await auth_pb2_grpc.AuthStub(channel).GetAccess(auth_pb2.GetAccessRequest(email=harness.JOHN))
            except grpc.aio.AioRpcError as exc:
                return exc.code()
            return grpc.StatusCode.OK

    assert asyncio.run(run()) is grpc.StatusCode.UNAUTHENTICATED


def test_the_server_denies_a_caller_the_get_access_contract_does_not_admit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture_env(monkeypatch, {'good': _BINDING})

    async def run() -> grpc.StatusCode:
        async with _serving() as channel:
            try:
                await auth_pb2_grpc.AuthStub(channel).GetAccess(
                    auth_pb2.GetAccessRequest(email=harness.JOHN),
                    metadata=claim_mod.pairs(auth_fixture.AGENT),
                )
            except grpc.aio.AioRpcError as exc:
                return exc.code()
            return grpc.StatusCode.OK

    assert asyncio.run(run()) is grpc.StatusCode.PERMISSION_DENIED


def test_the_fixture_backend_resolves_a_seeded_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(monkeypatch, {'tok-abc': _BINDING})

    async def run() -> auth_pb2.SessionContext:
        async with _serving() as channel:
            return await auth_pb2_grpc.AuthStub(channel).ResolveSession(
                auth_pb2.ResolveTokenRequest(session_token='tok-abc')
            )

    assert asyncio.run(run()) == auth_pb2.SessionContext(analysis_id='a1', project=auth_pb2.ProjectGrant(id='asd'))


@pytest.mark.parametrize('token', ['unknown', auth_backend.hash_token('tok-abc')])
def test_the_fixture_backend_refuses_an_unknown_token_or_its_hash(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    # The store's hashed key is not itself a bearer.
    _fixture_env(monkeypatch, {'tok-abc': _BINDING})

    async def run() -> grpc.StatusCode:
        async with _serving() as channel:
            try:
                await auth_pb2_grpc.AuthStub(channel).ResolveSession(auth_pb2.ResolveTokenRequest(session_token=token))
            except grpc.aio.AioRpcError as exc:
                return exc.code()
            return grpc.StatusCode.OK

    assert asyncio.run(run()) is grpc.StatusCode.PERMISSION_DENIED


def test_the_fixture_backend_serves_its_seeded_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(monkeypatch)
    acl_json = json.dumps({'format': 1, 'projects': {'rare': {'managers': [harness.JOHN], 'curators': []}}})
    monkeypatch.setenv(fixture.ACL_VAR, acl_json)

    async def run() -> auth_pb2.GetAccessResponse:
        async with _serving() as channel:
            return await auth_pb2_grpc.AuthStub(channel).GetAccess(
                auth_pb2.GetAccessRequest(email=harness.JOHN),
                metadata=claim_mod.pairs(auth_fixture.WEB),
            )

    assert asyncio.run(run()) == auth_pb2.GetAccessResponse(
        projects=[auth_pb2.ProjectGrant(id='rare', roles=[auth_pb2.ROLE_MANAGER])]
    )


def test_the_server_resolves_a_claimed_session_in_process_without_an_auth_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture_env(monkeypatch, {'tok': _BINDING})
    monkeypatch.delenv('THEMIS_AUTH_URL', raising=False)
    with_session = (*auth_fixture.CLU, *auth_fixture.claim(sandbox_options_pb2.CALLING_AS_SELF, 'tok'))

    async def run() -> auth_pb2.GetAccessResponse:
        async with _serving() as channel:
            return await auth_pb2_grpc.AuthStub(channel).GetAccess(
                auth_pb2.GetAccessRequest(email=harness.JOHN),
                metadata=claim_mod.pairs(with_session),
            )

    assert asyncio.run(run()) == auth_pb2.GetAccessResponse(
        projects=[auth_pb2.ProjectGrant(id='asd', roles=[auth_pb2.ROLE_MANAGER])]
    )


def test_the_server_denies_a_call_claiming_an_unknown_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(monkeypatch, {'tok': _BINDING})
    bad_session = (*auth_fixture.CLU, *auth_fixture.claim(sandbox_options_pb2.CALLING_AS_SELF, 'unknown'))

    async def run() -> grpc.StatusCode:
        async with _serving() as channel:
            try:
                await auth_pb2_grpc.AuthStub(channel).GetAccess(
                    auth_pb2.GetAccessRequest(email=harness.JOHN),
                    metadata=claim_mod.pairs(bad_session),
                )
            except grpc.aio.AioRpcError as exc:
                return exc.code()
            return grpc.StatusCode.OK

    assert asyncio.run(run()) is grpc.StatusCode.PERMISSION_DENIED


def test_the_configured_staleness_limit_governs_when_the_acl_copy_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture_env(monkeypatch, {'tok-abc': _BINDING})
    monkeypatch.setenv(main_mod.STALENESS_LIMIT_VAR, '1800')
    clock = harness.Clock()

    async def code(stub: auth_pb2_grpc.AuthAsyncStub) -> grpc.StatusCode:
        try:
            await stub.ResolveSession(auth_pb2.ResolveTokenRequest(session_token='tok-abc'))
        except grpc.aio.AioRpcError as exc:
            return exc.code()
        return grpc.StatusCode.OK

    async def run() -> list[grpc.StatusCode]:
        async with _serving(monotonic=clock) as channel:
            stub = auth_pb2_grpc.AuthStub(channel)
            clock.advance(1799)
            before_limit = await code(stub)
            clock.advance(2)
            past_limit = await code(stub)
            return [before_limit, past_limit]

    assert asyncio.run(run()) == [grpc.StatusCode.OK, grpc.StatusCode.UNAVAILABLE]


@pytest.mark.parametrize(
    'bindings',
    [
        pytest.param('not json', id='not-json'),
        pytest.param([], id='not-an-object'),
        pytest.param({'tok': {'project_id': 'p1'}}, id='no-analysis'),
        pytest.param({'tok': {**_BINDING, 'extra': 'x'}}, id='unknown-key'),
        pytest.param({'tok': {**_BINDING, 'project_id': ''}}, id='empty-project'),
    ],
)
def test_malformed_bindings_refuse_to_start(monkeypatch: pytest.MonkeyPatch, bindings: object) -> None:
    _fixture_env(monkeypatch, bindings)
    with pytest.raises(SystemExit, match=fixture.BINDINGS_VAR):
        asyncio.run(main_mod._serve(startup.StartupClock()))


@pytest.mark.parametrize('missing', [fixture.BINDINGS_VAR, fixture.ACL_VAR])
def test_each_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _fixture_env(monkeypatch, {})
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit, match=missing):
        asyncio.run(main_mod._serve(startup.StartupClock()))


@pytest.mark.parametrize(
    'missing', ['THEMIS_SQL_CONNECTION_NAME', 'THEMIS_SQL_DATABASE', 'THEMIS_DB_USER', 'THEMIS_ACL_SECRET']
)
def test_the_live_backend_requires_each_source_s_config(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _live_env(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit, match=missing):
        asyncio.run(main_mod._serve(startup.StartupClock()))


@pytest.mark.parametrize(
    'name',
    [
        pytest.param('acl', id='bare-id'),
        pytest.param('projects/themis-proj/secrets/acl/versions/latest', id='a-version'),
        pytest.param('projects//secrets/acl', id='no-project'),
        pytest.param('gs://acl-bucket/acl.json', id='a-uri'),
    ],
)
def test_the_live_backend_refuses_an_acl_secret_naming_no_secret(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    _live_env(monkeypatch, THEMIS_ACL_SECRET=name)
    with pytest.raises(SystemExit, match='THEMIS_ACL_SECRET'):
        asyncio.run(main_mod._serve(startup.StartupClock()))


def test_the_backend_selector_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(monkeypatch)
    monkeypatch.delenv('THEMIS_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_BACKEND'):
        asyncio.run(main_mod._serve(startup.StartupClock()))


def test_an_unknown_backend_selector_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(monkeypatch)
    monkeypatch.setenv('THEMIS_BACKEND', 'memory')
    with pytest.raises(SystemExit, match='THEMIS_BACKEND'):
        asyncio.run(main_mod._serve(startup.StartupClock()))


def test_the_staleness_limit_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(monkeypatch)
    monkeypatch.delenv(main_mod.STALENESS_LIMIT_VAR, raising=False)
    with pytest.raises(SystemExit, match=main_mod.STALENESS_LIMIT_VAR):
        asyncio.run(main_mod._serve(startup.StartupClock()))


@pytest.mark.parametrize(
    'raw',
    ['', 'ten', str(acl_holder.REFRESH_INTERVAL_SECONDS + auth_backend.ACL_READ_TIMEOUT_SECONDS)],
)
def test_a_staleness_limit_not_above_the_refresh_interval_plus_the_read_timeout_is_refused(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    _fixture_env(monkeypatch)
    monkeypatch.setenv(main_mod.STALENESS_LIMIT_VAR, raw)
    with pytest.raises(SystemExit, match=main_mod.STALENESS_LIMIT_VAR):
        asyncio.run(main_mod._serve(startup.StartupClock()))


@pytest.mark.parametrize('missing', ['THEMIS_AUTHORIZER_BACKEND', 'THEMIS_GCP_PROJECT', 'THEMIS_AUTH_FIXTURE_CALLERS'])
def test_each_authorizer_input_is_required(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _fixture_env(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit, match=missing):
        asyncio.run(main_mod._serve(startup.StartupClock()))


def test_no_auth_module_constructs_a_server() -> None:
    gate.assert_no_module_constructs_a_server(auth_service)


def test_the_auth_servicer_reads_no_call_metadata() -> None:
    gate.assert_servicer_reads_no_call_metadata(servicer_mod)


def test_the_cloudsql_backend_requires_a_sample_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    _live_env(monkeypatch)
    monkeypatch.delenv(tracing.SAMPLE_RATIO_VAR, raising=False)
    with pytest.raises(SystemExit, match=tracing.SAMPLE_RATIO_VAR):
        asyncio.run(main_mod._serve(startup.StartupClock()))
