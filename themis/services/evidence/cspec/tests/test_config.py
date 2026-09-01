"""The cspec interface's env contract: which adapter each selector value builds, fail-loud."""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx2
import pytest

from themis.rpc import auth_pb2, cspec_pb2
from themis.services.evidence import deps as deps_mod
from themis.services.evidence import errors
from themis.services.evidence.cspec import backend as cspec_backend
from themis.services.evidence.cspec import config


async def _unreachable_session_resolver(session_token: str) -> auth_pb2.SessionContext:
    raise AssertionError('building a backend resolves no session')


def _from_env() -> cspec_backend.CspecBackend:
    """Select the backend as the entrypoint would; no test here reaches an upstream."""
    return config.backend_from_env(
        deps_mod.Deps(
            session_resolver=_unreachable_session_resolver,
            http_client=httpx2.AsyncClient(),
            stack=contextlib.AsyncExitStack(),
        )
    )


def _list_specifications(backend: cspec_backend.CspecBackend, gene: str) -> cspec_pb2.ListSpecificationsResponse:
    return asyncio.run(backend.list_specifications(cspec_pb2.ListSpecificationsRequest(gene=gene)))


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_CSPEC_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_CSPEC_BACKEND'):
        _from_env()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CSPEC_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _from_env()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # The seed var, not the selector, is named — the operator has to know which value is missing.
    monkeypatch.setenv('THEMIS_CSPEC_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_CSPEC_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_CSPEC_FIXTURE'):
        _from_env()


def test_fixture_selector_answers_what_it_was_seeded_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unseeded key is a stated absence, not an empty response the caller would read as an answer."""
    monkeypatch.setenv('THEMIS_CSPEC_BACKEND', 'fixture')
    monkeypatch.setenv(
        'THEMIS_CSPEC_FIXTURE',
        json.dumps({'list_specifications': {'BRCA1': {'specifications': [{'id': 'GN101'}]}}}),
    )
    backend = _from_env()
    assert _list_specifications(backend, 'BRCA1').specifications[0].id == 'GN101'
    with pytest.raises(errors.UnknownVariantError):
        _list_specifications(backend, 'BRCA2')


def test_live_selector_builds_the_live_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    # Constructing it opens nothing: the adapter holds the image's shared client and issues on demand.
    monkeypatch.setenv('THEMIS_CSPEC_BACKEND', 'live')
    assert isinstance(_from_env(), cspec_backend.LiveBackend)
