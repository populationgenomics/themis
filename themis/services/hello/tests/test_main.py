"""Tests for the hello entrypoint's env-selected authorizer backend."""

from __future__ import annotations

import asyncio

import pytest

from themis.rpc import auth_pb2
from themis.services.hello import __main__ as main_mod


def test_build_session_resolver_requires_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_AUTHORIZER_BACKEND', raising=False)
    with pytest.raises(SystemExit):
        main_mod.build_session_resolver()


def test_build_session_resolver_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'nope')
    with pytest.raises(SystemExit):
        main_mod.build_session_resolver()


def test_fixture_backend_requires_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_HELLO_FIXTURE_CONTEXTS', raising=False)
    with pytest.raises(SystemExit):
        main_mod.build_session_resolver()


def test_fixture_backend_resolves_a_seeded_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_HELLO_FIXTURE_CONTEXTS', '{"tok": {"project_id": "p", "analysis_id": "a"}}')
    session_resolver = main_mod.build_session_resolver()

    async def run() -> auth_pb2.SessionContext:
        return await session_resolver('tok')

    context = asyncio.run(run())
    assert context.project_id == 'p'
    assert context.analysis_id == 'a'
