"""The gene_disease interface's env contract: which adapter each selector value builds, fail-loud."""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib

import httpx2
import pytest

from themis.rpc import auth_pb2, gene_disease_pb2
from themis.services.evidence import deps as deps_mod
from themis.services.evidence import errors
from themis.services.evidence.gene_disease import backend as gene_disease_backend
from themis.services.evidence.gene_disease import config

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'


async def _unreachable_session_resolver(session_token: str) -> auth_pb2.SessionContext:
    raise AssertionError('building a backend resolves no session')


def _from_env() -> gene_disease_backend.GeneDiseaseBackend:
    """Select the backend as the entrypoint would; this build is the image's one async one."""
    return asyncio.run(
        config.backend_from_env(
            deps_mod.Deps(
                session_resolver=_unreachable_session_resolver,
                http_client=httpx2.AsyncClient(),
                stack=contextlib.AsyncExitStack(),
            )
        )
    )


def _describe_gene(
    backend: gene_disease_backend.GeneDiseaseBackend, hgnc_id: str
) -> gene_disease_pb2.DescribeGeneResponse:
    return asyncio.run(backend.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id=hgnc_id)))


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_GENE_DISEASE_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_GENE_DISEASE_BACKEND'):
        _from_env()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _from_env()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # The seed var, not the selector, is named — the operator has to know which value is missing.
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_GENE_DISEASE_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_GENE_DISEASE_FIXTURE'):
        _from_env()


def test_fixture_selector_answers_what_it_was_seeded_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unseeded key is a stated absence, not an empty response the caller would read as an answer."""
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'fixture')
    monkeypatch.setenv(
        'THEMIS_GENE_DISEASE_FIXTURE',
        json.dumps({'describe_gene': {'HGNC:1100': {'entities': [{'disease_label': 'breast-ovarian cancer'}]}}}),
    )
    backend = _from_env()
    assert _describe_gene(backend, 'HGNC:1100').entities[0].disease_label == 'breast-ovarian cancer'
    with pytest.raises(errors.UnknownVariantError):
        _describe_gene(backend, 'HGNC:1101')


def test_live_backend_requires_the_resources_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset, the adapter would load no dump and answer "not curated" for every gene."""
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'live')
    monkeypatch.delenv('THEMIS_RESOURCES_BUCKET', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_RESOURCES_BUCKET'):
        _from_env()


def test_live_selector_builds_the_live_adapter_over_the_named_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one build that reads a bucket: the selector has to reach it, and with the shared client."""
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'live')
    monkeypatch.setenv('THEMIS_RESOURCES_BUCKET', 'resources-bucket')
    seen: dict[str, object] = {}

    def fake_download(bucket: str) -> dict[str, bytes]:
        seen['bucket'] = bucket
        return {
            gene_disease_backend._GENCC_OBJECT: (_FIXTURES / 'gencc.tsv').read_bytes(),
            gene_disease_backend._VALIDITY_OBJECT: (_FIXTURES / 'clingen_validity.csv').read_bytes(),
            gene_disease_backend._DOSAGE_OBJECT: (_FIXTURES / 'clingen_dosage.csv').read_bytes(),
            gene_disease_backend._PANELAPP_OBJECT: (_FIXTURES / 'panelapp.json').read_bytes(),
        }

    monkeypatch.setattr(gene_disease_backend, '_download_reference_blobs', fake_download)
    backend = _from_env()
    assert isinstance(backend, gene_disease_backend.LiveBackend)
    assert seen['bucket'] == 'resources-bucket'
