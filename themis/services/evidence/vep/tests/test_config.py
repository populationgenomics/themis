"""The vep interface's env contract: which adapter each selector value builds, fail-loud."""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx2
import pytest

from themis.rpc import auth_pb2, vep_pb2
from themis.services.evidence import deps as deps_mod
from themis.services.evidence import errors
from themis.services.evidence.vep import backend as vep_backend
from themis.services.evidence.vep import config


async def _unreachable_session_resolver(session_token: str) -> auth_pb2.SessionContext:
    raise AssertionError('building a backend resolves no session')


def _from_env() -> vep_backend.VepBackend:
    """Select the backend as the entrypoint would; no test here reaches an upstream."""
    return config.backend_from_env(
        deps_mod.Deps(
            session_resolver=_unreachable_session_resolver,
            http_client=httpx2.AsyncClient(),
            stack=contextlib.AsyncExitStack(),
        )
    )


def _annotate(backend: vep_backend.VepBackend, variant: str) -> vep_pb2.AnnotateResponse:
    return asyncio.run(backend.annotate(vep_pb2.AnnotateRequest(variant=variant)))


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_VEP_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_VEP_BACKEND'):
        _from_env()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_VEP_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _from_env()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # The seed var, not the selector, is named — the operator has to know which value is missing.
    monkeypatch.setenv('THEMIS_VEP_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_VEP_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_VEP_FIXTURE'):
        _from_env()


def test_fixture_selector_answers_what_it_was_seeded_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unseeded key is a stated absence, not an empty response the caller would read as an answer."""
    monkeypatch.setenv('THEMIS_VEP_BACKEND', 'fixture')
    monkeypatch.setenv(
        'THEMIS_VEP_FIXTURE',
        json.dumps({'annotate': {'17-43093464-C-G': {'provenance': [{'source': 'Ensembl VEP REST'}]}}}),
    )
    backend = _from_env()
    assert _annotate(backend, '17-43093464-C-G').provenance[0].source == 'Ensembl VEP REST'
    with pytest.raises(errors.UnknownVariantError):
        _annotate(backend, '17-43093464-C-A')


def test_live_selector_builds_the_live_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    # Constructing it opens nothing: the adapter holds the image's shared client and issues on demand.
    monkeypatch.setenv('THEMIS_VEP_BACKEND', 'live')
    assert isinstance(_from_env(), vep_backend.LiveBackend)
