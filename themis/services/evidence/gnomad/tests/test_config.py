"""The gnomad interface's env contract: which adapter each selector value builds, fail-loud."""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest

from themis.rpc import auth_pb2, gnomad_pb2
from themis.services.evidence import deps as deps_mod
from themis.services.evidence import errors
from themis.services.evidence.gnomad import backend as gnomad_backend
from themis.services.evidence.gnomad import config


async def _unreachable_session_resolver(session_token: str) -> auth_pb2.SessionContext:
    raise AssertionError('building a backend resolves no session')


def _from_env() -> gnomad_backend.GnomadBackend:
    """Select the backend as the entrypoint would; no test here reaches an upstream."""
    return config.backend_from_env(
        deps_mod.Deps(
            session_resolver=_unreachable_session_resolver,
            http_client=httpx.AsyncClient(),
            stack=contextlib.AsyncExitStack(),
        )
    )


def _describe_variant(backend: gnomad_backend.GnomadBackend, gnomad_id: str) -> gnomad_pb2.DescribeVariantResponse:
    return asyncio.run(
        backend.describe_variant(gnomad_pb2.DescribeVariantRequest(gnomad_id=gnomad_id, dataset='gnomad_r4'))
    )


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_GNOMAD_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_GNOMAD_BACKEND'):
        _from_env()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GNOMAD_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _from_env()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # The seed var, not the selector, is named — the operator has to know which value is missing.
    monkeypatch.setenv('THEMIS_GNOMAD_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_GNOMAD_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_GNOMAD_FIXTURE'):
        _from_env()


def test_fixture_selector_answers_what_it_was_seeded_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unseeded key raises rather than answering empty: here an absence is the rarity finding."""
    monkeypatch.setenv('THEMIS_GNOMAD_BACKEND', 'fixture')
    monkeypatch.setenv(
        'THEMIS_GNOMAD_FIXTURE',
        json.dumps({'describe_variant': {'17-43093464-C-G': {'provenance': [{'source': 'gnomAD GraphQL'}]}}}),
    )
    backend = _from_env()
    assert _describe_variant(backend, '17-43093464-C-G').provenance[0].source == 'gnomAD GraphQL'
    with pytest.raises(errors.UnknownVariantError):
        _describe_variant(backend, '17-43093464-C-A')


def test_live_selector_builds_the_live_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    # Constructing it opens nothing: the adapter holds the image's shared client and issues on demand.
    monkeypatch.setenv('THEMIS_GNOMAD_BACKEND', 'live')
    assert isinstance(_from_env(), gnomad_backend.LiveBackend)
