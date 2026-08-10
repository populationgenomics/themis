"""Tests for the post-run diagnostics + teardown (`themis.litcache.report`).

Manifests are constructed and written straight to a fake-gcs-server bucket (Docker-
gated via the shared `gcs_bucket` fixture); the seed prefix is seeded with throwaway
bytes. The Beam coupling (mapping a `PipelineResult` to the counter snapshot) is
exercised in `test_ingest_beam.py`.
"""

from __future__ import annotations

import datetime
import json

from google.cloud import storage as gcs
from google.protobuf import timestamp_pb2

from themis.litcache import ingest_beam, report
from themis.litcache.models import litcache_pb2

_NOW = datetime.datetime(2026, 6, 25, tzinfo=datetime.UTC)


def _timestamp() -> timestamp_pb2.Timestamp:
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(_NOW)
    return ts


def _manifest(doc_id: str, *, has_text_layer: bool | None) -> litcache_pb2.Manifest:
    """A minimal manifest with one pdf source whose revision carries `has_text_layer`."""
    rendering = litcache_pb2.Rendering(
        from_source='pdf',
        from_revision='0',
        converter=litcache_pb2.Converter.CONVERTER_DOCLING,
        converter_version='2.0.0',
        created_at=_timestamp(),
    )
    revision = litcache_pb2.Revision(hash='0', kind=litcache_pb2.SourceKind.SOURCE_KIND_SEED, captured_at=_timestamp())
    if has_text_layer is not None:
        revision.has_text_layer = has_text_layer
    source = litcache_pb2.Source(
        handle='pdf',
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
        licence='',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED,
        access=litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess()),
        revisions=[revision],
    )
    return litcache_pb2.Manifest(
        doc_id=doc_id,
        external_ids=litcache_pb2.ExternalIds(doi=f'10.1/{doc_id}'),
        claim_key=f'doi:10.1/{doc_id}',
        equivalence=litcache_pb2.Equivalence(edges=[], canonical_doc_id=doc_id),
        retraction=litcache_pb2.Retraction(),
        sources=[source],
        renderings={'0': rendering},
        files=[],
    )


def _write(bucket: gcs.Bucket, manifest: litcache_pb2.Manifest) -> None:
    bucket.blob(f'papers/{manifest.doc_id}/manifest.pb').upload_from_string(manifest.SerializeToString())


def test_flagged_lists_only_explicit_false(gcs_bucket: gcs.Bucket) -> None:
    _write(gcs_bucket, _manifest('aaa', has_text_layer=False))
    _write(gcs_bucket, _manifest('bbb', has_text_layer=True))
    _write(gcs_bucket, _manifest('ccc', has_text_layer=None))  # XML source of truth: probe skipped

    flagged = report.flagged_no_text_layer(gcs_bucket)

    assert [f.doc_id for f in flagged] == ['aaa']
    assert flagged[0].claim_key == 'doi:10.1/aaa'
    assert flagged[0].source_handles == ['pdf']


def test_flagged_ignores_nested_non_manifest_keys(gcs_bucket: gcs.Bucket) -> None:
    _write(gcs_bucket, _manifest('aaa', has_text_layer=False))
    gcs_bucket.blob('papers/aaa/sources/pdf/0.pdf').upload_from_string(b'not a manifest')

    assert [f.doc_id for f in report.flagged_no_text_layer(gcs_bucket)] == ['aaa']


def test_build_report_totals(gcs_bucket: gcs.Bucket) -> None:
    _write(gcs_bucket, _manifest('aaa', has_text_layer=False))
    _write(gcs_bucket, _manifest('bbb', has_text_layer=True))
    counters = {'papers_seen': 3, 'paper_written': 2, 'paper_skipped': 0}

    rep = report.build_report(gcs_bucket, unpaired_seeds=['ingest/lonely.json'], counters=counters, dead_lettered=1)

    assert rep.papers_total == 2
    assert [f.doc_id for f in rep.flagged] == ['aaa']
    assert rep.unpaired_seeds == ['ingest/lonely.json']
    assert rep.counters == counters
    assert rep.dead_lettered == 1


def test_render_report_mentions_flagged_and_unpaired(gcs_bucket: gcs.Bucket) -> None:
    _write(gcs_bucket, _manifest('aaa', has_text_layer=False))
    rep = report.build_report(
        gcs_bucket, unpaired_seeds=['ingest/lonely.json'], counters={'papers_seen': 1}, dead_lettered=0
    )

    rendered = report.render_report(rep)

    assert 'no_text_layer: 1' in rendered
    assert 'aaa (doi:10.1/aaa): pdf' in rendered
    assert 'unpaired: ingest/lonely.json' in rendered


def test_teardown_deletes_only_the_seed_prefix(gcs_bucket: gcs.Bucket) -> None:
    gcs_bucket.blob('ingest/a.json').upload_from_string(b'{}')
    gcs_bucket.blob('ingest/a.pdf').upload_from_string(b'%PDF')
    gcs_bucket.blob('papers/aaa/manifest.pb').upload_from_string(b'{}')  # not under ingest/: survives

    deleted = report.teardown_seed(gcs_bucket)

    assert deleted == 2
    assert list(gcs_bucket.list_blobs(prefix='ingest/')) == []
    assert gcs_bucket.blob('papers/aaa/manifest.pb').exists()


def test_write_dead_letter_summary_consolidates_records(gcs_bucket: gcs.Bucket) -> None:
    # Records come from the real producer, so the shape this summary documents is pinned to
    # what the pipeline writes rather than to a hand-written copy that can drift from it.
    prefix, summary_path = ingest_beam.dead_letter_paths(_NOW)
    ingest_beam._write_dead_letter(gcs_bucket, prefix=prefix, key='10.1/a', reason='metadata unresolved', doi='10.1/a')
    # an extract-stage failure is keyed by the seed object, before identity is known
    ingest_beam._write_dead_letter(gcs_bucket, prefix=prefix, key='ingest/b.json', reason='ValueError: boom')

    count = report.write_dead_letter_summary(gcs_bucket, records_prefix=prefix, summary_path=summary_path)

    assert count == 2
    body = gcs_bucket.blob(summary_path).download_as_bytes().decode('utf-8')
    records = [json.loads(line) for line in body.splitlines()]
    assert {r['key'] for r in records} == {'10.1/a', 'ingest/b.json'}
    assert {r['reason'] for r in records} == {'metadata unresolved', 'ValueError: boom'}


def test_write_dead_letter_summary_writes_nothing_when_clean(gcs_bucket: gcs.Bucket) -> None:
    count = report.write_dead_letter_summary(
        gcs_bucket, records_prefix='diagnostics/dead_letters/', summary_path='diagnostics/dead_letters.jsonl'
    )
    assert count == 0
    assert not gcs_bucket.blob('diagnostics/dead_letters.jsonl').exists()
