"""Tests for tools.resources.job: two executions of the reference build never overlap."""

from __future__ import annotations

import datetime as dt
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable

import pytest
from google.cloud import storage

from tools.resources import job

_TIMEOUT = dt.timedelta(hours=6)


def test_an_execution_refuses_to_start_while_another_runs(gcs_bucket: storage.Bucket) -> None:
    with (
        job.exclusive(gcs_bucket, 'exec-a', '0', timeout=_TIMEOUT),
        pytest.raises(job.OverlapError, match='exec-a still running'),
        job.exclusive(gcs_bucket, 'exec-b', '0', timeout=_TIMEOUT),
    ):
        pass
    with job.exclusive(gcs_bucket, 'exec-b', '1', timeout=_TIMEOUT):
        pass


def test_of_two_attempts_that_both_marked_themselves_the_earlier_goes_ahead(gcs_bucket: storage.Bucket) -> None:
    # exec-b marked itself started before exec-a looked: the two see each other and exec-b is the earlier.
    gcs_bucket.blob(f'{job.RUNS}/exec-b/0.started').upload_from_string(b'')
    with pytest.raises(job.OverlapError, match='exec-b'), job.exclusive(gcs_bucket, 'exec-a', '0', timeout=_TIMEOUT):
        pass
    assert [a.execution for a in job.live(gcs_bucket, now=dt.datetime.now(dt.UTC), timeout=_TIMEOUT)] == ['exec-b']


def test_a_retry_holds_the_run_after_its_failed_attempt_finished(gcs_bucket: storage.Bucket) -> None:
    with pytest.raises(RuntimeError), job.exclusive(gcs_bucket, 'exec-a', '0', timeout=_TIMEOUT):
        raise RuntimeError
    with (
        job.exclusive(gcs_bucket, 'exec-a', '1', timeout=_TIMEOUT),
        pytest.raises(job.OverlapError, match='exec-a'),
        job.exclusive(gcs_bucket, 'exec-b', '0', timeout=_TIMEOUT),
    ):
        pass


def test_a_retry_after_its_killed_attempt_is_not_its_own_rival(gcs_bucket: storage.Bucket) -> None:
    gcs_bucket.blob(f'{job.RUNS}/exec-a/0.started').upload_from_string(b'')
    with job.exclusive(gcs_bucket, 'exec-a', '1', timeout=_TIMEOUT):
        pass


def test_an_attempt_older_than_the_timeout_is_not_live(gcs_bucket: storage.Bucket) -> None:
    gcs_bucket.blob(f'{job.RUNS}/exec-a/0.started').upload_from_string(b'')
    later = dt.datetime.now(dt.UTC) + _TIMEOUT + dt.timedelta(minutes=1)
    assert job.live(gcs_bucket, now=later, timeout=_TIMEOUT) == []


def _stop_this_process() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def _sleep_in_a_child() -> None:
    timer = threading.Timer(0.5, _stop_this_process)
    timer.start()
    try:
        subprocess.run([sys.executable, '-c', 'import time; time.sleep(60)'], check=True)
    finally:
        timer.cancel()


@pytest.mark.parametrize('body', [_stop_this_process, _sleep_in_a_child], ids=['directly', 'during-a-subprocess'])
def test_an_attempt_stopped_by_sigterm_marks_itself_finished(
    gcs_bucket: storage.Bucket, body: Callable[[], None]
) -> None:
    began = time.monotonic()
    with (
        pytest.raises(job.Stopped),
        job.stoppable(),
        job.exclusive(gcs_bucket, 'exec-a', '0', timeout=_TIMEOUT),
    ):
        body()
    assert time.monotonic() - began < 10, 'the stop has to end the attempt within the grace period'
    assert gcs_bucket.blob(f'{job.RUNS}/exec-a/0.finished').exists()


def test_an_attempt_stopped_while_marking_itself_started_marks_itself_finished(
    gcs_bucket: storage.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = storage.Blob.upload_from_string

    def stopped_midway(blob: storage.Blob, *args: object, **kwargs: object) -> None:
        upload(blob, *args, **kwargs)  # pyright: ignore[reportArgumentType]
        if str(blob.name).endswith('.started'):
            _stop_this_process()

    monkeypatch.setattr(storage.Blob, 'upload_from_string', stopped_midway)
    with (
        pytest.raises(job.Stopped),
        job.stoppable(),
        job.exclusive(gcs_bucket, 'exec-a', '0', timeout=_TIMEOUT),
    ):
        pytest.fail('a stop during the started marker ends the attempt before its body')
    assert gcs_bucket.blob(f'{job.RUNS}/exec-a/0.finished').exists()


def test_a_stop_while_marking_itself_finished_keeps_the_error_the_attempt_ends_with(
    gcs_bucket: storage.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload = storage.Blob.upload_from_string

    def stopped_midway(blob: storage.Blob, *args: object, **kwargs: object) -> None:
        if str(blob.name).endswith('.finished'):
            _stop_this_process()
        upload(blob, *args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(storage.Blob, 'upload_from_string', stopped_midway)
    with (
        pytest.raises(RuntimeError, match='the build broke'),
        job.stoppable(),
        job.exclusive(gcs_bucket, 'exec-a', '0', timeout=_TIMEOUT),
    ):
        raise RuntimeError('the build broke')
    assert gcs_bucket.blob(f'{job.RUNS}/exec-a/0.finished').exists()


def test_a_started_marker_whose_write_already_landed_is_not_a_failure(gcs_bucket: storage.Bucket) -> None:
    gcs_bucket.blob(f'{job.RUNS}/exec-a/0.started').upload_from_string(b'')
    entered = False
    with job.exclusive(gcs_bucket, 'exec-a', '0', timeout=_TIMEOUT):
        entered = True
    assert entered
