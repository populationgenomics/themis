"""Launch a litcache seed-ingestion pass on Dataflow, minting into Cloud SQL.

The Dataflow counterpart of `tools.litcache.run_local`: instead of the in-process
DirectRunner + a throwaway Postgres, it submits `ingest_beam.run_ingestion` to Dataflow
with workers running as the `themis-ingest` SA, and mints doc_ids into the shared
`themis-sql` Cloud SQL instance via `cloudsql.CrosswalkConnFactory`.

Two run shapes:

- Staged sample (default): copies the first ``--limit`` paired seed objects into the
  scratch bucket and runs against it, so the output ``papers/`` never touches the real
  cache. ``--limit`` is required. Isolated in storage only — the crosswalk is the shared
  one on both paths, so a staged run mints `doc_id`s whose manifests live in the scratch
  bucket; deleting that bucket leaves those rows pointing at nothing, and a later real
  ingestion of the same paper adopts a `doc_id` with no artifacts behind it.
- Direct (``--direct``): reads ``ingest/`` from and writes ``papers/`` to the real store
  (``--source-bucket``) with no staging — the production shape. ``--limit`` is optional
  (omit to ingest the whole corpus).

Dataflow ``staging``/``temp`` live in the scratch bucket either way. The crosswalk is
always the real shared one, so every shape exercises the production mint path.

``--project``/``--region`` pick the deployment. Every project-scoped resource the run
touches — the Cloud SQL instance, the worker SA and its DB user, the subnet, and both
bucket defaults — is derived from them, so naming one project cannot leave part of a run
pointed at another.

Auth is ambient ADC (``gcloud``) for staging + job submission; the submitting identity
needs ``serviceAccountUser`` on ``themis-ingest`` and write on the scratch bucket.
``themis-ingest`` needs read on the worker image's Artifact Registry repo, and object
read/write on the scratch bucket on every run shape — Dataflow ``staging``/``temp`` live
there and the workers, not the submitter, read and write them; the staged shape also reads
``ingest/`` and writes ``papers/`` there. On the ``--direct`` shape it needs the same on
``--source-bucket``. (All granted out of band for a bounded run; baked into
``IngestionRuntime`` for production.)

Example::

    uv run --group dataflow python -m tools.litcache.launch_dataflow \
        --sdk-image australia-southeast1-docker.pkg.dev/cpg-themis-dev/themis/litcache-worker:<tag> \
        --limit 50
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime
import functools
import logging
import sys
from collections.abc import Sequence

from apache_beam.options import pipeline_options
from apache_beam.runners import runner
from google.cloud import storage as gcs

from themis.litcache import cloudsql, ingest_beam, pipeline, report
from themis.litcache.models import litcache_pb2

_LOG = logging.getLogger('litcache.launch_dataflow')

_DEFAULT_PROJECT = 'cpg-themis-dev'
_DEFAULT_REGION = 'australia-southeast1'
_SQL_DATABASE = 'themis'
# Parallel GCS rewrite calls when staging the seed; each is a metadata-only op, so oversubscribing
# the launcher's cores is fine.
_STAGE_CONCURRENCY = 16


@dataclasses.dataclass(frozen=True)
class _Target:
    """Where a run lands: the project and region, plus every resource name derived from them.

    The derivations mirror the infra that creates each resource, so a run against another
    project reaches that project's instance, SA and subnet rather than silently crossing
    back to the defaults.
    """

    project: str
    region: str

    @property
    def sql_connection_name(self) -> str:
        """The shared Cloud SQL instance holding the litcache crosswalk (infra/themis_infra/sql.py)."""
        return f'{self.project}:{self.region}:themis-sql'

    @property
    def ingest_sa(self) -> str:
        """The ingestion worker SA (infra/themis_infra/ingest.py)."""
        return f'themis-ingest@{self.project}.iam.gserviceaccount.com'

    @property
    def ingest_db_user(self) -> str:
        """The SA's Cloud SQL IAM DB-user login."""
        return f'themis-ingest@{self.project}.iam'  # the SA email minus `.gserviceaccount.com`

    @property
    def subnetwork(self) -> str:
        """The dedicated ingestion subnet the workers run on.

        Workers take external IPs (NCBI/OpenAlex egress goes out directly; GCS stays on the
        private path via the subnet's Private Google Access).
        """
        return (
            f'https://www.googleapis.com/compute/v1/projects/{self.project}'
            f'/regions/{self.region}/subnetworks/themis-ingest'
        )

    # Named `default_*` because `--scratch-bucket`/`--source-bucket` override them: the
    # resolved values live on the parsed args, and reading these instead would silently
    # ignore the flag. The other properties have no such override.
    @property
    def default_scratch_bucket(self) -> str:
        """Staging/output bucket a bounded run uses unless `--scratch-bucket` says otherwise."""
        return f'{self.project}-litcache-scratch'

    @property
    def default_source_bucket(self) -> str:
        """Bucket holding the real `ingest/` seed unless `--source-bucket` says otherwise."""
        return f'{self.project}-fulltext'


