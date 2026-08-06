"""Entrypoint wiring: backend selection and fail-loud fixture-seed parsing."""

from __future__ import annotations

import asyncio
import json

import pytest

from themis.rpc import literature_pb2
from themis.services.evidence import __main__ as main_mod
from themis.services.evidence.literature import backend as literature_backend

_ONE_PAPER = {
    'doc-1': {
        'title': 'A paper',
        'markdown': {'gcs_uri': 'gs://corpus/doc-1/rendering.md', 'from_xml': True},
        'files': [
            {'name': 'f1.png', 'role': 'FIGURE', 'media_type': 'image/png', 'gcs_uri': 'gs://corpus/doc-1/f1.png'}
        ],
        'markdown_locations': {'a quote': [3, 10]},
    }
}


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_BACKEND'):
        main_mod.build_literature_backend()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        main_mod.build_literature_backend()


def test_live_backend_requires_the_fulltext_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'live')
    monkeypatch.delenv('THEMIS_FULLTEXT_BUCKET', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_FULLTEXT_BUCKET'):
        main_mod.build_literature_backend()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_EVIDENCE_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_EVIDENCE_FIXTURE'):
        main_mod.build_literature_backend()


def test_fixture_seed_must_be_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_EVIDENCE_FIXTURE', '{not json')
    with pytest.raises(SystemExit, match='not valid JSON'):
        main_mod.build_literature_backend()


def test_fixture_paper_requires_a_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_EVIDENCE_FIXTURE', json.dumps({'doc-1': {'markdown': {'gcs_uri': 'gs://x'}}}))
    with pytest.raises(SystemExit, match='title'):
        main_mod.build_literature_backend()


def test_fixture_markdown_locations_must_be_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.setenv(
        'THEMIS_EVIDENCE_FIXTURE',
        json.dumps({'doc-1': {'title': 'A', 'markdown_locations': {'q': [1, 2, 3]}}}),
    )
    with pytest.raises(SystemExit, match='markdown_locations'):
        main_mod.build_literature_backend()


def test_empty_corpus_is_an_explicit_valid_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_EVIDENCE_FIXTURE', '{}')
    backend = main_mod.build_literature_backend()
    with pytest.raises(literature_backend.UnknownPaperError):
        asyncio.run(backend.describe_paper('doc-1'))


def test_valid_seed_builds_a_describable_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_EVIDENCE_FIXTURE', json.dumps(_ONE_PAPER))
    backend = main_mod.build_literature_backend()
    info = asyncio.run(backend.describe_paper('doc-1'))
    assert info.title == 'A paper'
    assert info.default_representation == literature_pb2.REPRESENTATION_MARKDOWN
    assert [f.name for f in info.files] == ['f1.png']


def test_fixture_paper_rejects_an_unknown_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    # `markdown_location` (singular) is a typo of `markdown_locations` — silently dropped before, now loud.
    monkeypatch.setenv('THEMIS_EVIDENCE_FIXTURE', json.dumps({'doc-1': {'title': 'A', 'markdown_location': {}}}))
    with pytest.raises(SystemExit, match='unknown field'):
        main_mod.build_literature_backend()


def test_fixture_markdown_object_rejects_an_unknown_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_BACKEND', 'fixture')
    # `fromXml` (camelCase) is a typo of `from_xml` inside the nested markdown object — now loud.
    fixture = {'doc-1': {'title': 'A', 'markdown': {'gcs_uri': 'gs://x', 'fromXml': True}}}
    monkeypatch.setenv('THEMIS_EVIDENCE_FIXTURE', json.dumps(fixture))
    with pytest.raises(SystemExit, match='unknown field'):
        main_mod.build_literature_backend()
