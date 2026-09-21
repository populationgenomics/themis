"""Tests for the sheaf entrypoint's env-selected backend and limits, and the gate it serves behind."""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from themis import sheaf
from themis.rpc import auth_pb2
from themis.services import sheaf as sheaf_service
from themis.services.sheaf import __main__ as main_mod
from themis.services.sheaf import servicer as servicer_mod
from themis.sheaf.backends import gcs
from themis.testing import gate

_LIMIT_ENV = {
    'THEMIS_SHEAF_MAX_PUBLISH_BYTES': '1048576',
    'THEMIS_SHEAF_MAX_REFS': '256',
    'THEMIS_SHEAF_MAX_DOCUMENT_BYTES': '65536',
}


def test_the_authorizer_is_seeded_from_the_sheaf_image_s_own_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_GCP_PROJECT', 'x')
    monkeypatch.setenv('THEMIS_SHEAF_FIXTURE_CONTEXTS', '{"tok": {"project_id": "p", "analysis_id": "a"}}')
    monkeypatch.setenv('THEMIS_SHEAF_FIXTURE_CALLERS', '{}')
    authorizer = main_mod.build_authorizer()

    async def resolve() -> auth_pb2.SessionContext:
        return await authorizer.session_resolver('tok')

    context = asyncio.run(resolve())
    assert (context.project_id, context.analysis_id) == ('p', 'a')


def test_the_sheaf_server_is_built_only_through_the_gated_factory() -> None:
    gate.assert_built_only_through_the_gated_factory(main_mod)


def test_no_sheaf_module_constructs_a_server() -> None:
    gate.assert_no_module_constructs_a_server(sheaf_service)


def test_the_sheaf_servicer_reads_no_call_metadata() -> None:
    gate.assert_servicer_reads_no_call_metadata(servicer_mod)


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
