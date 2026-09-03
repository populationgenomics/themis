"""Post-run diagnostics report and the (manual) seed teardown.

After a seed-ingestion run, a diagnostics report is emitted: the `no_text_layer`
papers (the pdf-derived papers whose pdf carries no recoverable character layer —
the quote→bbox problem the design surfaces early, literature-cache.md §Source
anchors), the seed objects left unpaired, and the per-stage totals. The report is
the input to the manual check before the transient `ingest/` prefix is deleted with
`teardown_seed`.

Teardown is deliberately not automatic: the seed bytes are the only re-completion
source for a paper whose manifest never got written, so deleting `ingest/` is an
explicit operator step taken only once the report confirms every paper ingested —
never a side effect of the run finishing.

This module operates on a `google.cloud.storage.Bucket` plus plain counts, so it is
testable without Beam. The Beam coupling — turning a `PipelineResult` into the
counter snapshot — lives in `themis.litcache.ingest_beam`.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterator, Mapping

from google.cloud import storage as gcs

from themis.litcache.models import litcache_pb2

_LOG = logging.getLogger(__name__)

_MANIFEST_SUFFIX = '/manifest.pb'


@dataclasses.dataclass(frozen=True)
class FlaggedPaper:
    """A paper whose pdf source has no recoverable character layer.

    Attributes:
        doc_id: The paper's `doc_id` (its directory name).
        claim_key: The precedence-primary external id, for a human reading the
            report.
        source_handles: The pdf `Source.handle`s with a revision flagged
            `no_text_layer` (a paper may hold more than one pdf lineage).
    """

    doc_id: str
    claim_key: str
    source_handles: list[str]


@dataclasses.dataclass(frozen=True)
class IngestReport:
    """The diagnostics summary of one seed-ingestion run.

    Attributes:
        papers_total: Number of manifests under the papers prefix.
        flagged: Papers flagged `no_text_layer` (in `doc_id` order).
        unpaired_seeds: Seed keys excluded for missing a counterpart (a `.json`
            with no `.pdf`, or the reverse, or an unknown extension).
        counters: The per-stage Beam-metric snapshot (`papers_seen`,
            `doc_id_minted`, …), as the runner reported it.
        dead_lettered: Papers the run could not ingest, counted from the records
            on the bucket rather than the metrics — a counter the runner never
            reported reads as 0, which would make a failed run look clean.
    """

    papers_total: int
    flagged: list[FlaggedPaper]
    unpaired_seeds: list[str]
    counters: Mapping[str, int]
    dead_lettered: int


def flagged_no_text_layer(bucket: gcs.Bucket, *, papers_prefix: str = 'papers/') -> list[FlaggedPaper]:
    """Scan the manifests for pdf sources flagged `no_text_layer`.

    A source is flagged only when `has_text_layer` is present and `False` — an
    unset value means the probe was skipped (XML is the source of truth), not a
    problem paper.

    Args:
        bucket: The cache bucket.
        papers_prefix: The key prefix the paper directories live under.

    Returns:
        The flagged papers, in `doc_id` order.
    """
    return [f for manifest in _scan_manifests(bucket, papers_prefix) if (f := _flagged(manifest)) is not None]


def _flagged(manifest: litcache_pb2.Manifest) -> FlaggedPaper | None:
    """The `FlaggedPaper` for a manifest with a `no_text_layer` pdf source, else None."""
    source_handles = [
        source.handle
        for source in manifest.sources
        if any(rev.HasField('has_text_layer') and not rev.has_text_layer for rev in source.revisions)
    ]
    if not source_handles:
        return None
    return FlaggedPaper(doc_id=manifest.doc_id, claim_key=manifest.claim_key, source_handles=source_handles)


def build_report(
    bucket: gcs.Bucket,
    *,
    unpaired_seeds: list[str],
    counters: Mapping[str, int],
    dead_lettered: int,
    papers_prefix: str = 'papers/',
) -> IngestReport:
    """Assemble the diagnostics report from the manifests, pairing, and metrics.

    Args:
        bucket: The cache bucket (scanned for manifests).
        unpaired_seeds: The seed keys the driver left unpaired.
        counters: The per-stage Beam-metric snapshot.
        dead_lettered: The number of dead-letter records the run left on the bucket.
        papers_prefix: The key prefix the paper directories live under.

    Returns:
        The `IngestReport`.
    """
    manifests = list(_scan_manifests(bucket, papers_prefix))
    flagged = [f for m in manifests if (f := _flagged(m)) is not None]
    return IngestReport(
        papers_total=len(manifests),
        flagged=flagged,
        unpaired_seeds=unpaired_seeds,
        counters=counters,
        dead_lettered=dead_lettered,
    )


def render_report(report: IngestReport) -> str:
    """Render the report as a human-readable multi-line summary."""
    lines = [
        'litcache ingestion report',
        f'  papers written: {report.papers_total}',
        f'  counters: {dict(report.counters)}',
        f'  dead-lettered: {report.dead_lettered}',
        f'  unresolved (dead-lettered): {report.counters.get("paper_unresolved", 0)}',
        f'  precondition failed (dead-lettered): {report.counters.get("paper_precondition_failed", 0)}',
        f'  schema drift (dead-lettered): {report.counters.get("paper_schema_drift", 0)}',
        f'  failed (dead-lettered): {report.counters.get("paper_failed", 0)}',
        f'  unpaired seed objects: {len(report.unpaired_seeds)}',
        f'  no_text_layer: {len(report.flagged)}',
    ]
    lines += [f'    {f.doc_id} ({f.claim_key}): {", ".join(f.source_handles)}' for f in report.flagged]
    lines += [f'    unpaired: {key}' for key in report.unpaired_seeds]
    return '\n'.join(lines)


def write_dead_letter_summary(bucket: gcs.Bucket, *, records_prefix: str, summary_path: str) -> int:
    """Consolidate the run's per-paper dead-letter records into one text file on GCS.

    The run writes one JSON record per dead-lettered paper under `records_prefix`;
    this concatenates them into a single JSON-lines blob at `summary_path` for later
    analysis. Each line is an object with `key` (the paper's `claim_key`, or its seed
    object key when extraction failed before identity), `pmid`, `doi`, and the failure
    `reason` — the shape `ingest_beam._write_dead_letter` emits. Each record is already a
    single line (`json.dumps` escapes newlines), so they are joined verbatim without
    re-parsing.

    Args:
        bucket: The cache bucket holding the run's dead-letter records.
        records_prefix: The prefix the per-paper records live under.
        summary_path: The key to write the consolidated JSON-lines summary to.

    Returns:
        The number of dead-letter records consolidated (0 writes no file).
    """
    records = sorted(bucket.list_blobs(prefix=records_prefix), key=lambda blob: blob.name)
    if not records:
        return 0
    body = '\n'.join(blob.download_as_bytes().decode('utf-8') for blob in records) + '\n'
    bucket.blob(summary_path).upload_from_string(body, content_type='application/x-ndjson')
    return len(records)


def teardown_seed(bucket: gcs.Bucket, *, seed_prefix: str = 'ingest/') -> int:
    """Delete every object under `seed_prefix` — a manual operator step.

    The seed prefix is transient: source bytes are copied into each paper
    directory, so once every paper's manifest is committed the seed is redundant.
    This is not called automatically by the run: the seed is the only re-completion
    source for an unwritten paper, so the operator deletes it only after the
    diagnostics report (`build_report`) confirms a clean ingestion.

    Args:
        bucket: The cache bucket.
        seed_prefix: The flat seed prefix to delete.

    Returns:
        The number of objects deleted.
    """
    deleted = 0
    for blob in list(bucket.list_blobs(prefix=seed_prefix)):
        blob.delete()
        deleted += 1
    _LOG.info('deleted %d seed object(s) under %r', deleted, seed_prefix)
    return deleted


def _scan_manifests(bucket: gcs.Bucket, papers_prefix: str) -> Iterator[litcache_pb2.Manifest]:
    """Yield each manifest under `papers_prefix` (exact `{doc_id}/manifest.pb` depth)."""
    for blob in bucket.list_blobs(prefix=papers_prefix):
        rel = blob.name[len(papers_prefix) :]
        if rel.endswith(_MANIFEST_SUFFIX) and rel.count('/') == 1:
            yield litcache_pb2.Manifest.FromString(blob.download_as_bytes())
