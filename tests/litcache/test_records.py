"""Tests for `themis.litcache.records`: the ranking writers move refs by, and what readers resolve through."""

from __future__ import annotations

import itertools

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


def _ref_after(*puts: tuple[str, litcache_pb2.Record]) -> str:
    manifest = litcache_pb2.Manifest(doc_id='d')
    for key, record in puts:
        records.put_record(manifest, key, record, records.MARKDOWN_REF)
    return manifest.refs[records.MARKDOWN_REF]


@pytest.mark.parametrize('order', itertools.permutations(range(3)))
def test_a_rendering_from_xml_outranks_a_transcription_whatever_their_ages(order: tuple[int, ...]) -> None:
    ranked = [
        ('ocr', _record(litcache_pb2.Converter.CONVERTER_LLM_OCR, seconds=200)),
        ('docling', _record(litcache_pb2.Converter.CONVERTER_DOCLING, seconds=300)),
        ('xml', _record(litcache_pb2.Converter.CONVERTER_LITDOWN, seconds=100)),
    ]
    assert _ref_after(*(ranked[i] for i in order)) == 'xml'
    assert _ref_after(*(ranked[i] for i in order if i < 2)) == 'ocr'  # llm-ocr outranks the older docling route


def test_a_tie_on_converter_goes_to_the_newer_rendering() -> None:
    older = _record(litcache_pb2.Converter.CONVERTER_LITDOWN, seconds=100)
    newer = _record(litcache_pb2.Converter.CONVERTER_LITDOWN, seconds=200)
    assert _ref_after(('newer', newer), ('older', older)) == 'newer'
    assert _ref_after(('older', older), ('newer', newer)) == 'newer'


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


def _holding(**records_by_key: litcache_pb2.Record) -> litcache_pb2.Manifest:
    manifest = litcache_pb2.Manifest(doc_id='d')
    for key, record in records_by_key.items():
        manifest.records[key].CopyFrom(record)
    return manifest


def test_a_new_record_takes_a_ref_that_names_nothing() -> None:
    manifest = _holding()
    assert records.put_record(manifest, 'm1', _record(litcache_pb2.Converter.CONVERTER_DOCLING), records.MARKDOWN_REF)
    assert manifest.refs[records.MARKDOWN_REF] == 'm1'


def test_bytes_produced_again_keep_the_newer_run_s_record() -> None:
    newer = _record(litcache_pb2.Converter.CONVERTER_LITDOWN, seconds=200)
    manifest = _holding(m1=newer)

    older = _record(litcache_pb2.Converter.CONVERTER_LITDOWN, seconds=100, deps=('other-rev',))
    assert not records.put_record(manifest, 'm1', older, None)
    assert manifest.records['m1'] == newer

    newest = _record(litcache_pb2.Converter.CONVERTER_LITDOWN, seconds=300, deps=('other-rev',))
    assert records.put_record(manifest, 'm1', newest, None)
    assert manifest.records['m1'] == newest


def test_a_ref_moves_only_to_a_record_that_outranks_the_one_named() -> None:
    manifest = _holding(ocr=_record(litcache_pb2.Converter.CONVERTER_LLM_OCR, seconds=100))
    manifest.refs[records.MARKDOWN_REF] = 'ocr'

    assert records.put_record(
        manifest, 'docling', _record(litcache_pb2.Converter.CONVERTER_DOCLING), records.MARKDOWN_REF
    )
    assert manifest.refs[records.MARKDOWN_REF] == 'ocr'  # held, not named
    records.put_record(manifest, 'xml', _record(litcache_pb2.Converter.CONVERTER_LITDOWN), records.MARKDOWN_REF)
    assert manifest.refs[records.MARKDOWN_REF] == 'xml'


def test_a_ref_never_moves_to_a_record_the_writer_is_not_adding() -> None:
    # A writer whose build ranks differently may disagree with the ref, but it moves the ref only to its own record.
    manifest = _holding(
        docling=_record(litcache_pb2.Converter.CONVERTER_DOCLING), xml=_record(litcache_pb2.Converter.CONVERTER_LITDOWN)
    )
    manifest.refs[records.MARKDOWN_REF] = 'docling'
    records.put_record(
        manifest, 'old-docling', _record(litcache_pb2.Converter.CONVERTER_DOCLING, seconds=-1), records.MARKDOWN_REF
    )
    assert manifest.refs[records.MARKDOWN_REF] == 'docling'


def test_a_record_of_a_kind_with_no_ranking_cannot_take_a_ref() -> None:
    with pytest.raises(records.RecordRefusedError, match='no ranking'):
        records.put_record(_holding(), 'k', litcache_pb2.Record(deps=['rev']), records.MARKDOWN_REF)


def test_a_ref_naming_a_record_the_manifest_lacks_is_refused() -> None:
    manifest = _holding()
    manifest.refs[records.MARKDOWN_REF] = 'gone'
    with pytest.raises(records.RecordRefusedError, match='gone'):
        records.put_record(manifest, 'm1', _record(litcache_pb2.Converter.CONVERTER_LITDOWN), records.MARKDOWN_REF)


def test_a_run_finishing_at_the_same_moment_keeps_the_held_record() -> None:
    held = _record(litcache_pb2.Converter.CONVERTER_LITDOWN, seconds=100)
    manifest = _holding(m1=held)
    assert not records.put_record(
        manifest, 'm1', _record(litcache_pb2.Converter.CONVERTER_LITDOWN, seconds=100, deps=('other',)), None
    )
    assert manifest.records['m1'] == held


def test_a_re_delivered_record_that_outranks_the_named_one_takes_the_ref() -> None:
    manifest = _holding(
        docling=_record(litcache_pb2.Converter.CONVERTER_DOCLING), xml=_record(litcache_pb2.Converter.CONVERTER_LITDOWN)
    )
    manifest.refs[records.MARKDOWN_REF] = 'docling'
    assert records.put_record(manifest, 'xml', manifest.records['xml'], records.MARKDOWN_REF)
    assert manifest.refs[records.MARKDOWN_REF] == 'xml'


def test_bytes_held_as_one_kind_are_refused_as_another() -> None:
    manifest = _holding(m1=_record(litcache_pb2.Converter.CONVERTER_LITDOWN))
    with pytest.raises(records.RecordRefusedError, match='held as a rendering'):
        records.put_record(
            manifest, 'm1', litcache_pb2.Record(deps=['rev'], created_at=timestamp_pb2.Timestamp(seconds=9)), None
        )
