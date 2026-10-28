"""The server entrypoint: the backend, the staleness limit and the gate's authorizer, each from the environment."""

from __future__ import annotations

import asyncio
import json

import pytest
from google.cloud import secretmanager
from google.cloud.sql import connector

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2
from themis.services import auth as auth_service
from themis.services.auth import __main__ as main_mod
from themis.services.auth import acl_holder, fixture
from themis.services.auth import backend as auth_backend
from themis.services.auth import servicer as servicer_mod
from themis.services.auth.tests import harness
from themis.telemetry import tracing
from themis.testing import gate

_BINDING = {'analysis_id': 'a1', 'project_id': 'asd'}
_LIVE_ENV = {
    'THEMIS_BACKEND': 'cloudsql',
    'THEMIS_SQL_CONNECTION_NAME': 'proj:region:instance',
    'THEMIS_SQL_DATABASE': 'themis',
    'THEMIS_DB_USER': 'themis-auth@proj.iam',
    'THEMIS_ACL_SECRET': 'projects/themis-proj/secrets/acl',
}


def _fixture_env(monkeypatch: pytest.MonkeyPatch, bindings: object, acl: str | None = None) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.setenv(fixture.BINDINGS_VAR, bindings if isinstance(bindings, str) else json.dumps(bindings))
    monkeypatch.setenv(fixture.ACL_VAR, harness.acl_version().decode() if acl is None else acl)


def _live_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    # Neither client may reach Google Cloud: the connector dials and the Secret Manager client finds credentials.
    class _Connector:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

    class _SecretManagerClient:
        pass

    monkeypatch.setattr(connector, 'Connector', _Connector)
    monkeypatch.setattr(secretmanager, 'SecretManagerServiceClient', _SecretManagerClient)
    for name, value in {**_LIVE_ENV, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_the_fixture_backend_resolves_a_seeded_token_by_its_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(monkeypatch, {'tok-abc': _BINDING})
    backend = main_mod.build_backend()

    assert asyncio.run(backend.resolve('tok-abc')) == auth_backend.StoredSession(analysis_id='a1', project_id='asd')
    for token in ('unknown', auth_backend.hash_token('tok-abc')):  # the store's key is not a bearer
        with pytest.raises(auth_backend.UnresolvedError):
            asyncio.run(backend.resolve(token))


def test_the_fixture_backend_serves_its_seeded_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(monkeypatch, {})
    assert asyncio.run(main_mod.build_backend().read_acl()) == harness.acl_version()


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
        main_mod.build_backend()


@pytest.mark.parametrize('missing', [fixture.BINDINGS_VAR, fixture.ACL_VAR])
def test_each_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _fixture_env(monkeypatch, {})
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit, match=missing):
        main_mod.build_backend()


def test_an_invalid_fixture_acl_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(monkeypatch, {}, acl='{"format": 1}')
    backend = main_mod.build_backend()
    with pytest.raises(SystemExit, match='acl rejected'):
        asyncio.run(
            acl_holder.AclHolder.load(
                backend.read_acl,
                read_timeout_seconds=harness.READ_TIMEOUT_SECONDS,
                staleness_limit_seconds=600,
                clock=harness.Clock(),
            )
        )


