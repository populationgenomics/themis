"""Regenerate `metadata.pb` for committed litcache papers whose record is absent.

The metadata-only refresh: delete the `metadata.pb` objects to redo, then run this. It
lists `papers/`, takes every paper with a manifest and no `metadata.pb`, resolves their
bibliographic records through the same ladder ingestion uses (`resolve.resolve_batch`:
batched efetch, then the DOI path), and writes each record (`writer.write_metadata`).
Conversions, sources and renderings are untouched, and a paper whose `metadata.pb` is
present is never read or written — so a re-run is a no-op and nothing here can replace a
record that exists.

No Beam, no Dataflow, no Cloud SQL: the whole run is this process against GCS under
ambient ADC (`gcloud`), plus live NCBI / Europe PMC / OpenAlex egress. The listing is one
GCS call; only the due manifests are downloaded.

``--dry-run`` prints the requests a run would make and writes nothing; it already reports
the papers a run could not attempt (an unreadable manifest, a ``doc_id`` disagreeing with
its directory, no pmid and no doi). A live run adds what the resolver settles against a
paper: a miss, a record failing the store's precondition, a record its mirror does not hold.
Any failure exits non-zero — the refresh is complete only when a run exits 0. Resolution is chunked
and each chunk written before the next, so a transport failure loses nothing already
written and a re-run resumes.

Example::

    uv run --group litcache python -m tools.litcache.refresh_metadata --dry-run
    uv run --group litcache python -m tools.litcache.refresh_metadata --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

import httpx2
import litfetch
from google.cloud import storage as gcs

from themis.common import constants
from themis.litcache import refresh, resolve

_DEFAULT_PROJECT = 'cpg-themis-dev'


def _open_bucket(name: str) -> gcs.Bucket:
    """Open a GCS bucket under ambient (ADC) credentials."""
    return gcs.Client().bucket(name)


async def _resolve_live(requests: Sequence[resolve.ResolveRequest]) -> dict[str, resolve.Outcome]:
    """`resolve.resolve_batch` over a live HTTP client and litfetch session, opened per call (the ingestion wiring)."""
    async with (
        httpx2.AsyncClient() as client,
        litfetch.Session(contact=constants.CONTACT_EMAIL) as session,
    ):
        return await resolve.resolve_batch(requests, http_client=client, session=session)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '--project', default=_DEFAULT_PROJECT, help='GCP project the store lives in (default: %(default)s)'
    )
    parser.add_argument(
        '--bucket', default=None, help='the litcache bucket holding papers/ (default: <project>-fulltext)'
    )
    parser.add_argument(
        '--limit', type=int, default=None, help='refresh at most this many papers, in doc_id order (default: all due)'
    )
    parser.add_argument('--dry-run', action='store_true', help='list the papers that would be refreshed; write nothing')
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error('--limit must be positive')
    if args.bucket is None:
        args.bucket = f'{args.project}-fulltext'  # follows --project, as the launcher's defaults do
    return args


def _dry_run(bucket: gcs.Bucket, *, limit: int | None) -> int:
    found = refresh.plan(bucket, limit=limit)
    root = f'gs://{bucket.name}/{refresh.PAPERS_PREFIX}'
    print(f'{found.manifests} manifest(s) under {root}; {len(found.due)} due for refresh')
    for request in found.due:
        print(f'  {request.claim_key}: pmid={request.pmid!r} doi={request.doi!r}')
    if found.failures:
        listing = refresh.render_failures(found.failures)
        print(f'\n{len(found.failures)} due paper(s) a refresh cannot attempt:\n{listing}', file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Plan (and unless `--dry-run`, refresh) the papers with no `metadata.pb`. Returns an exit code."""
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s', force=True)
    bucket = _open_bucket(args.bucket)
    if args.dry_run:
        return _dry_run(bucket, limit=args.limit)

    report = asyncio.run(refresh.refresh(bucket, _resolve_live, limit=args.limit))
    print(
        f'{report.manifests} manifest(s) under gs://{bucket.name}/papers/; {len(report.refreshed)} metadata.pb written'
    )
    if report.failures:
        listing = refresh.render_failures(report.failures)
        print(f'\n{len(report.failures)} paper(s) not refreshed:\n{listing}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
