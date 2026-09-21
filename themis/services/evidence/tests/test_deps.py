"""Tests for the image-level collaborators every interface is handed."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from themis.services.evidence import deps as deps_mod


def _seed_fixture_authorizer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full fixture authorizer config: a session seed, a caller seed, the project."""
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_EVIDENCE_FIXTURE_CONTEXTS', '{}')
    monkeypatch.setenv('THEMIS_EVIDENCE_FIXTURE_CALLERS', '{}')
    monkeypatch.setenv('THEMIS_GCP_PROJECT', 'x')


def test_deps_hold_the_stack_that_owns_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # The client is entered on the stack rather than owned per interface, so it closes once when the
    # entrypoint's stack unwinds; an interface that built its own would leak a pool per interface.
    _seed_fixture_authorizer(monkeypatch)

    async def build() -> deps_mod.Deps:
        async with contextlib.AsyncExitStack() as stack:
            deps = await deps_mod.deps_from_env(stack)
            assert deps.stack is stack
            assert not deps.http_client.is_closed
            return deps
        raise AssertionError  # unreachable; `async with` does not swallow

    built = asyncio.run(build())
    assert built.http_client.is_closed


def test_the_authorizer_carries_the_deployment_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_fixture_authorizer(monkeypatch)

    async def build() -> deps_mod.Deps:
        async with contextlib.AsyncExitStack() as stack:
            return await deps_mod.deps_from_env(stack)
        raise AssertionError

    deps = asyncio.run(build())
    assert deps.authorizer.project == 'x'


@pytest.mark.parametrize('unset', ['THEMIS_EVIDENCE_FIXTURE_CALLERS', 'THEMIS_GCP_PROJECT'])
def test_a_missing_authorizer_input_exits(monkeypatch: pytest.MonkeyPatch, unset: str) -> None:
    _seed_fixture_authorizer(monkeypatch)
    monkeypatch.delenv(unset, raising=False)

    async def build() -> None:
        async with contextlib.AsyncExitStack() as stack:
            await deps_mod.deps_from_env(stack)

    with pytest.raises(SystemExit, match=unset):
        asyncio.run(build())
