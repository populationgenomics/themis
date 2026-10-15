"""Rewrite every litcache manifest onto records and refs, or check that the rewrite is done.

The one-off step of moving renderings into records (`docs/design/litcache-manifest.md`, "Renderings move into records by
rewriting every manifest once"). A manifest written before records existed lists its renderings in the deprecated
`renderings` map and its fetched supplementary files with no hash. The rewrite copies each rendering into a `Record`
(its revision becomes the record's one dep), points the `markdown` ref at the best of them, and fills each fetched
file's hash from the path its bytes are stored at, which already names them. It leaves the `renderings` map in place,
so a reader still running the previous code keeps working until the new readers deploy.

The rewrite is idempotent, and safe to re-run after writers on the new code have touched a manifest: a record already
present is kept as it is, and a `markdown` ref already set is never moved, since a writer that set it ranked the
renderings itself.

Run it with every manifest writer paused: the conversion queue paused and no ingestion running. A metadata refresh
writes only `metadata.pb` and can run throughout. The order is:

1. Pause the conversion queue and stop ingestion. Pausing a queue does not stop a conversion already dispatched, so
   wait out the convert worker's request timeout (or see the queue's in-flight count reach zero) before going on.
2. ``--dry-run``, and fix anything it reports: a manifest that breaks its constraints can be written by no writer.
3. Rewrite.
4. Deploy the readers and writers that use only records and refs.
5. Rewrite again, to catch any manifest an old writer touched despite the pause, then ``--verify``.
6. Resume the queue and ingestion.

Rolling the services back is cheap until step 6 and costly after it: a manifest written after the writers resume has
no entry in the deprecated map, so the previous readers see its paper as having no text and the previous producer
converts it again.

Each manifest is written back with a generation-matched read-modify-write, so a concurrent writer is re-read rather
than overwritten, and the bucket keeps each object's earlier generations, which are the way back. ``--dry-run`` computes
and validates every rewrite and writes nothing; it reports the manifests a live run would change and the ones it could
not rewrite. ``--verify`` writes nothing and reports every manifest not yet on records and refs. Any failure exits
non-zero.

Example::

    uv run --group litcache python -m tools.litcache.rewrite_manifests --project cpg-themis-dev --dry-run
    uv run --group litcache python -m tools.litcache.rewrite_manifests --project cpg-themis-dev
    uv run --group litcache python -m tools.litcache.rewrite_manifests --project cpg-themis-dev --verify
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import logging
import posixpath
import sys
from collections.abc import Iterator, Sequence

import google.auth
from google.api_core import exceptions as api_exceptions
from google.auth.transport import requests as google_requests
from google.cloud import storage as gcs
from requests import adapters

from themis.litcache import records, writer
from themis.litcache.models import litcache_pb2

_PAPERS_PREFIX = f'{writer.PAPERS_DIR}/'
_DEFAULT_WORKERS = 32
_WRITE_ATTEMPTS = 5


@dataclasses.dataclass(frozen=True)
class Failure:
    """A manifest the rewrite could not bring onto records.

    Attributes:
        doc_id: The paper whose manifest failed.
        reason: Why, for an operator to act on.
    """

    doc_id: str
    reason: str


@dataclasses.dataclass(frozen=True)
class Report:
    """What a run did across the corpus.

    Attributes:
        manifests: Every manifest listed.
        rewritten: The papers whose manifest this run changed (or, on a dry run, would have).
        failures: The papers whose manifest could not be rewritten.
    """

    manifests: int
    rewritten: list[str]
    failures: list[Failure]


def _record_from(rendering: litcache_pb2.Rendering) -> litcache_pb2.Record:
    construction = litcache_pb2.RenderingConstruction(
        converter=rendering.converter, converter_version=rendering.converter_version
    )
    if rendering.HasField('model'):
        construction.model = rendering.model
    return litcache_pb2.Record(deps=[rendering.from_revision], created_at=rendering.created_at, rendering=construction)


def rewrite(manifest: litcache_pb2.Manifest) -> litcache_pb2.Manifest | None:
    """The manifest moved onto records and refs, or None when it already is.

    Args:
        manifest: A stored manifest, in either shape.

    Returns:
        A new manifest with a record for every rendering, a `markdown` ref when it holds any rendering, and a hash on
        every fetched file; or None when `manifest` needs none of that.

    Raises:
        ValueError: If the result breaks the manifest's constraints, an unchanged manifest included: every writer
            validates the whole manifest before it writes, so a manifest that breaks them could never be written
            again. A rendering made from a revision no lineage holds, say, or a fetched file whose path names no hash.
    """
    result = litcache_pb2.Manifest()
    result.CopyFrom(manifest)
    for key, rendering in manifest.renderings.items():
        if key not in result.records:
            result.records[key].CopyFrom(_record_from(rendering))
    if records.MARKDOWN_REF not in result.refs:
        best = records.best_rendering(result.records.items())
        if best is not None:
            result.refs[records.MARKDOWN_REF] = best
    for entry in result.files:
        if entry.path and not entry.hash:
            stem = posixpath.splitext(posixpath.basename(entry.path))[0]
            if not stem:
                raise ValueError(f'manifest {manifest.doc_id}: file {entry.name!r} has a path naming no hash')
            entry.hash = stem
    writer.validate_manifest(result)
    if result == manifest:
        return None
    return result


def unfinished(manifest: litcache_pb2.Manifest) -> str | None:
    """Why `manifest` is not yet on records and refs, or None when it is.

    A manifest is finished when it meets its own constraints, every rendering in the deprecated map has its record, a
    manifest holding any rendering has a `markdown` ref, and every fetched file has its hash.
    """
    try:
        writer.validate_manifest(manifest)
    except writer.ManifestRefusedError as e:
        return str(e)
    missing = [key for key in manifest.renderings if key not in manifest.records]
    if missing:
        return f'{len(missing)} rendering(s) without a record'
    if any(records.is_rendering(r) for r in manifest.records.values()) and records.MARKDOWN_REF not in manifest.refs:
        return 'renderings but no markdown ref'
    if any(f.path and not f.hash for f in manifest.files):
        return 'a fetched file without its hash'
    return None


def _doc_ids(bucket: gcs.Bucket) -> Iterator[str]:
    for blob in bucket.list_blobs(prefix=_PAPERS_PREFIX, match_glob=f'{_PAPERS_PREFIX}*/{writer.MANIFEST_NAME}'):
        yield blob.name.split('/')[1]


def _rewrite_one(bucket: gcs.Bucket, doc_id: str, *, dry_run: bool) -> bool:
    """Rewrite one paper's manifest in place; return whether it changed (or would have)."""
    blob_name = writer.manifest_path(doc_id)
    for _ in range(_WRITE_ATTEMPTS):
        blob = bucket.blob(blob_name)  # a fresh handle each attempt, so a stale generation is never reused
        data = blob.download_as_bytes()
        manifest = litcache_pb2.Manifest.FromString(data)
        if manifest.doc_id != doc_id:
            raise ValueError(f'manifest in {doc_id} names doc_id {manifest.doc_id!r}')
        rewritten = rewrite(manifest)
        if rewritten is None:
            return False
        if dry_run:
            return True
        try:
            blob.upload_from_string(rewritten.SerializeToString(), if_generation_match=blob.generation)
        except api_exceptions.PreconditionFailed:
            continue  # a writer changed the manifest between the read and the write; re-read it
        return True
    raise writer.ConcurrentWriteError(f'{blob_name}: {_WRITE_ATTEMPTS} rewrite attempts lost the race')


