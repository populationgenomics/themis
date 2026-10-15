"""Tests for the one-off manifest rewrite onto records and refs (`tools.litcache.rewrite_manifests`).

The pure `rewrite` runs offline; `run` and `verify` run against a fake-gcs-server bucket (Docker-gated via the shared
`gcs_bucket` fixture), so the generation-matched write-back is the production one.
"""

from __future__ import annotations

import pytest
from google.cloud import storage as gcs
from google.protobuf import timestamp_pb2

from themis.litcache import records, writer
from themis.litcache.models import litcache_pb2
from tools.litcache import rewrite_manifests

_DOC = '9f3a0000-0000-4000-8000-0000000000aa'


def _legacy_manifest(doc_id: str = _DOC) -> litcache_pb2.Manifest:
    """A manifest as written before records: renderings in the old map, a fetched file with no hash."""
    manifest = litcache_pb2.Manifest(doc_id=doc_id)
    pdf = manifest.sources.add(handle='pdf', media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF)
    pdf.access.unknown.SetInParent()
    pdf.revisions.add(hash='pdfrev')
    xml = manifest.sources.add(handle='xml', media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_XML)
    xml.access.free_to_read.SetInParent()
    xml.revisions.add(hash='xmlrev')
    manifest.renderings['docling-md'].CopyFrom(
        litcache_pb2.Rendering(
            from_source='pdf',
            from_revision='pdfrev',
            converter=litcache_pb2.Converter.CONVERTER_DOCLING,
            converter_version='2.0.0',
            created_at=timestamp_pb2.Timestamp(seconds=200),
        )
    )
    manifest.renderings['litdown-md'].CopyFrom(
        litcache_pb2.Rendering(
            from_source='xml',
            from_revision='xmlrev',
            converter=litcache_pb2.Converter.CONVERTER_LITDOWN,
            converter_version='0.7.0',
            created_at=timestamp_pb2.Timestamp(seconds=100),
        )
    )
    manifest.files.add(
        role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_SUPPLEMENTARY,
        name='mmc1.xlsx',
        path='supplementary/abc123.xlsx',
    )
    manifest.files.add(role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_SUPPLEMENTARY, name='unfetched.pdf')
    return manifest


def test_rewrite_copies_renderings_into_records_and_points_the_ref_at_the_best() -> None:
    result = rewrite_manifests.rewrite(_legacy_manifest())
    assert result is not None
    assert list(result.records['docling-md'].deps) == ['pdfrev']
    assert result.records['litdown-md'].rendering.converter == litcache_pb2.Converter.CONVERTER_LITDOWN
    assert result.refs[records.MARKDOWN_REF] == 'litdown-md'  # xml outranks the newer docling rendering
    assert set(result.renderings) == {'docling-md', 'litdown-md'}  # left for readers on the previous code
    files = {f.name: f for f in result.files}
    assert files['mmc1.xlsx'].hash == 'abc123'
    assert not files['unfetched.pdf'].hash


def test_rewrite_keeps_a_model_on_a_transcription() -> None:
    manifest = _legacy_manifest()
    manifest.renderings['ocr-md'].CopyFrom(
        litcache_pb2.Rendering(
            from_source='pdf',
            from_revision='pdfrev',
            converter=litcache_pb2.Converter.CONVERTER_LLM_OCR,
            converter_version='1.1',
            model='claude-sonnet-5',
        )
    )
    result = rewrite_manifests.rewrite(manifest)
    assert result is not None
    assert result.records['ocr-md'].rendering.model == 'claude-sonnet-5'


def test_a_rewritten_manifest_needs_no_second_rewrite() -> None:
    once = rewrite_manifests.rewrite(_legacy_manifest())
    assert once is not None
    assert rewrite_manifests.rewrite(once) is None
    assert rewrite_manifests.unfinished(once) is None


def test_rewrite_never_moves_a_ref_a_writer_has_set() -> None:
    # After the new writers deploy, a writer that adds a rendering sets the ref by its own ranking; the re-run
    # rewrite must not second-guess it.
    manifest = _legacy_manifest()
    manifest.records['ocr-md'].CopyFrom(
        litcache_pb2.Record(
            deps=['pdfrev'],
            rendering=litcache_pb2.RenderingConstruction(
                converter=litcache_pb2.Converter.CONVERTER_LLM_OCR, converter_version='1.1', model='m'
            ),
        )
    )
    manifest.refs[records.MARKDOWN_REF] = 'ocr-md'
    result = rewrite_manifests.rewrite(manifest)
    assert result is not None
    assert result.refs[records.MARKDOWN_REF] == 'ocr-md'


