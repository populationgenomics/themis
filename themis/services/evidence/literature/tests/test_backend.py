"""Seed parsing: a malformed fixture corpus fails loud rather than serving a partial one."""

from __future__ import annotations

import asyncio
import json

import pytest

from themis.rpc import literature_pb2
from themis.services.evidence.literature import backend as literature_backend

_VAR_NAME = 'THEMIS_LITERATURE_FIXTURE'

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


def _backend(seed: object) -> literature_backend.FixtureBackend:
    return literature_backend.fixture_backend_from_json(json.dumps(seed), var_name=_VAR_NAME)


def test_an_absent_seed_names_its_source() -> None:
    with pytest.raises(SystemExit, match=_VAR_NAME):
        literature_backend.fixture_backend_from_json(None, var_name=_VAR_NAME)


def test_seed_must_be_valid_json() -> None:
    with pytest.raises(SystemExit, match='not valid JSON'):
        literature_backend.fixture_backend_from_json('{not json', var_name=_VAR_NAME)


def test_seed_must_be_an_object_of_papers() -> None:
    with pytest.raises(SystemExit, match='must be a JSON object'):
        _backend(['doc-1'])


def test_paper_requires_a_title() -> None:
    with pytest.raises(SystemExit, match='title'):
        _backend({'doc-1': {'markdown': {'gcs_uri': 'gs://x'}}})


def test_markdown_locations_must_be_pairs() -> None:
    with pytest.raises(SystemExit, match='markdown_locations'):
        _backend({'doc-1': {'title': 'A', 'markdown_locations': {'q': [1, 2, 3]}}})


def test_pdf_location_rects_must_be_quads() -> None:
    with pytest.raises(SystemExit, match=r'must be \[x, y, w, h\]'):
        _backend({'doc-1': {'title': 'A', 'pdf_locations': {'q': {'page': 0, 'rects': [[1, 2, 3]]}}}})


def test_file_role_must_be_known() -> None:
    file = {'name': 'f', 'role': 'CHART', 'media_type': 'image/png', 'gcs_uri': 'gs://c/f'}
    with pytest.raises(SystemExit, match='role'):
        _backend({'doc-1': {'title': 'A', 'files': [file]}})


def test_paper_rejects_an_unknown_field() -> None:
    # `markdown_location` (singular) is a plausible typo of `markdown_locations`; dropping the field
    # silently would serve the paper with no quote locations at all.
    with pytest.raises(SystemExit, match='unknown field'):
        _backend({'doc-1': {'title': 'A', 'markdown_location': {}}})


def test_markdown_object_rejects_an_unknown_field() -> None:
    # `fromXml` (camelCase) is a plausible typo of `from_xml` inside the nested markdown object.
    with pytest.raises(SystemExit, match='unknown field'):
        _backend({'doc-1': {'title': 'A', 'markdown': {'gcs_uri': 'gs://x', 'fromXml': True}}})


def test_empty_corpus_is_an_explicit_valid_seed() -> None:
    backend = literature_backend.fixture_backend_from_json('{}', var_name=_VAR_NAME)
    with pytest.raises(literature_backend.UnknownPaperError):
        asyncio.run(backend.describe_paper('doc-1'))


def test_a_valid_seed_builds_a_describable_corpus() -> None:
    info = asyncio.run(_backend(_ONE_PAPER).describe_paper('doc-1'))
    assert info.title == 'A paper'
    assert info.default_representation == literature_pb2.REPRESENTATION_MARKDOWN
    assert [f.name for f in info.files] == ['f1.png']
