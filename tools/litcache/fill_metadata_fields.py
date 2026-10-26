"""Refill the metadata fields of every stored `metadata.pb` under the current rule, or check that they match it.

Every record is written with its metadata fields (title, authors, year, container title, abstract) filled. When the
rule that fills them changes, this rewrite brings the stored records up to it: it refills each record's fields from the
index record it holds (`themis.litcache.metadata_fields.fill`), reading the JSON text or PubMed's typed record already
stored, so it fetches nothing from any index, and rewrites only the records whose fields differ.

Run it after the writers on the new rule have deployed, so a record written during the run is filled by that rule:

1. ``--dry-run``, and act on anything it reports.
2. Refill.
3. ``--verify``: every record's fields are as the current rule fills them.

A record is read checking only its source record, so metadata that breaks a rule tightened since it was written is
refilled rather than refused. Each record is written back with a generation-matched read-modify-write, so a concurrent
writer is re-read rather than overwritten, and the bucket keeps each object's earlier generations, which are the way
back. ``--dry-run`` computes and validates every fill and writes nothing. ``--verify`` writes nothing. Any failure exits
non-zero.

Example::

    uv run --group litcache python -m tools.litcache.fill_metadata_fields --project cpg-themis-dev --dry-run
    uv run --group litcache python -m tools.litcache.fill_metadata_fields --project cpg-themis-dev
    uv run --group litcache python -m tools.litcache.fill_metadata_fields --project cpg-themis-dev --verify
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import logging
import sys
from collections.abc import Iterator, Sequence

import google.auth
from google.api_core import exceptions as api_exceptions
from google.auth.transport import requests as google_requests
from google.cloud import storage as gcs
from requests import adapters

from themis.litcache import metadata_fields, sourced_metadata, writer
from themis.litcache.models import litcache_pb2

_PAPERS_PREFIX = f'{writer.PAPERS_DIR}/'
_DEFAULT_WORKERS = 32
_WRITE_ATTEMPTS = 5


@dataclasses.dataclass(frozen=True)
class Failure:
    """A record the fill could not bring up to the current rule.

    Attributes:
        doc_id: The paper whose `metadata.pb` failed.
        reason: Why, for an operator to act on; drift names the field.
    """

    doc_id: str
    reason: str


@dataclasses.dataclass(frozen=True)
class Report:
    """What a run did across the corpus.

    Attributes:
        records: Every `metadata.pb` listed.
        filled: The papers whose record this run changed (or, on a dry run, would have).
        failures: The papers whose record could not be filled.
    """

    records: int
    filled: list[str]
    failures: list[Failure]


def filled(sourced: litcache_pb2.SourcedMetadata) -> litcache_pb2.SourcedMetadata | None:
    """The record with its metadata as the current rule fills it from the source record, or None when it already is.

    The record is a modified copy of `sourced`, so a field this code does not know survives the fill.

    Raises:
        upstream_record.SchemaDriftError: If a field cannot be read from the source record, or the metadata filled
            breaks its rules (`metadata_fields.fill`).
        ValueError: If the source record has no index record to fill from.
    """
    metadata = metadata_fields.fill(sourced.source)
    if sourced.HasField('metadata') and metadata == sourced.metadata:
        return None
    result = litcache_pb2.SourcedMetadata()
    result.CopyFrom(sourced)
    result.metadata.CopyFrom(metadata)
    return result


def _doc_ids(bucket: gcs.Bucket) -> Iterator[str]:
    for blob in bucket.list_blobs(prefix=_PAPERS_PREFIX, match_glob=f'{_PAPERS_PREFIX}*/{writer.METADATA_NAME}'):
        yield blob.name.split('/')[1]


def _fill_one(bucket: gcs.Bucket, doc_id: str, *, dry_run: bool) -> bool:
    """Fill one paper's record in place; return whether it changed (or would have)."""
    blob_name = writer.metadata_path(doc_id)
    for _ in range(_WRITE_ATTEMPTS):
        blob = bucket.blob(blob_name)  # a fresh handle each attempt, so a stale generation is never reused
        sourced = sourced_metadata.parse_to_refill(blob.download_as_bytes())
        if blob.generation is None:
            raise ValueError(f'{blob_name}: the read returned no generation, so no write could be conditional on it')
        result = filled(sourced)
        if result is None:
            return False
        if dry_run:
            return True
        try:
            blob.upload_from_string(sourced_metadata.to_canonical_bytes(result), if_generation_match=blob.generation)
        except api_exceptions.PreconditionFailed:
            continue  # a writer replaced the record between the read and the write; re-read it
        return True
    raise writer.ConcurrentWriteError(f'{blob_name}: {_WRITE_ATTEMPTS} fill attempts lost the race')


