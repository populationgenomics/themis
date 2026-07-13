"""Tests for the server entrypoint's backend construction."""

from __future__ import annotations

import asyncio
import json

import pytest

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2
from themis.services.store import __main__ as main_mod
from themis.services.store import storage as storage_mod


async def _resolve_session(session_resolver: session_mod.SessionResolver, token: str) -> auth_pb2.SessionContext:
    return await session_resolver(token)


def test_fixture_storage_is_built(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_STORAGE_BACKEND', 'fixture')
    assert isinstance(main_mod.build_storage(), storage_mod.FixtureStorage)


def test_missing_storage_backend_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_STORAGE_BACKEND', raising=False)
    with pytest.raises(SystemExit):
        main_mod.build_storage()


def test_unsupported_storage_backend_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_STORAGE_BACKEND', 'memory')
    with pytest.raises(SystemExit):
        main_mod.build_storage()


def test_gcs_storage_requires_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_STORAGE_BACKEND', 'gcs')
    for name in ('THEMIS_STORE_WORKING_DOCUMENT_BUCKET', 'THEMIS_STORE_WORKSPACE_BUCKET'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        main_mod.build_storage()


def test_fixture_session_resolver_resolves_seeded_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv(
        'THEMIS_STORE_FIXTURE_CONTEXTS',
        json.dumps({'tok': {'project_id': 'p1', 'analysis_id': 'a1'}}),
    )
    session_resolver = main_mod.build_session_resolver()

    context = asyncio.run(_resolve_session(session_resolver, 'tok'))
    assert context.project_id == 'p1'
    assert context.analysis_id == 'a1'
    with pytest.raises(session_mod.UnresolvedSessionError):
        asyncio.run(_resolve_session(session_resolver, 'unknown'))


def test_missing_authorizer_backend_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_AUTHORIZER_BACKEND', raising=False)
    with pytest.raises(SystemExit):
        main_mod.build_session_resolver()


def test_unsupported_authorizer_backend_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'ldap')
    with pytest.raises(SystemExit):
        main_mod.build_session_resolver()


def test_http_authorizer_requires_auth_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'http')
    monkeypatch.delenv('THEMIS_AUTH_URL', raising=False)
    with pytest.raises(SystemExit):
        main_mod.build_session_resolver()


def test_missing_fixture_contexts_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_STORE_FIXTURE_CONTEXTS', raising=False)
    with pytest.raises(SystemExit):
        main_mod.build_session_resolver()


def test_malformed_fixture_contexts_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_STORE_FIXTURE_CONTEXTS', 'not json')
    with pytest.raises(SystemExit):
        main_mod.build_session_resolver()


def test_malformed_fixture_binding_shape_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_STORE_FIXTURE_CONTEXTS', json.dumps({'tok': {'project_id': 'p1'}}))
    with pytest.raises(SystemExit):
        main_mod.build_session_resolver()
