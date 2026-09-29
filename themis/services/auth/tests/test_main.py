"""Tests for the server entrypoint's backend construction."""

from __future__ import annotations

import asyncio
import json

import pytest
from google.cloud.sql import connector

from themis.services.auth import __main__ as main_mod
from themis.services.auth import backend as auth_backend


def test_fixture_backend_resolves_seeded_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.setenv(
        'THEMIS_FIXTURE_BINDINGS',
        json.dumps({'tok-abc': {'project_id': 'p1', 'analysis_id': 'a1'}}),
    )
    backend = main_mod.build_backend()

    context = asyncio.run(backend.resolve('tok-abc'))
    assert context.project_id == 'p1'
    assert context.analysis_id == 'a1'
    with pytest.raises(auth_backend.UnresolvedError):
        asyncio.run(backend.resolve('unknown'))


def test_fixture_backend_keys_by_hash_not_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_FIXTURE_BINDINGS', json.dumps({'tok-abc': {'project_id': 'p1', 'analysis_id': 'a1'}}))
    backend = main_mod.build_backend()

    # The plaintext token resolves; its hash (what the store holds) is not a key.
    with pytest.raises(auth_backend.UnresolvedError):
        asyncio.run(backend.resolve(auth_backend.hash_token('tok-abc')))


def test_explicit_empty_store_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit empty fixture store still boots (resolves nothing).
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_FIXTURE_BINDINGS', '{}')
    backend = main_mod.build_backend()
    with pytest.raises(auth_backend.UnresolvedError):
        asyncio.run(backend.resolve('anything'))


def test_cloudsql_backend_requires_sql_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'cloudsql')
    for name in ('THEMIS_SQL_CONNECTION_NAME', 'THEMIS_SQL_DATABASE', 'THEMIS_DB_USER'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        main_mod.build_backend()


def test_cloudsql_backend_refreshes_its_certificate_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cloud Run throttles the CPU between requests, stalling the connector's default background refresh.
    strategies: list[object] = []

    class _RecordingConnector:
        def __init__(self, **kwargs: object) -> None:
            strategies.append(kwargs.get('refresh_strategy'))

    monkeypatch.setattr(connector, 'Connector', _RecordingConnector)
    monkeypatch.setenv('THEMIS_BACKEND', 'cloudsql')
    monkeypatch.setenv('THEMIS_SQL_CONNECTION_NAME', 'proj:region:instance')
    monkeypatch.setenv('THEMIS_SQL_DATABASE', 'themis')
    monkeypatch.setenv('THEMIS_DB_USER', 'themis-auth@proj.iam')
    main_mod.build_backend()

    assert strategies == [connector.RefreshStrategy.LAZY]


def test_missing_bindings_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_FIXTURE_BINDINGS', raising=False)
    with pytest.raises(SystemExit):
        main_mod.build_backend()


def test_missing_backend_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_BACKEND', raising=False)
    with pytest.raises(SystemExit):
        main_mod.build_backend()


def test_unsupported_backend_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'memory')
    with pytest.raises(SystemExit):
        main_mod.build_backend()


def test_malformed_bindings_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_FIXTURE_BINDINGS', 'not json')
    with pytest.raises(SystemExit):
        main_mod.build_backend()


def test_malformed_binding_shape_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    # Valid JSON, but a binding of the wrong shape (missing analysis_id).
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_FIXTURE_BINDINGS', json.dumps({'tok-abc': {'project_id': 'p1'}}))
    with pytest.raises(SystemExit):
        main_mod.build_backend()
