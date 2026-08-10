"""Run a bounded litcache seed-ingestion pass on the local DirectRunner.

A dev driver for exercising the real pipeline (`themis.litcache.ingest_beam`)
end-to-end without any Dataflow or Pulumi infrastructure. It:

1. stages the first ``--limit`` paired seed objects from the real seed bucket into
   a **scratch** bucket's ``ingest/`` prefix (so output ``papers/`` lands in the
   scratch bucket, never the real cache);
2. **replaces** the crosswalk table's contents with the ones inverted from the scratch
   bucket's existing manifests (so a re-run adopts already-committed papers and skips
   them rather than minting duplicates), against either a given Postgres (``--pg-host``,
   which requires ``--rebuild-crosswalk`` because the replace is destructive) or a
   throwaway testcontainers instance the litcache migration is applied to;
3. runs the ingestion on the DirectRunner and prints the diagnostics report.

Auth is ambient GCS credentials (your ``gcloud`` ADC) — no IAM grants, no Cloud SQL.
Metadata resolution and the OA ladder use their live defaults; resolution is batched,
so the request rate is bounded by call count, not worker count. A paper that cannot be
resolved or written is dead-lettered under ``diagnostics/dead_letters/`` and the run
carries on; more than ``--max-dead-letters`` of them exits non-zero. Teardown is manual:
delete the scratch bucket, or call ``themis.litcache.report.teardown_seed``.

Example (throwaway Postgres needs the ``test`` group for testcontainers)::

    uv run --group dataflow --group test python -m tools.litcache.run_local \
        --scratch-bucket cpg-themis-dev-litcache-scratch --limit 6

    # against an existing Postgres (schema already migrated; no testcontainers):
    uv run --group dataflow python -m tools.litcache.run_local \
        --scratch-bucket cpg-themis-dev-litcache-scratch --limit 6 \
        --pg-host localhost --pg-user litcache --pg-database litcache --rebuild-crosswalk
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import functools
import logging
import pathlib
import sys
from collections.abc import Callable, Iterator, Sequence

import pg8000.dbapi
from apache_beam.options import pipeline_options
from apache_beam.runners import runner
from google.cloud import storage as gcs

from themis.litcache import crosswalk, ingest_beam, pipeline, rebuild, report
from themis.litcache.models import litcache_pb2
from themis.migrate import migrate

_LOG = logging.getLogger('litcache.run_local')

# The real seed dump (literature-cache.md §Seed source). Reading it needs only read
# access under your own credentials — no Pulumi-provisioned grant.
_DEFAULT_SOURCE_BUCKET = 'cpg-themis-dev-fulltext'

# A throwaway container's credentials are not secrets; named once so the container
# and its connections agree (and so no password literal trips the secrets linter).
_THROWAWAY_DB = 'litcache'

_MIGRATIONS = pathlib.Path(migrate.__file__).resolve().parent / 'migrations'


def _open_bucket(name: str) -> gcs.Bucket:
    """Open a GCS bucket under ambient (ADC) credentials."""
    return gcs.Client().bucket(name)


def _licence_fallback() -> pipeline.LicenceFacts:
    """The honest non-OA fallback for a local run: unknown licence, unknown access.

    Used only on the non-OA branch (the OA branch reads licence/access from the
    fetched artifact). litfetch's access authorities replace this once wired; until
    then a local run cannot know a non-OA paper's licence, so it asserts the
    `unknown` access rather than inventing one.
    """
    return pipeline.LicenceFacts(
        licence='',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED,
        access=litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess()),
    )


def _stage_seed(source: gcs.Bucket, scratch: gcs.Bucket, *, seed_prefix: str, limit: int) -> int:
    """Copy the first `limit` paired seed objects from `source` into `scratch`.

    Pairs the source seed prefix, takes the first `limit` pairs in stem order, and
    writes each pair's json + pdf under the same key in `scratch`. Idempotent: a
    pair already present in `scratch` is skipped, so re-runs don't re-download.

    Args:
        source: The real seed bucket (read).
        scratch: The scratch bucket (write).
        seed_prefix: The flat seed prefix in both buckets.
        limit: The number of pairs to stage.

    Returns:
        The number of pairs staged (the first `limit`, or fewer if the source has
        fewer paired objects).
    """
    pairing = ingest_beam.pair_seed_keys(
        (blob.name for blob in source.list_blobs(prefix=seed_prefix)), prefix=seed_prefix
    )
    refs = pairing.refs[:limit]
    if len(pairing.refs) < limit:
        _LOG.warning('source has only %d paired seed object(s) (< limit %d)', len(pairing.refs), limit)
    for ref in refs:
        for key in (ref.json_key, ref.pdf_key):
            if scratch.blob(key).exists():
                continue
            scratch.blob(key).upload_from_string(source.blob(key).download_as_bytes())
    return len(refs)


def _apply_litcache_migration(conn: crosswalk.Connection) -> None:
    """Create the crosswalk schema by applying the litcache migration (deploy runs the same)."""
    sql = next(m.sql for m in migrate.discover(_MIGRATIONS) if m.name == 'litcache_crosswalk')
    with contextlib.closing(conn.cursor()) as cur:
        for statement in migrate.split_statements(migrate.render(sql, {})):
            cur.execute(statement)
    conn.commit()


@contextlib.contextmanager
def _crosswalk_conn_factory(args: argparse.Namespace) -> Iterator[Callable[[], crosswalk.Connection]]:
    """Yield a connection factory: an existing Postgres, or a throwaway container.

    With `--pg-host` given, connections open against that (already-migrated) Postgres.
    Otherwise a `postgres:16` container is started for the run, the litcache migration
    applied once, and torn down on exit. testcontainers is a dev/test dependency,
    imported lazily so this tool imports without it whenever an external host is set.
    """
    if args.pg_host is not None:
        yield functools.partial(
            pg8000.dbapi.connect,
            host=args.pg_host,
            port=args.pg_port,
            user=args.pg_user,
            password=args.pg_password,
            database=args.pg_database,
        )
        return

    import testcontainers.postgres  # noqa: PLC0415 -- dev-only heavy dep; only the throwaway path imports it

    with testcontainers.postgres.PostgresContainer(
        'postgres:16', username=_THROWAWAY_DB, password=_THROWAWAY_DB, dbname=_THROWAWAY_DB
    ) as container:
        factory = functools.partial(
            pg8000.dbapi.connect,
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            user=_THROWAWAY_DB,
            password=_THROWAWAY_DB,
            database=_THROWAWAY_DB,
        )
        with contextlib.closing(factory()) as conn:
            _apply_litcache_migration(conn)
        yield factory


def _rebuild_crosswalk(conn_factory: Callable[[], crosswalk.Connection], bucket: gcs.Bucket) -> int:
    """Replace the crosswalk table's contents with the bucket's manifests.

    Destructive: `rebuild.rebuild` deletes every existing crosswalk row before inserting
    the ones inverted from `bucket`'s manifests. Against a throwaway that is free; against
    an operator-supplied Postgres it discards whatever was there, which is why the
    `--pg-host` path requires `--rebuild-crosswalk`.

    Rebuilding from `papers/` (rather than leaving the table empty) makes a re-run
    idempotent: papers already committed in `bucket` are re-indexed, so the run
    adopts their `doc_id`s and skips them instead of minting duplicates. On a clean
    bucket this writes zero rows. Fails loud on a manifest inconsistency.

    Returns:
        The number of existing manifests re-indexed.
    """
    with contextlib.closing(conn_factory()) as conn:
        result = rebuild.rebuild(conn, bucket)
    return result.papers


def _options(num_workers: int) -> pipeline_options.PipelineOptions:
    """Local DirectRunner options: multi-threaded for per-paper I/O parallelism.

    `multi_threading` overlaps the per-paper OA fetches / GCS writes across `num_workers`
    threads — safe because the workers share one crosswalk connection and serialize mints
    on its mutex, a pg8000 connection not being thread-safe (`ingest_beam._MintConnection`).
    The DirectRunner runs the SDK over a gRPC control channel with a deadline that caps a
    long local run regardless of mode (in-memory routes through the same FnApi path), so
    this driver is a bounded spot-check; the full-seed run is Dataflow, which has no such
    deadline.
    """
    return pipeline_options.PipelineOptions(
        ['--runner=DirectRunner', '--direct_running_mode=multi_threading', f'--direct_num_workers={num_workers}']
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '--scratch-bucket',
        required=True,
        help='GCS bucket for the staged seed + output papers/ (isolates the run from the real cache)',
    )
    parser.add_argument(
        '--source-bucket',
        default=_DEFAULT_SOURCE_BUCKET,
        help='bucket holding the real ingest/ seed (default: %(default)s)',
    )
    parser.add_argument('--seed-prefix', default='ingest/', help='seed prefix in both buckets (default: %(default)s)')
    parser.add_argument('--limit', type=int, default=100, help='papers to stage + ingest (default: %(default)s)')
    parser.add_argument(
        '--pg-host', default=None, help='host of an existing (migrated) Postgres; omit to start a throwaway container'
    )
    parser.add_argument(
        '--rebuild-crosswalk',
        action='store_true',
        help="replace the --pg-host database's litcache.crosswalk with the scratch bucket's manifests "
        '(destructive; required with --pg-host, and what makes a re-run adopt already-committed papers)',
    )
    parser.add_argument('--pg-port', type=int, default=5432, help='existing Postgres port (default: %(default)s)')
    parser.add_argument('--pg-user', default=_THROWAWAY_DB, help='existing Postgres user (default: %(default)s)')
    parser.add_argument(
        '--pg-password', default=_THROWAWAY_DB, help='existing Postgres password (default: %(default)s)'
    )
    parser.add_argument('--pg-database', default=_THROWAWAY_DB, help='existing Postgres db (default: %(default)s)')
    parser.add_argument(
        '--direct-num-workers',
        type=int,
        default=8,
        help='DirectRunner worker threads for per-paper OA-fetch/write parallelism (default: %(default)s)',
    )
    parser.add_argument(
        '--max-dead-letters',
        type=int,
        default=0,
        help='papers the run may dead-letter before it is reported a failure (default: %(default)s)',
    )
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error('--limit must be positive')
    if args.direct_num_workers <= 0:
        parser.error('--direct-num-workers must be positive')
    if args.max_dead_letters < 0:
        parser.error('--max-dead-letters must not be negative')
    if args.pg_host is not None and not args.rebuild_crosswalk:
        # The rebuild replaces the table wholesale and this tool cannot tell a throwaway
        # from the real instance behind a proxy on localhost, so the operator states intent.
        parser.error('--pg-host requires --rebuild-crosswalk (the rebuild replaces litcache.crosswalk)')
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Stage, run, and report a bounded local ingestion pass. Returns an exit code."""
    args = _parse_args(argv)
    # force=True: Beam configures the root logger on import, so a plain basicConfig is a
    # no-op and the driver's INFO progress never shows.
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s', force=True)

    now = datetime.datetime.now(tz=datetime.UTC)
    source = _open_bucket(args.source_bucket)
    scratch = _open_bucket(args.scratch_bucket)

    staged = _stage_seed(source, scratch, seed_prefix=args.seed_prefix, limit=args.limit)
    _LOG.info('staged %d seed pair(s) into gs://%s/%s', staged, args.scratch_bucket, args.seed_prefix)

    bucket_factory = functools.partial(_open_bucket, args.scratch_bucket)
    with _crosswalk_conn_factory(args) as conn_factory:
        reindexed = _rebuild_crosswalk(conn_factory, scratch)
        _LOG.info(
            'crosswalk rebuilt from %d existing manifest(s) under gs://%s/papers/', reindexed, args.scratch_bucket
        )
        run = ingest_beam.run_ingestion(
            bucket_factory=bucket_factory,
            conn_factory=conn_factory,
            licence=_licence_fallback(),
            now=now,
            options=_options(args.direct_num_workers),
            seed_prefix=args.seed_prefix,
            limit=args.limit,
        )
        state = run.result.wait_until_finish()
        if state != runner.PipelineState.DONE:
            raise RuntimeError(f'ingestion did not complete cleanly (pipeline state: {state})')
        rep = ingest_beam.report_run(run, bucket_factory, seed_prefix=args.seed_prefix)

    print(report.render_report(rep))
    print(f'\nOutput written to gs://{args.scratch_bucket}/papers/ — seed left intact under {args.seed_prefix}.')
    print('Teardown is manual: delete the scratch bucket, or call themis.litcache.report.teardown_seed.')
    if rep.dead_lettered > args.max_dead_letters:
        # Per-paper isolation keeps the pipeline DONE however many papers failed, so the
        # dead-letter count is the only thing separating a clean pass from a total one.
        print(
            f'\n{rep.dead_lettered} paper(s) dead-lettered, over the --max-dead-letters '
            f'tolerance of {args.max_dead_letters}: see '
            f'gs://{args.scratch_bucket}/{run.dead_letter_summary}',
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
