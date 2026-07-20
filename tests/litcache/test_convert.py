"""Tests for `themis.litcache.convert` over the committed fixtures.

Two branches: the non-OA branch renders the synthetic `nonoa/docling.json` via
docling-core (`pdf-derived`); the OA branch renders the real `oa/fulltext.xml`
JATS via litdown (`xml-faithful`).
"""

from __future__ import annotations

import datetime
import pathlib

import pydantic
import pytest

from themis.litcache import convert
from themis.litcache.models import litcache_pb2

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_NONOA = _FIXTURES / 'nonoa' / 'docling.json'
_OA_JATS = _FIXTURES / 'oa' / 'fulltext.xml'
_CREATED_AT = datetime.datetime(2026, 6, 25, 12, 0, tzinfo=datetime.UTC)
# The hash of the source revision the markdown is produced from — convert records
# it verbatim (the writer is what verifies it against the source it writes).
_FROM_REVISION = '1111111111111111111111111111111111111111111111111111111111111111'

_ABSTRACT = (
    'This document is entirely synthetic. It exists to exercise the '
    'docling-to-markdown rendering path (S8) without redistributing any '
    'copyrighted full text.'
)
# docling joins block items with blank lines; headings carry their `#` prefix.
_EXPECTED_MARKDOWN = '\n\n'.join(
    [
        '# Synthetic Non-OA Fixture: A Case Study in Nothing',
        '## Abstract',
        _ABSTRACT,
        '## Methods',
        'We invented three fictional samples and measured imaginary quantities.',
        '## Results',
        'Sample 1 was alpha. Sample 2 was beta. Sample 3 was gamma. No real data were involved.',
    ]
)


def _convert() -> convert.Conversion:
    return convert.convert_docling(
        _NONOA.read_bytes(), from_source='pdf', from_revision=_FROM_REVISION, created_at=_CREATED_AT
    )


def test_renders_expected_markdown() -> None:
    assert _convert().markdown == _EXPECTED_MARKDOWN


def test_rendering_record_fields() -> None:
    rendering = _convert().rendering
    assert rendering.converter == litcache_pb2.Converter.CONVERTER_DOCLING
    assert rendering.converter_version  # docling-core's installed version
    assert rendering.from_source == 'pdf'
    assert rendering.from_revision == _FROM_REVISION
    assert not rendering.HasField('model')  # docling is not a model-driven converter
    assert rendering.created_at.ToDatetime(tzinfo=datetime.UTC) == _CREATED_AT


def test_accepts_str_and_bytes_identically() -> None:
    from_bytes = convert.convert_docling(
        _NONOA.read_bytes(), from_source='pdf', from_revision=_FROM_REVISION, created_at=_CREATED_AT
    )
    from_str = convert.convert_docling(
        _NONOA.read_text(), from_source='pdf', from_revision=_FROM_REVISION, created_at=_CREATED_AT
    )
    assert from_bytes.markdown == from_str.markdown


def test_invalid_json_fails_loud() -> None:
    # docling-core validates with pydantic; malformed json fails its parse.
    with pytest.raises(pydantic.ValidationError):
        convert.convert_docling(b'{not valid', from_source='pdf', from_revision=_FROM_REVISION, created_at=_CREATED_AT)


def test_empty_document_fails_loud() -> None:
    # A schema-valid DoclingDocument with no body items exports to empty markdown.
    empty = b'{"schema_name": "DoclingDocument", "version": "1.10.0", "name": "empty"}'
    with pytest.raises(ValueError, match='empty markdown'):
        convert.convert_docling(empty, from_source='pdf', from_revision=_FROM_REVISION, created_at=_CREATED_AT)


def _convert_jats() -> convert.Conversion:
    return convert.convert_jats(
        _OA_JATS.read_bytes(), from_source='xml', from_revision=_FROM_REVISION, created_at=_CREATED_AT
    )


def test_jats_renders_markdown() -> None:
    markdown = _convert_jats().markdown
    # litdown carries the JATS title through as the top-level heading.
    assert markdown.startswith('# Whole exome sequencing in 342 congenital cardiac')


def test_jats_rendering_record_fields() -> None:
    rendering = _convert_jats().rendering
    assert rendering.converter == litcache_pb2.Converter.CONVERTER_LITDOWN
    assert rendering.converter_version  # litdown's installed version
    assert rendering.from_source == 'xml'
    assert rendering.from_revision == _FROM_REVISION
    assert not rendering.HasField('model')  # litdown is not a model-driven converter
    assert rendering.created_at.ToDatetime(tzinfo=datetime.UTC) == _CREATED_AT


def test_jats_empty_conversion_fails_loud() -> None:
    # A well-formed article with an empty body converts to empty markdown.
    empty = b'<article><body></body></article>'
    with pytest.raises(ValueError, match='empty markdown'):
        convert.convert_jats(empty, from_source='xml', from_revision=_FROM_REVISION, created_at=_CREATED_AT)
