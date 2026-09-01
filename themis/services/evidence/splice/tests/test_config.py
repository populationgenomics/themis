"""The splice interface's env contract: which adapter each selector value builds, fail-loud."""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx2
import pytest

from themis.rpc import auth_pb2, splice_pb2
from themis.services.evidence import deps as deps_mod
from themis.services.evidence import errors
from themis.services.evidence.splice import backend as splice_backend
from themis.services.evidence.splice import config

_SEED = json.dumps(
    {
        'predict_deltas': {'13-32332343-A-G': {'spliceai_loss': 0.5}},
        'predict_skip_outcome': {'NM_000059.4:GRCh38:exon:11': {'gene': 'BRCA2'}},
    }
)


async def _unreachable_session_resolver(session_token: str) -> auth_pb2.SessionContext:
    raise AssertionError('building a backend resolves no session')


def _from_env() -> splice_backend.SpliceBackend:
    """Select the backend as the entrypoint would; no test here reaches an upstream."""
    return config.backend_from_env(
        deps_mod.Deps(
            session_resolver=_unreachable_session_resolver,
            http_client=httpx2.AsyncClient(),
            stack=contextlib.AsyncExitStack(),
        )
    )


def _predict_deltas(backend: splice_backend.SpliceBackend, variant: str) -> splice_pb2.PredictDeltasResponse:
    return asyncio.run(backend.predict_deltas(splice_pb2.PredictDeltasRequest(variant=variant)))


def _predict_skip_outcome(backend: splice_backend.SpliceBackend, exon: int) -> splice_pb2.PredictSkipOutcomeResponse:
    return asyncio.run(
        backend.predict_skip_outcome(
            splice_pb2.PredictSkipOutcomeRequest(transcript='NM_000059.4', genome_build='GRCh38', exon=exon)
        )
    )


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_SPLICE_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_SPLICE_BACKEND'):
        _from_env()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_SPLICE_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _from_env()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # The seed var, not the selector, is named — the operator has to know which value is missing.
    monkeypatch.setenv('THEMIS_SPLICE_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_SPLICE_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_SPLICE_FIXTURE'):
        _from_env()


def test_fixture_selector_answers_what_it_was_seeded_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unseeded key raises rather than answering empty: here an absence is "no score exists"."""
    monkeypatch.setenv('THEMIS_SPLICE_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_SPLICE_FIXTURE', _SEED)
    backend = _from_env()
    assert _predict_deltas(backend, '13-32332343-A-G').spliceai_loss == 0.5
    assert _predict_skip_outcome(backend, 11).gene == 'BRCA2'
    with pytest.raises(errors.UnknownVariantError):
        _predict_deltas(backend, '13-32332343-A-T')
    with pytest.raises(errors.UnknownVariantError):
        _predict_skip_outcome(backend, 12)


def test_live_selector_builds_the_live_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    # Constructing it opens nothing: the adapter holds the image's shared client and issues on demand.
    monkeypatch.setenv('THEMIS_SPLICE_BACKEND', 'live')
    assert isinstance(_from_env(), splice_backend.LiveBackend)
