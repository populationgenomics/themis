"""Tests for the post-run diagnostics + teardown (`themis.litcache.report`).

Manifests are constructed and written straight to a fake-gcs-server bucket (Docker-
gated via the shared `gcs_bucket` fixture); the seed prefix is seeded with throwaway
bytes. The Beam coupling (mapping a `PipelineResult` to the counter snapshot) is
exercised in `test_ingest_beam.py`.
"""

from __future__ import annotations

import datetime

from google.cloud import storage as gcs
from google.protobuf import timestamp_pb2

from themis.litcache import report
from themis.litcache.models import litcache_pb2

_NOW = datetime.datetime(2026, 6, 25, tzinfo=datetime.UTC)


def _timestamp() -> timestamp_pb2.Timestamp:
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(_NOW)
    return ts


def _manifest(doc_id: str, *, has_text_layer: bool | None) -> litcache_pb2.Manifest:
    """A minimal manifest with one pdf source whose revision carries `has_text_layer`."""
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
        files=[],
    )


def _write(bucket: gcs.Bucket, manifest: litcache_pb2.Manifest) -> None:
    bucket.blob(f'papers/{manifest.doc_id}/manifest.pb').upload_from_string(manifest.SerializeToString())


def test_build_report_totals_and_flags_only_explicit_false_manifests(gcs_bucket: gcs.Bucket) -> None:
    _write(gcs_bucket, _manifest('aaa', has_text_layer=False))
    _write(gcs_bucket, _manifest('bbb', has_text_layer=True))
    _write(gcs_bucket, _manifest('ccc', has_text_layer=None))  # XML source of truth: probe skipped
    gcs_bucket.blob('papers/aaa/sources/pdf/0.pdf').upload_from_string(b'not a manifest')
    counters = {'papers_seen': 4, 'paper_written': 3, 'paper_skipped': 0}

    rep = report.build_report(gcs_bucket, unpaired_seeds=['ingest/lonely.json'], counters=counters, dead_lettered=1)

    assert rep.papers_total == 3
    assert rep.flagged == [report.FlaggedPaper(doc_id='aaa', claim_key='doi:10.1/aaa', source_handles=['pdf'])]
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


def test_write_dead_letter_summary_writes_nothing_when_clean(gcs_bucket: gcs.Bucket) -> None:
    count = report.write_dead_letter_summary(
        gcs_bucket, records_prefix='diagnostics/dead_letters/', summary_path='diagnostics/dead_letters.jsonl'
    )
    assert count == 0
    assert not gcs_bucket.blob('diagnostics/dead_letters.jsonl').exists()
