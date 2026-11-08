"""The reference-build Cloud Run Job: mirror the pinned upstreams, then the Entrez status tables, then build.

Each stage is idempotent — the mirror skips an object still matching its provenance, a historical
release with a status table is not fetched again, and a build whose prefix exists is skipped — so a
run with nothing new to do reads provenance and exits. A stage's failure does not stop the next: a
build reads only what it can verify was mirrored, and fails on its own when an input did not land.
The run exits non-zero if anything failed.

Two executions must not overlap: the second would find the first's objects half-written and report
them as wreckage to delete. Nothing here may delete an object, so the guard is a pair of markers each
attempt adds under ``reference-runs/<execution>/`` — ``<attempt>.started``, then ``<attempt>.finished``
however it ends. Per attempt, because Cloud Run retries a failed execution under the same name and
the job timeout bounds one attempt, not the execution. An attempt marks itself first and then looks:
if another execution's attempt is started, unfinished and younger than the timeout (past which Cloud
Run has killed it), the earlier of the two goes ahead and the later finishes at once, refused. Two
attempts starting together both see each other, and the same tie-break picks one. An earlier attempt
of its own execution is no rival: Cloud Run starts a retry only once that attempt has ended.

A refused execution is not queued: it fails, and pages, saying which execution it waited on, for a
person to run again once that one has finished.

Run only as the Job, which sets ``CLOUD_RUN_EXECUTION``, ``CLOUD_RUN_TASK_ATTEMPT`` and
``THEMIS_REFERENCE_BUILD_TIMEOUT_SECONDS``; an operator runs the stages' own modules.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import os
import sys
from collections.abc import Iterator

from google.cloud import storage

from tools.resources import build, manifest, mirror, status

RUNS = 'reference-runs'
_STARTED = '.started'
_FINISHED = '.finished'


class OverlapError(Exception):
    """Another execution of the Job is still running."""


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'{name} is not set; this runs as the reference-build Job, and an operator runs its stages')
    return value


@dataclasses.dataclass(frozen=True, order=True)
class Attempt:
    """One attempt of one execution, ordered by when it marked itself started."""

    started: dt.datetime
    execution: str
    attempt: str


def live(bucket: storage.Bucket, *, now: dt.datetime, timeout: dt.timedelta) -> list[Attempt]:
    """The attempts started, unfinished and younger than `timeout`, earliest first."""
    blobs = {blob.name: blob for blob in bucket.list_blobs(prefix=f'{RUNS}/')}
    found = []
    for name, blob in blobs.items():
        if not name.endswith(_STARTED) or blob.time_created is None:
            continue
        execution, _, attempt = name.removeprefix(f'{RUNS}/').removesuffix(_STARTED).rpartition('/')
        if f'{RUNS}/{execution}/{attempt}{_FINISHED}' in blobs or now - blob.time_created >= timeout:
            continue
        found.append(Attempt(blob.time_created, execution, attempt))
    return sorted(found)


def _mark(bucket: storage.Bucket, execution: str, attempt: str, suffix: str) -> None:
    bucket.blob(f'{RUNS}/{execution}/{attempt}{suffix}').upload_from_string(b'', if_generation_match=0)


@contextlib.contextmanager
def exclusive(bucket: storage.Bucket, execution: str, attempt: str, *, timeout: dt.timedelta) -> Iterator[None]:
    """Hold the Job's run for this attempt while the block runs.

    Raises:
        OverlapError: If another execution's live attempt marked itself started first.
    """
    _mark(bucket, execution, attempt, _STARTED)
    try:
        attempts = live(bucket, now=dt.datetime.now(dt.UTC), timeout=timeout)
        rivals = [a for a in attempts if a.execution != execution]
        own = [a for a in attempts if (a.execution, a.attempt) == (execution, attempt)]
        if rivals and (not own or rivals[0] < own[0]):
            raise OverlapError(f'{rivals[0].execution} still running; run again once it has finished')
        yield
    finally:
        try:
            _mark(bucket, execution, attempt, _FINISHED)
        except mirror.TRANSFER_ERRORS as error:  # never masks the block's own exception
            print(
                f'could not mark {execution}/{attempt} finished; it reads as running until the timeout: {error}',
                file=sys.stderr,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', required=True, help='GCP project owning the resources bucket')
    args = parser.parse_args()
    execution = _required_env('CLOUD_RUN_EXECUTION')
    attempt = _required_env('CLOUD_RUN_TASK_ATTEMPT')
    timeout = dt.timedelta(seconds=int(_required_env('THEMIS_REFERENCE_BUILD_TIMEOUT_SECONDS')))

    pinned = manifest.load()
    client = storage.Client(project=args.project)
    bucket = mirror.resources_bucket(client, args.project)
    try:
        with exclusive(bucket, execution, attempt, timeout=timeout):
            print('mirror', file=sys.stderr)
            failed = mirror.mirror_all(
                pinned.entries, dataset=pinned.dataset, bucket=bucket, client=client, dry_run=False
            )
            print('status tables', file=sys.stderr)
            failed += status.mirror_all(pinned, bucket=bucket, today=dt.datetime.now(dt.UTC).date(), dry_run=False)
            print('builds', file=sys.stderr)
            failed += build.build_all(pinned, bucket=bucket, dry_run=False)
    except OverlapError as error:
        raise SystemExit(f'not started: {error}') from error
    if failed:
        print(f'{len(failed)} failed: {", ".join(failed)}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
