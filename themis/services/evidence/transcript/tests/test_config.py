"""The transcript interface's env contract: which adapter each selector value builds, fail-loud."""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest

from themis.rpc import auth_pb2, transcript_pb2
from themis.services.evidence import deps as deps_mod
from themis.services.evidence import errors
from themis.services.evidence.transcript import backend as transcript_backend
from themis.services.evidence.transcript import config

_SEED = json.dumps(
    {
        'get_structure': {'NM_000059.4:GRCh38': {'gene': 'BRCA2'}},
        'assess_exon_relevance': {'BRCA2:NM_000059.4:11': {'in_mane_select': True}},
    }
)


async def _unreachable_session_resolver(session_token: str) -> auth_pb2.SessionContext:
    raise AssertionError('building a backend resolves no session')


def _from_env() -> transcript_backend.TranscriptBackend:
    """Select the backend as the entrypoint would; no test here reaches an upstream."""
    return config.backend_from_env(
        deps_mod.Deps(
            session_resolver=_unreachable_session_resolver,
            http_client=httpx.AsyncClient(),
            stack=contextlib.AsyncExitStack(),
        )
    )


def _get_structure(
    backend: transcript_backend.TranscriptBackend, transcript: str
) -> transcript_pb2.GetStructureResponse:
    return asyncio.run(
        backend.get_structure(transcript_pb2.GetStructureRequest(transcript=transcript, genome_build='GRCh38'))
    )


def _assess_exon_relevance(
    backend: transcript_backend.TranscriptBackend, exon: int
) -> transcript_pb2.AssessExonRelevanceResponse:
    return asyncio.run(
        backend.assess_exon_relevance(
            transcript_pb2.AssessExonRelevanceRequest(gene='BRCA2', transcript='NM_000059.4', exon=exon)
        )
    )


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_TRANSCRIPT_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_TRANSCRIPT_BACKEND'):
        _from_env()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_TRANSCRIPT_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _from_env()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # The seed var, not the selector, is named — the operator has to know which value is missing.
    monkeypatch.setenv('THEMIS_TRANSCRIPT_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_TRANSCRIPT_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_TRANSCRIPT_FIXTURE'):
        _from_env()


def test_fixture_selector_answers_what_it_was_seeded_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each rpc's section reaches its own rpc, and an unseeded key is a stated absence."""
    monkeypatch.setenv('THEMIS_TRANSCRIPT_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_TRANSCRIPT_FIXTURE', _SEED)
    backend = _from_env()
    assert _get_structure(backend, 'NM_000059.4').gene == 'BRCA2'
    assert _assess_exon_relevance(backend, 11).in_mane_select
    with pytest.raises(errors.UnknownVariantError):
        _get_structure(backend, 'NM_007294.4')
    with pytest.raises(errors.UnknownVariantError):
        _assess_exon_relevance(backend, 12)


def test_live_selector_builds_the_live_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    # Constructing it opens nothing: the adapter holds the image's shared client and issues on demand.
    monkeypatch.setenv('THEMIS_TRANSCRIPT_BACKEND', 'live')
    assert isinstance(_from_env(), transcript_backend.LiveBackend)