@pytest.mark.parametrize(
    'missing', ['THEMIS_SQL_CONNECTION_NAME', 'THEMIS_SQL_DATABASE', 'THEMIS_DB_USER', 'THEMIS_ACL_SECRET']
)
def test_the_live_backend_requires_each_source_s_config(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _live_env(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit, match=missing):
        main_mod.build_backend()


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
        main_mod.build_backend()


def test_the_live_backend_refreshes_its_certificate_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cloud Run throttles the CPU between requests, stalling the connector's default background refresh.
    strategies: list[object] = []

    class _RecordingConnector:
        def __init__(self, **kwargs: object) -> None:
            strategies.append(kwargs.get('refresh_strategy'))

    _live_env(monkeypatch)
    monkeypatch.setattr(connector, 'Connector', _RecordingConnector)
    main_mod.build_backend()

    assert strategies == [connector.RefreshStrategy.LAZY]


@pytest.mark.parametrize('selector', [None, 'memory'])
def test_the_backend_selector_is_required_and_known(monkeypatch: pytest.MonkeyPatch, selector: str | None) -> None:
    if selector is None:
        monkeypatch.delenv('THEMIS_BACKEND', raising=False)
    else:
        monkeypatch.setenv('THEMIS_BACKEND', selector)
    with pytest.raises(SystemExit, match='THEMIS_BACKEND'):
        main_mod.build_backend()


@pytest.mark.parametrize(
    'raw',
    [None, '', 'ten', str(acl_holder.REFRESH_INTERVAL_SECONDS + auth_backend.ACL_READ_TIMEOUT_SECONDS)],
)
def test_the_staleness_limit_is_an_integer_above_the_refresh_interval_plus_the_read_timeout(
    monkeypatch: pytest.MonkeyPatch, raw: str | None
) -> None:
    if raw is None:
        monkeypatch.delenv(main_mod.STALENESS_LIMIT_VAR, raising=False)
    else:
        monkeypatch.setenv(main_mod.STALENESS_LIMIT_VAR, raw)
    with pytest.raises(SystemExit, match=main_mod.STALENESS_LIMIT_VAR):
        main_mod.staleness_limit_seconds()


def test_a_staleness_limit_above_that_floor_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(main_mod.STALENESS_LIMIT_VAR, '1800')
    assert main_mod.staleness_limit_seconds() == 1800


def test_the_gate_resolves_sessions_in_process_and_needs_no_auth_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_GCP_PROJECT', 'x')
    monkeypatch.setenv('THEMIS_AUTH_FIXTURE_CALLERS', '{}')
    monkeypatch.delenv('THEMIS_AUTH_URL', raising=False)
    _fixture_env(monkeypatch, {'tok': _BINDING})

    async def run() -> auth_pb2.SessionContext:
        backend = main_mod.build_backend()
        acl = await acl_holder.AclHolder.load(
            backend.read_acl,
            read_timeout_seconds=harness.READ_TIMEOUT_SECONDS,
            staleness_limit_seconds=600,
            clock=harness.Clock(),
        )
        authorizer = main_mod.build_authorizer(servicer_mod.Servicer(backend, acl, operator=harness.OPERATOR))
        with pytest.raises(session_mod.UnresolvedSessionError):
            await authorizer.session_resolver('unknown')
        return await authorizer.session_resolver('tok')

    assert asyncio.run(run()).project.id == 'asd'


@pytest.mark.parametrize('missing', ['THEMIS_AUTHORIZER_BACKEND', 'THEMIS_GCP_PROJECT', 'THEMIS_AUTH_FIXTURE_CALLERS'])
def test_each_authorizer_input_is_required(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_GCP_PROJECT', 'x')
    monkeypatch.setenv('THEMIS_AUTH_FIXTURE_CALLERS', '{}')
    monkeypatch.delenv(missing)
    _fixture_env(monkeypatch, {})

    async def run() -> None:
        backend = main_mod.build_backend()
        acl = await acl_holder.AclHolder.load(
            backend.read_acl,
            read_timeout_seconds=harness.READ_TIMEOUT_SECONDS,
            staleness_limit_seconds=600,
            clock=harness.Clock(),
        )
        main_mod.build_authorizer(servicer_mod.Servicer(backend, acl, operator=harness.OPERATOR))

    with pytest.raises(SystemExit, match=missing):
        asyncio.run(run())


def test_the_auth_server_is_built_only_through_the_gated_factory() -> None:
    gate.assert_built_only_through_the_gated_factory(main_mod)


def test_no_auth_module_constructs_a_server() -> None:
    gate.assert_no_module_constructs_a_server(auth_service)


def test_the_auth_servicer_reads_no_call_metadata() -> None:
    gate.assert_servicer_reads_no_call_metadata(servicer_mod)


def test_the_fixture_backend_is_never_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    # Offline, the ratio is never read: its absence is no error, and nothing reaches Google Cloud.
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.delenv(tracing.SAMPLE_RATIO_VAR, raising=False)
    main_mod.install_tracing()


def test_the_cloudsql_backend_requires_a_sample_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'cloudsql')
    monkeypatch.delenv(tracing.SAMPLE_RATIO_VAR, raising=False)
    with pytest.raises(SystemExit, match=tracing.SAMPLE_RATIO_VAR):
        main_mod.install_tracing()
