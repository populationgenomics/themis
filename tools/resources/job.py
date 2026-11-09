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

"However it ends" includes being stopped. Cloud Run stops an attempt — a cancelled execution, one past
its timeout — with SIGTERM and kills it with SIGKILL seconds later. Python installs no SIGTERM handler, and
the container's init process, which this is, ignores a signal it does not handle; left so, the attempt
would run on until SIGKILL and read as running, refusing every new execution until the timeout. So
SIGTERM is raised as an exception, and the attempt marks itself finished on its way out, in a write
bounded to fit the grace period; a second SIGTERM during that write is dropped.

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
import signal
import sys
import types
from collections.abc import Iterator

from google.api_core import exceptions as google_exceptions
from google.auth import exceptions as google_auth_exceptions
from google.cloud import storage
from google.cloud.storage import retry as storage_retry

from tools.resources import build, manifest, mirror, status

RUNS = 'reference-runs'
_STARTED = '.started'
_FINISHED = '.finished'
# The finished marker's write, inside the ten seconds Cloud Run gives a stopped attempt before SIGKILL, with
# room for the unwind before it: no new try starts past the deadline, and a try runs at most its timeout
# connecting and again reading.
_FINISHED_TIMEOUT_SECONDS = 2
_FINISHED_RETRY = storage_retry.ConditionalRetryPolicy(
    storage_retry.DEFAULT_RETRY.with_timeout(3),
    storage_retry.is_generation_specified,
    ['query_params'],
)
# What a shell reports for a process ended by SIGTERM.
_STOPPED_EXIT = 128 + signal.SIGTERM


class OverlapError(Exception):
    """Another execution of the Job is still running."""


class Stopped(BaseException):
    """Cloud Run sent SIGTERM. A BaseException, so no `except Exception` on the way out swallows it."""


def _raise_stopped(signum: int, _: types.FrameType | None) -> None:
    raise Stopped(signal.Signals(signum).name)


@contextlib.contextmanager
def stoppable() -> Iterator[None]:
    """Raise `Stopped` on SIGTERM while the block runs, so its `finally` clauses run before the process ends."""
    previous = signal.signal(signal.SIGTERM, _raise_stopped)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


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


@contextlib.contextmanager
def _sigterm_dropped() -> Iterator[None]:
    """Drop a SIGTERM arriving in the block.

    A handler swapped in for the block, not a signal mask: the kernel hands a process signal to any
    thread that does not mask it, and Python runs the handler on the main thread regardless.
    """
    previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _mark_started(bucket: storage.Bucket, execution: str, attempt: str) -> None:
    """Add the started marker; one already there is this attempt's own, from a try whose response was lost."""
    with contextlib.suppress(google_exceptions.PreconditionFailed):
        bucket.blob(f'{RUNS}/{execution}/{attempt}{_STARTED}').upload_from_string(b'', if_generation_match=0)


def _mark_finished(bucket: storage.Bucket, execution: str, attempt: str) -> None:
    """Add the finished marker within a stopped attempt's grace, dropping a SIGTERM during it.

    The attempt is ending either way, and a SIGTERM raised here would replace the exception it is
    ending with.
    """
    with _sigterm_dropped(), contextlib.suppress(google_exceptions.PreconditionFailed):
        bucket.blob(f'{RUNS}/{execution}/{attempt}{_FINISHED}').upload_from_string(
            b'', if_generation_match=0, timeout=_FINISHED_TIMEOUT_SECONDS, retry=_FINISHED_RETRY
        )


@contextlib.contextmanager
def exclusive(bucket: storage.Bucket, execution: str, attempt: str, *, timeout: dt.timedelta) -> Iterator[None]:
    """Hold the Job's run for this attempt while the block runs.

    Raises:
        OverlapError: If another execution's live attempt marked itself started first.
    """
    try:
        # Inside the try: a started marker that landed, however its write then ended (a stop, a lost response), is
        # paired with a finished one. A finished marker without a started one is never read.
        _mark_started(bucket, execution, attempt)
        attempts = live(bucket, now=dt.datetime.now(dt.UTC), timeout=timeout)
        rivals = [a for a in attempts if a.execution != execution]
        own = [a for a in attempts if (a.execution, a.attempt) == (execution, attempt)]
        if rivals and (not own or rivals[0] < own[0]):
            raise OverlapError(f'{rivals[0].execution} still running; run again once it has finished')
        yield
    finally:
        try:
            _mark_finished(bucket, execution, attempt)
        # Never masks the block's own exception.
        except (*mirror.TRANSFER_ERRORS, google_auth_exceptions.GoogleAuthError) as error:
            print(
                f'could not mark {execution}/{attempt} finished; it reads as running until the timeout: {error}',
                file=sys.stderr,
            )


def _run() -> list[str]:
    """Run the stages under the overlap guard; return what failed."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', required=True, help='GCP project owning the resources bucket')
    args = parser.parse_args()
    execution = _required_env('CLOUD_RUN_EXECUTION')
    attempt = _required_env('CLOUD_RUN_TASK_ATTEMPT')
    timeout = dt.timedelta(seconds=int(_required_env('THEMIS_REFERENCE_BUILD_TIMEOUT_SECONDS')))

    pinned = manifest.load()
    client = storage.Client(project=args.project)
    bucket = mirror.resources_bucket(client, args.project)
    with exclusive(bucket, execution, attempt, timeout=timeout):
        print('mirror', file=sys.stderr)
        failed = mirror.mirror_all(pinned.entries, dataset=pinned.dataset, bucket=bucket, client=client, dry_run=False)
        print('status tables', file=sys.stderr)
        failed += status.mirror_all(pinned, bucket=bucket, today=dt.datetime.now(dt.UTC).date(), dry_run=False)
        print('builds', file=sys.stderr)
        failed += build.build_all(pinned, bucket=bucket, dry_run=False)
    return failed


def main() -> None:
    try:
        # First thing: PID 1 ignores a SIGTERM it has no handler for; one during the imports, before this, still is.
        with stoppable():
            failed = _run()
    except OverlapError as error:
        raise SystemExit(f'not started: {error}') from error
    except Stopped as stopped:
        print(f'stopped by {stopped}', file=sys.stderr)
        raise SystemExit(_STOPPED_EXIT) from stopped
    if failed:
        print(f'{len(failed)} failed: {", ".join(failed)}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
