"""Tests for full-text readiness + the terminal-outcome sidecar (`themis.litcache.outcome`).

Backed by the shared fake-gcs-server `gcs_bucket` fixture (Docker-gated): readiness is derived from a
real `papers/{doc_id}/` layout — a committed manifest, its renderings, and the `.fetch_outcome`
sidecar — the same objects the deployed service reads.
"""

from __future__ import annotations

import datetime
import hashlib

import pytest
from google.cloud import storage as gcs
from google.protobuf import timestamp_pb2

from themis.litcache import outcome, writer
from themis.litcache.models import litcache_pb2

_DOC_ID = 'a1b20000-0000-4000-8000-000000000001'
_AT = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=datetime.UTC)
_PDF_BYTES = b'%PDF-1.7 fake'
_PDF_HASH = hashlib.sha256(_PDF_BYTES).hexdigest()
_MARKDOWN = '# Title\n\nBody.\n'


def _source() -> writer.SourceInput:
    return writer.SourceInput(
        handle='pdf',
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
        kind=litcache_pb2.SourceKind.SOURCE_KIND_SEED,
        data=_PDF_BYTES,
        licence='cc-by',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT,
        access=litcache_pb2.Access(free_to_read=litcache_pb2.FreeToRead()),
        captured_at=datetime.datetime(2026, 8, 3, tzinfo=datetime.UTC),
    )


def _rendering() -> litcache_pb2.Rendering:
    created = timestamp_pb2.Timestamp()
    created.FromDatetime(_AT)
    return litcache_pb2.Rendering(
        from_source='pdf',
        from_revision=_PDF_HASH,
        converter=litcache_pb2.Converter.CONVERTER_DOCLING,
        converter_version='2.0.0',
        created_at=created,
    )


def _metadata() -> bytes:
    metadata = litcache_pb2.PaperMetadata()
    metadata.pubmed.article.medline_citation.pmid.value = '1'
    return metadata.SerializeToString()


def _paper(
    *,
    sources: list[writer.SourceInput],
    renderings: list[writer.RenderingInput],
    external_ids: litcache_pb2.ExternalIds | None = None,
    claim_key: str = 'doi:10.1/x',
) -> writer.PaperInput:
    return writer.PaperInput(
        doc_id=_DOC_ID,
        external_ids=external_ids if external_ids is not None else litcache_pb2.ExternalIds(doi='10.1/x'),
        claim_key=claim_key,
        equivalence=litcache_pb2.Equivalence(canonical_doc_id=_DOC_ID),
        retraction=litcache_pb2.Retraction(),
        sources=sources,
        renderings=renderings,
        metadata=_metadata(),
        files=(),
    )


def _write_pending(bucket: gcs.Bucket) -> None:
    """A paper with a source but no rendering — the conversion-outstanding state."""
    writer.write_paper(bucket, _paper(sources=[_source()], renderings=[]))


def _write_ready(bucket: gcs.Bucket) -> None:
    writer.write_paper(
        bucket,
        _paper(sources=[_source()], renderings=[writer.RenderingInput(rendering=_rendering(), markdown=_MARKDOWN)]),
    )


# --- the sidecar round-trips ---------------------------------------------------------------------


def test_outcome_absent_reads_as_none(gcs_bucket: gcs.Bucket) -> None:
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None


def test_outcome_round_trips(gcs_bucket: gcs.Bucket) -> None:
    written = outcome.FetchOutcome(kind=outcome.OutcomeKind.FAILED, at=_AT, error='ocr timed out')
    outcome.write_outcome(gcs_bucket, _DOC_ID, written)
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) == written


def test_a_later_outcome_supersedes_an_earlier_one(gcs_bucket: gcs.Bucket) -> None:
    outcome.write_outcome(gcs_bucket, _DOC_ID, outcome.FetchOutcome(outcome.OutcomeKind.NO_FULL_TEXT, _AT))
    superseding = outcome.FetchOutcome(outcome.OutcomeKind.FAILED, _AT, error='then a convert failed')
    outcome.write_outcome(gcs_bucket, _DOC_ID, superseding)
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) == superseding


# --- readiness derivation ------------------------------------------------------------------------


def test_unknown_paper_reads_as_none(gcs_bucket: gcs.Bucket) -> None:
    assert outcome.read_readiness(gcs_bucket, 'no-such-doc') is None


def test_a_rendering_is_ready(gcs_bucket: gcs.Bucket) -> None:
    _write_ready(gcs_bucket)
    assert outcome.read_readiness(gcs_bucket, _DOC_ID) is outcome.Readiness.READY


@pytest.mark.parametrize(
    'paper',
    [
        pytest.param(_paper(sources=[_source()], renderings=[]), id='pdf-source-not-yet-converted'),
        pytest.param(_paper(sources=[], renderings=[]), id='freshly-minted-nothing-fetched'),
        pytest.param(
            _paper(sources=[], renderings=[], external_ids=litcache_pb2.ExternalIds(), claim_key='binhash:abc'),
            # No fetchable id and no PDF source: recognisably unproducible from the manifest alone, and
            # still PENDING — the producer settles this class with a marker (`_record_no_full_text`).
            id='no-fetchable-id-no-pdf',
        ),
    ],
)
def test_no_rendering_and_no_marker_is_pending(gcs_bucket: gcs.Bucket, paper: writer.PaperInput) -> None:
    # Only a marker settles a paper, whatever the manifest holds.
    writer.write_paper(gcs_bucket, paper)
    assert outcome.read_readiness(gcs_bucket, _DOC_ID) is outcome.Readiness.PENDING


def test_a_terminal_marker_wins_over_pending(gcs_bucket: gcs.Bucket) -> None:
    # A paper with a source (would read PENDING) but a FAILED marker is terminal, not pending.
    _write_pending(gcs_bucket)
    outcome.write_outcome(gcs_bucket, _DOC_ID, outcome.FetchOutcome(outcome.OutcomeKind.FAILED, _AT))
    assert outcome.read_readiness(gcs_bucket, _DOC_ID) is outcome.Readiness.FAILED


def test_no_full_text_marker_reads_as_no_full_text(gcs_bucket: gcs.Bucket) -> None:
    _write_pending(gcs_bucket)
    outcome.write_outcome(gcs_bucket, _DOC_ID, outcome.FetchOutcome(outcome.OutcomeKind.NO_FULL_TEXT, _AT))
    assert outcome.read_readiness(gcs_bucket, _DOC_ID) is outcome.Readiness.NO_FULL_TEXT


def test_a_rendering_wins_over_a_stale_marker(gcs_bucket: gcs.Bucket) -> None:
    # A rendering that landed after a prior failure marker: READY takes precedence (the manifest is
    # checked first), so a succeeded retry is not masked by an old marker.
    _write_ready(gcs_bucket)
    outcome.write_outcome(gcs_bucket, _DOC_ID, outcome.FetchOutcome(outcome.OutcomeKind.FAILED, _AT))
    assert outcome.read_readiness(gcs_bucket, _DOC_ID) is outcome.Readiness.READY
