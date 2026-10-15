"""Tests for `themis.litcache.records`: the ranking writers move refs by, and what readers resolve through."""

from __future__ import annotations

import pytest
from google.protobuf import timestamp_pb2

from themis.litcache import records
from themis.litcache.models import litcache_pb2


def _record(
    converter: litcache_pb2.Converter, *, seconds: int = 0, deps: tuple[str, ...] = ('rev',)
) -> litcache_pb2.Record:
    construction = litcache_pb2.RenderingConstruction(converter=converter, converter_version='1')
    if converter == litcache_pb2.Converter.CONVERTER_LLM_OCR:
        construction.model = 'claude-sonnet-5'
    return litcache_pb2.Record(
        deps=list(deps), created_at=timestamp_pb2.Timestamp(seconds=seconds), rendering=construction
    )


def test_a_rendering_from_xml_outranks_a_transcription_whatever_their_ages() -> None:
    ranked = [
        ('ocr', _record(litcache_pb2.Converter.CONVERTER_LLM_OCR, seconds=200)),
        ('docling', _record(litcache_pb2.Converter.CONVERTER_DOCLING, seconds=300)),
        ('xml', _record(litcache_pb2.Converter.CONVERTER_LITDOWN, seconds=100)),
    ]
    assert records.best_rendering(ranked) == 'xml'
    assert records.best_rendering(ranked[:2]) == 'ocr'  # llm-ocr outranks the older docling route


def test_a_tie_on_converter_goes_to_the_newer_rendering() -> None:
    older = _record(litcache_pb2.Converter.CONVERTER_LITDOWN, seconds=100)
    newer = _record(litcache_pb2.Converter.CONVERTER_LITDOWN, seconds=200)
    assert records.best_rendering([('older', older), ('newer', newer)]) == 'newer'


def test_no_rendering_ranks_when_there_is_none() -> None:
    assert records.best_rendering([]) is None


def test_markdown_follows_the_ref() -> None:
    manifest = litcache_pb2.Manifest(doc_id='d')
    assert records.markdown(manifest) is None
    manifest.records['m1'].CopyFrom(_record(litcache_pb2.Converter.CONVERTER_LITDOWN))
    manifest.refs[records.MARKDOWN_REF] = 'm1'
    resolved = records.markdown(manifest)
    assert resolved is not None
    assert resolved[0] == 'm1'


def test_a_ref_naming_no_held_rendering_is_a_corrupt_manifest() -> None:
    manifest = litcache_pb2.Manifest(doc_id='d')
    manifest.refs[records.MARKDOWN_REF] = 'gone'
    with pytest.raises(ValueError, match='gone'):
        records.markdown(manifest)


def test_a_dep_resolves_to_every_lineage_holding_its_bytes() -> None:
    manifest = litcache_pb2.Manifest(doc_id='d')
    for handle in ('pdf', 'pdf-oa'):
        manifest.sources.add(handle=handle).revisions.add(hash='shared')
    manifest.sources.add(handle='xml').revisions.add(hash='other')
    found = records.source_revisions(manifest, _record(litcache_pb2.Converter.CONVERTER_LLM_OCR, deps=('shared',)))
    assert sorted(source.handle for source, _ in found) == ['pdf', 'pdf-oa']