def run(bucket: gcs.Bucket, *, dry_run: bool, workers: int = _DEFAULT_WORKERS) -> Report:
    """Fill the metadata fields of every `metadata.pb` under `papers/`.

    Args:
        bucket: The litcache bucket.
        dry_run: Compute and validate every fill, and write nothing.
        workers: How many records to fill at once.

    Returns:
        The `Report`. A run is complete only when it has no failures.
    """
    doc_ids = sorted(_doc_ids(bucket))
    changed: list[str] = []
    failures: list[Failure] = []

    def one(doc_id: str) -> tuple[str, bool | Failure]:
        try:
            return doc_id, _fill_one(bucket, doc_id, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001 -- one record's fault is reported, never allowed to end the run
            return doc_id, Failure(doc_id=doc_id, reason=f'{type(e).__name__}: {e}')

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for doc_id, outcome in pool.map(one, doc_ids):
            if isinstance(outcome, Failure):
                failures.append(outcome)
            elif outcome:
                changed.append(doc_id)
    return Report(records=len(doc_ids), filled=changed, failures=failures)


def verify(bucket: gcs.Bucket, *, workers: int = _DEFAULT_WORKERS) -> Report:
    """Check every record's fields are as the current rule fills them. Writes nothing."""
    doc_ids = sorted(_doc_ids(bucket))

    def one(doc_id: str) -> Failure | None:
        try:
            sourced = sourced_metadata.parse(bucket.blob(writer.metadata_path(doc_id)).download_as_bytes())
            if filled(sourced) is None:
                return None
            reason = 'metadata fields differ from what the current rule fills'
        except Exception as e:  # noqa: BLE001 -- one record's fault is reported, never allowed to end the check
            reason = f'{type(e).__name__}: {e}'
        return Failure(doc_id=doc_id, reason=reason)

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        failures = [f for f in pool.map(one, doc_ids) if f is not None]
    return Report(records=len(doc_ids), filled=[], failures=failures)


def _open_bucket(project: str, name: str, workers: int) -> gcs.Bucket:
    """Open the bucket under ambient (ADC) credentials, with an HTTP connection per worker."""
    credentials, _ = google.auth.default()
    session = google_requests.AuthorizedSession(credentials)
    session.mount('https://', adapters.HTTPAdapter(pool_connections=workers, pool_maxsize=workers))
    return gcs.Client(project=project, credentials=credentials, _http=session).bucket(name)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', required=True, help='GCP project the store lives in')
    parser.add_argument(
        '--bucket', default=None, help='the litcache bucket holding papers/ (default: <project>-fulltext)'
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='compute and validate every fill; write nothing')
    mode.add_argument('--verify', action='store_true', help='report every record not filled by the current rule')
    parser.add_argument(
        '--workers', type=int, default=_DEFAULT_WORKERS, help='records handled at once (default: %(default)s)'
    )
    args = parser.parse_args(argv)
    if args.workers <= 0:
        parser.error('--workers must be positive')
    if args.bucket is None:
        args.bucket = f'{args.project}-fulltext'
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Fill (or dry-run, or verify) every record's metadata fields. Returns an exit code."""
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s', force=True)
    bucket = _open_bucket(args.project, args.bucket, args.workers)
    root = f'gs://{bucket.name}/{_PAPERS_PREFIX}'
    if args.verify:
        report = verify(bucket, workers=args.workers)
        print(f'{report.records} record(s) under {root}; {len(report.failures)} not filled by the current rule')
    else:
        report = run(bucket, dry_run=args.dry_run, workers=args.workers)
        verb = 'would be filled' if args.dry_run else 'filled'
        print(f'{report.records} record(s) under {root}; {len(report.filled)} {verb}')
    if report.failures:
        for failure in report.failures:
            print(f'  {failure.doc_id}: {failure.reason}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
