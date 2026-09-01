"""The clinvar interface's env contract: which adapter each selector value builds, fail-loud."""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib

import clinvar_proto
import defusedxml.ElementTree
import httpx2
import pytest
from google.protobuf import json_format

from themis.rpc import auth_pb2, clinvar_pb2
from themis.services.evidence import deps as deps_mod
from themis.services.evidence import errors
from themis.services.evidence.clinvar import backend as clinvar_backend
from themis.services.evidence.clinvar import config

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_VCV = 'VCV001731988'


def _seeded_archive() -> dict[str, object]:
    """The committed VCV XML in the JSON form a seed carries, over the adapter's own converter path.

    Written by hand the archive would be a shape ClinVar never emits; converted, it is the same
    message the live adapter builds out of the same bytes.
    """
    root = defusedxml.ElementTree.fromstring((_FIXTURES / 'clinvar_vcv.xml').read_bytes())
    archive = root.find('VariationArchive')
    assert archive is not None, 'the committed VCV fixture carries no VariationArchive'
    return json_format.MessageToDict(clinvar_proto.xml_converter.VariationArchiveType(archive))


_SEED = json.dumps(
    {
        'describe_variant': {
            f'{_VCV}:BRCA1': {'this_variant': {'clinvar_id': _VCV}, 'variationArchive': _seeded_archive()}
        },
        'search_coding_span': {'NM_007294.4:5074:5076': {'transcript': 'NM_007294.4'}},
    }
)


async def _unreachable_session_resolver(session_token: str) -> auth_pb2.SessionContext:
    raise AssertionError('building a backend resolves no session')


def _from_env() -> clinvar_backend.ClinVarBackend:
    """Select the backend as the entrypoint would; no test here reaches an upstream."""
    return config.backend_from_env(
        deps_mod.Deps(
            session_resolver=_unreachable_session_resolver,
            http_client=httpx2.AsyncClient(),
            stack=contextlib.AsyncExitStack(),
        )
    )


def _describe_variant(backend: clinvar_backend.ClinVarBackend, gene: str) -> clinvar_pb2.DescribeVariantResponse:
    return asyncio.run(backend.describe_variant(clinvar_pb2.DescribeVariantRequest(vcv=_VCV, gene=gene)))


def _search_coding_span(
    backend: clinvar_backend.ClinVarBackend, transcript: str
) -> clinvar_pb2.SearchCodingSpanResponse:
    return asyncio.run(
        backend.search_coding_span(
            clinvar_pb2.SearchCodingSpanRequest(transcript=transcript, cds_start=5074, cds_end=5076, max_records=50)
        )
    )


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_CLINVAR_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_CLINVAR_BACKEND'):
        _from_env()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_CLINVAR_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _from_env()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # The seed var, not the selector, is named — the operator has to know which value is missing.
    monkeypatch.setenv('THEMIS_CLINVAR_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_CLINVAR_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_CLINVAR_FIXTURE'):
        _from_env()


def test_fixture_selector_answers_what_it_was_seeded_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each rpc's section reaches its own rpc, and an unseeded key is a stated absence."""
    monkeypatch.setenv('THEMIS_CLINVAR_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_CLINVAR_FIXTURE', _SEED)
    backend = _from_env()
    described = _describe_variant(backend, 'BRCA1')
    assert described.this_variant.clinvar_id == _VCV
    # The archive is a seeded field like any other, so an offline caller reads the same record type.
    assert described.variation_archive.accession == _VCV
    assert described.variation_archive.classified_record.clinical_assertion_list
    assert _search_coding_span(backend, 'NM_007294.4').transcript == 'NM_007294.4'
    with pytest.raises(errors.UnknownVariantError):
        _describe_variant(backend, 'BRCA2')
    with pytest.raises(errors.UnknownVariantError):
        _search_coding_span(backend, 'NM_000059.4')


def test_live_selector_builds_the_live_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    # Constructing it opens nothing: the adapter holds the image's shared client and issues on demand.
    monkeypatch.setenv('THEMIS_CLINVAR_BACKEND', 'live')
    assert isinstance(_from_env(), clinvar_backend.LiveBackend)
