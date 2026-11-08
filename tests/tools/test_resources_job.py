"""Tests for tools.resources.job: two executions of the reference build never overlap."""

from __future__ import annotations

import datetime as dt

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
