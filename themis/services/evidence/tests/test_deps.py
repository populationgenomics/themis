"""Tests for the image-level collaborators every interface is handed."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2
from themis.services.evidence import deps as deps_mod


def test_fixture_authorizer_resolves_seeded_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_EVIDENCE_FIXTURE_CONTEXTS', json.dumps({'tok': {'project_id': 'p', 'analysis_id': 'a'}}))
    session_resolver = deps_mod._session_resolver_from_env()

    async def resolve(token: str) -> auth_pb2.SessionContext:
        return await session_resolver(token)

    context = asyncio.run(resolve('tok'))
    assert context.project_id == 'p'
    assert context.analysis_id == 'a'
    with pytest.raises(session_mod.UnresolvedSessionError):
        asyncio.run(resolve('unknown'))


@pytest.mark.parametrize(
    ('env', 'unset'),
    [
        ({}, 'THEMIS_AUTHORIZER_BACKEND'),
        ({'THEMIS_AUTHORIZER_BACKEND': 'ldap'}, None),
        ({'THEMIS_AUTHORIZER_BACKEND': 'http'}, 'THEMIS_AUTH_URL'),
        ({'THEMIS_AUTHORIZER_BACKEND': 'fixture'}, 'THEMIS_EVIDENCE_FIXTURE_CONTEXTS'),
    ],
)
def test_an_unusable_authorizer_selection_exits(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], unset: str | None
) -> None:
    monkeypatch.delenv('THEMIS_AUTHORIZER_BACKEND', raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if unset is not None:
        monkeypatch.delenv(unset, raising=False)
    with pytest.raises(SystemExit):
        deps_mod._session_resolver_from_env()


def test_deps_hold_the_stack_that_owns_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # The client is entered on the stack rather than owned per interface, so it closes once when the
    # entrypoint's stack unwinds; an interface that built its own would leak a pool per interface.
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_EVIDENCE_FIXTURE_CONTEXTS', '{}')

    async def build() -> deps_mod.Deps:
        async with contextlib.AsyncExitStack() as stack:
            deps = await deps_mod.deps_from_env(stack)
            assert deps.stack is stack
            assert not deps.http_client.is_closed
            return deps
        raise AssertionError  # unreachable; `async with` does not swallow

    built = asyncio.run(build())
    assert built.http_client.is_closed