def run(bucket: gcs.Bucket, *, dry_run: bool, workers: int = _DEFAULT_WORKERS) -> Report:
    """Rewrite every manifest under `papers/` onto records and refs.

    Args:
        bucket: The litcache bucket.
        dry_run: Compute and validate every rewrite, and write nothing.
        workers: How many manifests to rewrite at once.

    Returns:
        The `Report`. A run is complete only when it has no failures.
    """
    doc_ids = sorted(_doc_ids(bucket))
    rewritten: list[str] = []
    failures: list[Failure] = []

    def one(doc_id: str) -> tuple[str, bool | Failure]:
        try:
            return doc_id, _rewrite_one(bucket, doc_id, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001 -- one manifest's fault is reported, never allowed to end the run
            return doc_id, Failure(doc_id=doc_id, reason=f'{type(e).__name__}: {e}')

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for doc_id, outcome in pool.map(one, doc_ids):
            if isinstance(outcome, Failure):
                failures.append(outcome)
            elif outcome:
                rewritten.append(doc_id)
    return Report(manifests=len(doc_ids), rewritten=rewritten, failures=failures)


def verify(bucket: gcs.Bucket, *, workers: int = _DEFAULT_WORKERS) -> Report:
    """Check every manifest is on records and refs and meets its constraints; each that does not is a failure.

    Writes nothing.
    """
    doc_ids = sorted(_doc_ids(bucket))

    def one(doc_id: str) -> Failure | None:
        try:
            data = bucket.blob(writer.manifest_path(doc_id)).download_as_bytes()
            reason = unfinished(litcache_pb2.Manifest.FromString(data))
        except Exception as e:  # noqa: BLE001 -- one manifest's fault is reported, never allowed to end the check
            reason = f'{type(e).__name__}: {e}'
        return None if reason is None else Failure(doc_id=doc_id, reason=reason)

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        failures = [f for f in pool.map(one, doc_ids) if f is not None]
    return Report(manifests=len(doc_ids), rewritten=[], failures=failures)


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
    mode.add_argument('--dry-run', action='store_true', help='compute and validate every rewrite; write nothing')
    mode.add_argument('--verify', action='store_true', help='report every manifest not yet rewritten; write nothing')
    parser.add_argument(
        '--workers', type=int, default=_DEFAULT_WORKERS, help='manifests handled at once (default: %(default)s)'
    )
    args = parser.parse_args(argv)
    if args.workers <= 0:
        parser.error('--workers must be positive')
    if args.bucket is None:
        args.bucket = f'{args.project}-fulltext'
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Rewrite (or dry-run, or verify) every manifest. Returns an exit code."""
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s', force=True)
    bucket = _open_bucket(args.project, args.bucket, args.workers)
    root = f'gs://{bucket.name}/{_PAPERS_PREFIX}'
    if args.verify:
        report = verify(bucket, workers=args.workers)
        print(f'{report.manifests} manifest(s) under {root}; {len(report.failures)} not yet on records and refs')
    else:
        report = run(bucket, dry_run=args.dry_run, workers=args.workers)
        verb = 'would be rewritten' if args.dry_run else 'rewritten'
        print(f'{report.manifests} manifest(s) under {root}; {len(report.rewritten)} {verb}')
    if report.failures:
        for failure in report.failures:
            print(f'  {failure.doc_id}: {failure.reason}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
