"""Tests for the sheaf entrypoint's env-selected authorizer, backend and limits."""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from themis import sheaf
from themis.rpc import auth_pb2
from themis.services.sheaf import __main__ as main_mod
from themis.services.sheaf import servicer as servicer_mod
from themis.sheaf.backends import gcs

_LIMIT_ENV = {
    'THEMIS_SHEAF_MAX_PUBLISH_BYTES': '1048576',
    'THEMIS_SHEAF_MAX_REFS': '256',
    'THEMIS_SHEAF_MAX_DOCUMENT_BYTES': '65536',
}


def test_build_session_resolver_requires_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_AUTHORIZER_BACKEND', raising=False)
    with pytest.raises(SystemExit):
        main_mod.build_session_resolver()


def test_build_session_resolver_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'nope')
    with pytest.raises(SystemExit):
        main_mod.build_session_resolver()


def test_fixture_authorizer_requires_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_SHEAF_FIXTURE_CONTEXTS', raising=False)
    with pytest.raises(SystemExit):
        main_mod.build_session_resolver()


def test_fixture_authorizer_resolves_a_seeded_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_SHEAF_FIXTURE_CONTEXTS', '{"tok": {"project_id": "p", "analysis_id": "a"}}')
    session_resolver = main_mod.build_session_resolver()

    async def run() -> auth_pb2.SessionContext:
        return await session_resolver('tok')

    context = asyncio.run(run())
    assert context.project_id == 'p'
    assert context.analysis_id == 'a'


def test_build_backend_requires_a_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_SHEAF_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_SHEAF_BACKEND'):
        main_mod.build_backend()


def test_build_backend_rejects_an_unknown_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_SHEAF_BACKEND', 'memory')
    with pytest.raises(SystemExit, match='memory'):
        main_mod.build_backend()


def test_local_backend_requires_its_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_SHEAF_BACKEND', 'local')
    monkeypatch.delenv('THEMIS_SHEAF_LOCAL_ROOT', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_SHEAF_LOCAL_ROOT'):
        main_mod.build_backend()


def test_local_backend_is_rooted_where_told(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv('THEMIS_SHEAF_BACKEND', 'local')
    monkeypatch.setenv('THEMIS_SHEAF_LOCAL_ROOT', str(tmp_path / 'sheaf'))
    backend = main_mod.build_backend()
    assert isinstance(backend, sheaf.LocalBackend)
    assert backend.root == tmp_path / 'sheaf'


def test_gcs_backend_requires_its_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_SHEAF_BACKEND', 'gcs')
    monkeypatch.delenv('THEMIS_WORKSPACE_BUCKET', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_WORKSPACE_BUCKET'):
        main_mod.build_backend()


def test_limits_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for var, value in _LIMIT_ENV.items():
        monkeypatch.setenv(var, value)
    assert main_mod.build_limits() == servicer_mod.Limits(
        max_publish_bytes=1048576, max_refs=256, max_document_bytes=65536
    )


@pytest.mark.parametrize('missing', sorted(_LIMIT_ENV))
def test_every_limit_is_required(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    for var, value in _LIMIT_ENV.items():
        monkeypatch.setenv(var, value)
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit, match=missing):
        main_mod.build_limits()


@pytest.mark.parametrize('bad', ['0', '-1', 'lots', '1.5', ''])
def test_a_limit_that_is_not_a_positive_integer_is_refused(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    for var, value in _LIMIT_ENV.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv('THEMIS_SHEAF_MAX_REFS', bad)
    with pytest.raises(SystemExit, match='THEMIS_SHEAF_MAX_REFS'):
        main_mod.build_limits()


def test_gcs_backend_keys_repositories_under_the_workspaces_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_SHEAF_BACKEND', 'gcs')
    monkeypatch.setenv('THEMIS_WORKSPACE_BUCKET', 'a-bucket')
    monkeypatch.setenv('STORAGE_EMULATOR_HOST', 'http://127.0.0.1:1')  # a client that never authenticates
    backend = main_mod.build_backend()
    assert isinstance(backend, gcs.GcsBackend)
    assert backend.prefix == main_mod.WORKSPACES_PREFIX == 'workspaces'