def _open_bucket(name: str) -> gcs.Bucket:
    """Open a GCS bucket under ambient (ADC) credentials."""
    return gcs.Client().bucket(name)


def _licence_fallback() -> pipeline.LicenceFacts:
    """The honest non-OA fallback: unknown licence / access (the OA branch reads its own)."""
    return pipeline.LicenceFacts(
        licence='',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED,
        access=litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess()),
    )


def _stage_seed(source: gcs.Bucket, scratch: gcs.Bucket, *, seed_prefix: str, limit: int) -> int:
    """Copy the first `limit` paired seed objects from `source` into `scratch` (idempotent).

    The copy is a server-side GCS rewrite, so the seed bytes never round-trip through
    this process; existing objects are left untouched.
    """
    pairing = ingest_beam.pair_seed_keys(
        (blob.name for blob in source.list_blobs(prefix=seed_prefix)), prefix=seed_prefix
    )
    refs = pairing.refs[:limit]
    if len(pairing.refs) < limit:
        _LOG.warning('source has only %d paired seed object(s) (< limit %d)', len(pairing.refs), limit)
    keys = [key for ref in refs for key in (ref.json_key, ref.pdf_key)]

    def _copy(key: str) -> None:
        if not scratch.blob(key).exists():
            source.copy_blob(source.blob(key), scratch, new_name=key)

    with concurrent.futures.ThreadPoolExecutor(max_workers=_STAGE_CONCURRENCY) as pool:
        list(pool.map(_copy, keys))
    return len(refs)