def test_rewrite_refuses_a_rendering_of_a_revision_no_lineage_holds() -> None:
    manifest = _legacy_manifest()
    manifest.renderings['docling-md'].from_revision = 'gone'
    with pytest.raises(ValueError, match='deps_resolve'):
        rewrite_manifests.rewrite(manifest)


def test_unfinished_names_what_is_missing() -> None:
    assert rewrite_manifests.unfinished(_legacy_manifest()) == '2 rendering(s) without a record'
    assert rewrite_manifests.unfinished(litcache_pb2.Manifest(doc_id=_DOC)) is None  # nothing to rewrite


def _store(bucket: gcs.Bucket, manifest: litcache_pb2.Manifest) -> None:
    bucket.blob(writer.manifest_path(manifest.doc_id)).upload_from_string(manifest.SerializeToString())


def _load(bucket: gcs.Bucket, doc_id: str = _DOC) -> litcache_pb2.Manifest:
    return litcache_pb2.Manifest.FromString(bucket.blob(writer.manifest_path(doc_id)).download_as_bytes())


def test_a_dry_run_writes_nothing(gcs_bucket: gcs.Bucket) -> None:
    _store(gcs_bucket, _legacy_manifest())
    report = rewrite_manifests.run(gcs_bucket, dry_run=True, workers=2)
    assert (report.manifests, report.rewritten, report.failures) == (1, [_DOC], [])
    assert not _load(gcs_bucket).records


def test_a_run_rewrites_the_corpus_and_a_second_run_changes_nothing(gcs_bucket: gcs.Bucket) -> None:
    _store(gcs_bucket, _legacy_manifest())
    assert rewrite_manifests.verify(gcs_bucket, workers=2).failures

    first = rewrite_manifests.run(gcs_bucket, dry_run=False, workers=2)
    assert (first.rewritten, first.failures) == ([_DOC], [])
    assert _load(gcs_bucket).refs[records.MARKDOWN_REF] == 'litdown-md'

    second = rewrite_manifests.run(gcs_bucket, dry_run=False, workers=2)
    assert (second.rewritten, second.failures) == ([], [])
    assert rewrite_manifests.verify(gcs_bucket, workers=2).failures == []


def test_a_manifest_that_cannot_be_rewritten_is_reported_and_left_alone(gcs_bucket: gcs.Bucket) -> None:
    broken = _legacy_manifest()
    broken.renderings['docling-md'].from_revision = 'gone'
    _store(gcs_bucket, broken)
    report = rewrite_manifests.run(gcs_bucket, dry_run=False, workers=2)
    assert report.rewritten == []
    (failure,) = report.failures
    assert failure.doc_id == _DOC
    assert 'deps_resolve' in failure.reason
    assert _load(gcs_bucket) == broken


def test_an_unchanged_manifest_that_breaks_its_constraints_is_reported() -> None:
    # Every writer validates the whole manifest, so one that breaks a constraint could never be written again.
    manifest = litcache_pb2.Manifest(doc_id=_DOC)
    manifest.sources.add(handle='pdf')  # a lineage with no access and no revisions
    with pytest.raises(ValueError, match='breaks its constraints'):
        rewrite_manifests.rewrite(manifest)
    assert 'breaks its constraints' in (rewrite_manifests.unfinished(manifest) or '')


def test_one_unreadable_manifest_is_a_failure_not_the_end_of_the_run(gcs_bucket: gcs.Bucket) -> None:
    other = '9f3a0000-0000-4000-8000-0000000000bb'
    _store(gcs_bucket, _legacy_manifest())
    gcs_bucket.blob(writer.manifest_path(other)).upload_from_string(b'\xff\xff not a manifest')
    report = rewrite_manifests.run(gcs_bucket, dry_run=False, workers=2)
    assert report.rewritten == [_DOC]
    assert [f.doc_id for f in report.failures] == [other]
    assert [f.doc_id for f in rewrite_manifests.verify(gcs_bucket, workers=2).failures] == [other]


@pytest.mark.parametrize('workers', ['0', '-1'])
def test_workers_must_be_positive(workers: str) -> None:
    with pytest.raises(SystemExit):
        rewrite_manifests._parse_args(['--project', 'cpg-themis-test', '--workers', workers])


def test_a_failure_exits_non_zero_and_names_the_paper(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = _legacy_manifest()
    broken.renderings['docling-md'].from_revision = 'gone'
    _store(gcs_bucket, broken)
    monkeypatch.setattr(rewrite_manifests, '_open_bucket', lambda _project, _name, _workers: gcs_bucket)

    code = rewrite_manifests.main(['--project', 'cpg-themis-test', '--dry-run'])

    assert code == 1
    assert _DOC in capsys.readouterr().err