def _dataflow_options(
    *, target: _Target, scratch_bucket: str, sdk_image: str, job_name: str, max_workers: int
) -> pipeline_options.PipelineOptions:
    """Dataflow Runner v2 options: workers run as the ingestion SA off the custom image."""
    return pipeline_options.PipelineOptions(
        [
            '--runner=DataflowRunner',
            f'--project={target.project}',
            f'--region={target.region}',
            f'--temp_location=gs://{scratch_bucket}/dataflow/tmp',
            f'--staging_location=gs://{scratch_bucket}/dataflow/staging',
            f'--sdk_container_image={sdk_image}',
            '--experiments=use_runner_v2',
            # Headroom for reading + parsing large seed pdfs whole in memory (the oversized ones
            # are dead-lettered, but legitimate large papers still load fully).
            '--worker_machine_type=n1-standard-2',
            f'--subnetwork={target.subnetwork}',
            f'--service_account_email={target.ingest_sa}',
            f'--job_name={job_name}',
            # Scale the parallel write phase up to the cap by backlog; resolution stays on one
            # worker regardless (single-shard by design), so the pool idles back down for it.
            '--autoscaling_algorithm=THROUGHPUT_BASED',
            f'--max_num_workers={max_workers}',
            '--labels=cost-monitor-expiry=48h',  # cost attribution for the bounded run
        ]
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--sdk-image', required=True, help='the litcache worker image in Artifact Registry')
    parser.add_argument('--project', default=_DEFAULT_PROJECT, help='GCP project to run in (default: %(default)s)')
    parser.add_argument('--region', default=_DEFAULT_REGION, help='GCP region to run in (default: %(default)s)')
    parser.add_argument(
        '--scratch-bucket',
        default=None,
        help='GCS bucket for staged seed + output papers/ + Dataflow staging/temp '
        '(default: <project>-litcache-scratch)',
    )
    parser.add_argument(
        '--source-bucket',
        default=None,
        help='bucket holding the real ingest/ seed (default: <project>-fulltext)',
    )
    parser.add_argument('--seed-prefix', default='ingest/', help='seed prefix in both buckets')
    parser.add_argument(
        '--limit', type=int, default=None, help='cap papers ingested; omit to ingest all (required unless --direct)'
    )
    parser.add_argument('--max-workers', type=int, default=2, help='Dataflow max workers (default: %(default)s)')
    parser.add_argument(
        '--direct',
        action='store_true',
        help='read ingest/ from and write papers/ to --source-bucket directly (the real store), no staging',
    )
    parser.add_argument(
        '--max-dead-letters',
        type=int,
        default=0,
        help='papers the job may dead-letter before it is reported a failure (default: %(default)s)',
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error('--limit must be positive')
    if args.max_dead_letters < 0:
        parser.error('--max-dead-letters must not be negative')
    if not args.direct and args.limit is None:
        parser.error('--limit is required unless --direct (a staged sample must be bounded)')
    args.target = _Target(project=args.project, region=args.region)
    # Bucket defaults follow --project, so naming one project cannot leave a run reading or
    # writing another's.
    if args.scratch_bucket is None:
        args.scratch_bucket = args.target.default_scratch_bucket
    if args.source_bucket is None:
        args.source_bucket = args.target.default_source_bucket
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Stage, submit the Dataflow job, block on it, and print the report. Returns an exit code."""
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s', force=True)

    now = datetime.datetime.now(tz=datetime.UTC)
    job_name = f'litcache-ingest-{now:%Y%m%d-%H%M%S}'

    if args.direct:
        run_bucket = args.source_bucket
        _LOG.info('direct run against the store gs://%s (no staging)', run_bucket)
    else:
        source = _open_bucket(args.source_bucket)
        scratch = _open_bucket(args.scratch_bucket)
        staged = _stage_seed(source, scratch, seed_prefix=args.seed_prefix, limit=args.limit)
        _LOG.info('staged %d seed pair(s) into gs://%s/%s', staged, args.scratch_bucket, args.seed_prefix)
        run_bucket = args.scratch_bucket

    conn_factory = cloudsql.CrosswalkConnFactory(
        connection_name=args.target.sql_connection_name,
        database=_SQL_DATABASE,
        iam_user=args.target.ingest_db_user,
    )
    bucket_factory = functools.partial(_open_bucket, run_bucket)
    options = _dataflow_options(
        target=args.target,
        scratch_bucket=args.scratch_bucket,
        sdk_image=args.sdk_image,
        job_name=job_name,
        max_workers=args.max_workers,
    )

    run = ingest_beam.run_ingestion(
        bucket_factory=bucket_factory,
        conn_factory=conn_factory,
        licence=_licence_fallback(),
        now=now,
        options=options,
        seed_prefix=args.seed_prefix,
        limit=args.limit,
    )
    _LOG.info('submitted Dataflow job %s (project %s, region %s)', job_name, args.target.project, args.target.region)
    state = run.result.wait_until_finish()
    if state != runner.PipelineState.DONE:
        raise RuntimeError(f'Dataflow job did not complete cleanly (state: {state})')

    rep = ingest_beam.report_run(run, bucket_factory, seed_prefix=args.seed_prefix)
    print(report.render_report(rep))
    print(f'\nOutput written to gs://{run_bucket}/papers/; crosswalk minted into {args.target.sql_connection_name}.')
    if rep.dead_lettered > args.max_dead_letters:
        # Per-paper isolation keeps the job in DONE however many papers failed, so the
        # dead-letter count is the only thing separating a clean pass from a total one.
        print(
            f'\n{rep.dead_lettered} paper(s) dead-lettered, over the --max-dead-letters '
            f'tolerance of {args.max_dead_letters}: see '
            f'gs://{run_bucket}/{run.dead_letter_summary}',
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
