"""Tests for the job log (`themis.litcache.joblog`): the verdicts on a set of entries, and the appends.

The appends run against a fake-gcs-server bucket (Docker-gated via the shared `gcs_bucket` fixture), so the generation
preconditions are the ones production meets.
"""

from __future__ import annotations

import datetime

import pytest
from google.cloud import storage as gcs

from themis.litcache import joblog
from themis.litcache.models import work_pb2

_DOC_ID = '7c1e0000-0000-4000-8000-000000000000'
_PDF = '9f3a' + '0' * 60
_OTHER_PDF = '4b2c' + '0' * 60
_STEP = 'transcription'
_NOW = datetime.datetime(2026, 10, 21, 15, 20, tzinfo=datetime.UTC)
_DEADLINE = datetime.timedelta(minutes=30)
_YOUNG = _NOW - datetime.timedelta(minutes=10)
_OLD = _NOW - datetime.timedelta(minutes=31)

_TASK = '7c1e--sweep-20261021T1500'


def _attempt(
    n: int, *, task: str = '7c1e--gen-5', retry_count: int = 0, queue: str = 'themis-work-bulk'
) -> joblog.Attempt:
    return joblog.Attempt(id=f'00000000-0000-4000-8000-{n:012d}', queue=queue, task=task, retry_count=retry_count)


# The attempt asking, in every assessment below.
_ASKING = _attempt(0, task=_TASK, retry_count=2)


def _begun(
    attempt: joblog.Attempt, at: datetime.datetime = _YOUNG, inputs: tuple[str, ...] = (_PDF,)
) -> work_pb2.JobLogEntry:
    return attempt.entry(at=at, step=_STEP, inputs=inputs, event=work_pb2.JOB_EVENT_BEGUN)


def _closed(
    attempt: joblog.Attempt, event: work_pb2.JobEvent, *, model_answered: bool = False
) -> list[work_pb2.JobLogEntry]:
    return [
        _begun(attempt),
        attempt.entry(at=_YOUNG, step=_STEP, inputs=(_PDF,), event=event, model_answered=model_answered),
    ]


def _failed(attempt: joblog.Attempt, *, model_answered: bool) -> list[work_pb2.JobLogEntry]:
    return _closed(attempt, work_pb2.JOB_EVENT_FAILED, model_answered=model_answered)


def _settled(attempt: joblog.Attempt, inputs: tuple[str, ...] = (_PDF,)) -> work_pb2.JobLogEntry:
    return attempt.entry(
        at=_YOUNG, step=_STEP, inputs=inputs, event=work_pb2.JOB_EVENT_SETTLED, outcome=work_pb2.SETTLED_OUTCOME_NOTHING
    )


def _assess(entries: list[work_pb2.JobLogEntry]) -> joblog.Verdict:
    return joblog.assess(
        work_pb2.JobLog(entries=entries), step=_STEP, inputs={_PDF}, attempt=_ASKING, now=_NOW, deadline=_DEADLINE
    )


@pytest.mark.parametrize(
    ('entries', 'verdict'),
    [
        pytest.param([], joblog.Verdict.WORK, id='nothing tried'),
        pytest.param([_settled(_attempt(1))], joblog.Verdict.SETTLED, id='settled'),
        pytest.param([_settled(_attempt(1), inputs=(_OTHER_PDF,))], joblog.Verdict.WORK, id='settled on other inputs'),
        pytest.param([_begun(_attempt(1))], joblog.Verdict.RUNNING, id='another attempt is running'),
        pytest.param([_begun(_attempt(1), at=_OLD)], joblog.Verdict.WORK, id='another attempt died at the deadline'),
        pytest.param([_begun(_ASKING)], joblog.Verdict.BEGUN, id='this attempt began it'),
        pytest.param(
            [_begun(_attempt(1, task=_TASK, retry_count=1))],
            joblog.Verdict.WORK,
            id='an earlier delivery of this task is dead',
        ),
        pytest.param(
            [_begun(_attempt(1, task=_TASK, retry_count=2))],
            joblog.Verdict.RUNNING,
            id='a duplicate delivery is running',
        ),
        pytest.param(
            [_begun(_attempt(1, task=_TASK, retry_count=1, queue='themis-work-interactive'))],
            joblog.Verdict.RUNNING,
            id='the same name on another queue is another task',
        ),
        pytest.param(
            [_begun(_attempt(1, task='7c1e--main_text', retry_count=5))],
            joblog.Verdict.RUNNING,
            id='another task is running',
        ),
        pytest.param(
            [e for n in (1, 2, 3) for e in _closed(_attempt(n), work_pb2.JOB_EVENT_REFUSED)],
            joblog.Verdict.WORK,
            id='refusals spend nothing',
        ),
        pytest.param(
            [e for n in (1, 2, 3) for e in _failed(_attempt(n), model_answered=False)],
            joblog.Verdict.WORK,
            id='failures before the model answered spend nothing',
        ),
        pytest.param(
            [e for n in (1, 2) for e in _failed(_attempt(n), model_answered=True)],
            joblog.Verdict.WORK,
            id='two spent attempts',
        ),
        pytest.param(
            [
                *_failed(_attempt(1), model_answered=True),
                *_failed(_attempt(2), model_answered=True),
                _begun(_attempt(3), at=_OLD),
            ],
            joblog.Verdict.SPENT,
            id='two answered failures and a dead attempt',
        ),
        pytest.param(
            _closed(_attempt(1), 99),  # type: ignore[arg-type]
            joblog.Verdict.RUNNING,
            id='an unknown event leaves a begun entry running',
        ),
        pytest.param(
            [
                e
                for n in (1, 2, 3)
                for e in (
                    _begun(_attempt(n), at=_OLD),
                    _attempt(n).entry(at=_OLD, step=_STEP, inputs=(_PDF,), event=99),  # type: ignore[arg-type]
                )
            ],
            joblog.Verdict.SPENT,
            id='and dead, and spent, once older than the deadline',
        ),
        pytest.param(
            [*(e for n in (1, 2, 3) for e in _failed(_attempt(n), model_answered=True)), _begun(_attempt(4))],
            joblog.Verdict.RUNNING,
            id='a running attempt outranks the spent ones',
        ),
        pytest.param(
            [_begun(_ASKING), _settled(_attempt(1))], joblog.Verdict.SETTLED, id='a settled step outranks a begun one'
        ),
    ],
)
def test_the_verdict_on_a_set_of_entries(entries: list[work_pb2.JobLogEntry], verdict: joblog.Verdict) -> None:
    assert _assess(entries) == verdict


def test_inputs_are_compared_as_a_set() -> None:
    entry = _settled(_attempt(1), inputs=(_OTHER_PDF, _PDF))
    log = work_pb2.JobLog(entries=[entry])

    for inputs in ({_PDF, _OTHER_PDF}, [_OTHER_PDF, _PDF]):
        verdict = joblog.assess(log, step=_STEP, inputs=inputs, attempt=_ASKING, now=_NOW, deadline=_DEADLINE)
        assert verdict == joblog.Verdict.SETTLED
    assert _assess([entry]) == joblog.Verdict.WORK  # a subset is other inputs


def test_the_order_of_entries_decides_nothing() -> None:
    entries = [
        *_failed(_attempt(1), model_answered=True),
        _begun(_attempt(2), at=_OLD),
        *_failed(_attempt(3), model_answered=True),
    ]

    assert _assess(entries) == _assess(entries[::-1]) == joblog.Verdict.SPENT


def _begin(bucket: gcs.Bucket, attempt: joblog.Attempt) -> joblog.Verdict:
    return joblog.begin(bucket, _DOC_ID, step=_STEP, inputs={_PDF}, attempt=attempt, now=_NOW, deadline=_DEADLINE)


def test_an_absent_log_reads_as_empty(gcs_bucket: gcs.Bucket) -> None:
    assert joblog.read(gcs_bucket, _DOC_ID) == work_pb2.JobLog()


def test_appends_join_and_keep_the_bytes_already_written(gcs_bucket: gcs.Bucket) -> None:
    first = _closed(_attempt(1), work_pb2.JOB_EVENT_REFUSED)
    joblog.append(gcs_bucket, _DOC_ID, first)
    held = gcs_bucket.blob(joblog.job_log_path(_DOC_ID)).download_as_bytes()

    joblog.append(gcs_bucket, _DOC_ID, [_settled(_attempt(2))])

    assert gcs_bucket.blob(joblog.job_log_path(_DOC_ID)).download_as_bytes().startswith(held)
    assert list(joblog.read(gcs_bucket, _DOC_ID).entries) == [*first, _settled(_attempt(2))]


def test_an_append_refuses_a_malformed_entry_and_writes_nothing(gcs_bucket: gcs.Bucket) -> None:
    malformed = _attempt(1).entry(at=_NOW, step=_STEP, inputs=(_PDF,), event=work_pb2.JOB_EVENT_SETTLED)  # no outcome

    with pytest.raises(ValueError, match='outcome_iff_settled'):
        joblog.append(gcs_bucket, _DOC_ID, [_settled(_attempt(2)), malformed])
    assert not gcs_bucket.blob(joblog.job_log_path(_DOC_ID)).exists()


def test_an_append_losing_its_race_keeps_the_winners_entry(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = joblog._write
    raced: list[int] = []

    def write_after_another(blob: gcs.Blob, data: bytes, generation: int) -> bool:
        if not raced:
            raced.append(1)
            real_write(
                gcs_bucket.blob(blob.name),
                work_pb2.JobLog(entries=[_settled(_attempt(2))]).SerializeToString(),
                generation,
            )
        return real_write(blob, data, generation)

    monkeypatch.setattr(joblog, '_write', write_after_another)
    joblog.append(gcs_bucket, _DOC_ID, [_settled(_attempt(1))])

    assert list(joblog.read(gcs_bucket, _DOC_ID).entries) == [_settled(_attempt(2)), _settled(_attempt(1))]


def test_an_append_that_never_wins_fails_loud(gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(joblog, '_write', lambda _blob, _data, _generation: False)

    with pytest.raises(joblog.LogContentionError, match='did not converge'):
        joblog.append(gcs_bucket, _DOC_ID, [_settled(_attempt(1))])


def test_the_first_attempt_to_begin_holds_the_step(gcs_bucket: gcs.Bucket) -> None:
    assert _begin(gcs_bucket, _attempt(1)) == joblog.Verdict.BEGUN
    assert _begin(gcs_bucket, _attempt(2, task='7c1e--main_text')) == joblog.Verdict.RUNNING
    assert _begin(gcs_bucket, _attempt(1)) == joblog.Verdict.BEGUN
    assert len(joblog.read(gcs_bucket, _DOC_ID).entries) == 1


def test_of_two_attempts_beginning_at_once_the_second_backs_off(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other attempt appends its begun entry between this one's read and its write, so this one's write fails and
    # its reread finds the other running.
    real_write = joblog._write
    raced: list[int] = []

    def write_after_another_begins(blob: gcs.Blob, data: bytes, generation: int) -> bool:
        if not raced:
            raced.append(1)
            assert _begin(gcs_bucket, _attempt(2, task='7c1e--main_text')) == joblog.Verdict.BEGUN
        return real_write(blob, data, generation)

    monkeypatch.setattr(joblog, '_write', write_after_another_begins)

    assert _begin(gcs_bucket, _attempt(1)) == joblog.Verdict.RUNNING
    assert [e.attempt for e in joblog.read(gcs_bucket, _DOC_ID).entries] == [_attempt(2).id]


def test_a_begin_whose_response_was_lost_holds_the_step(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The append lands but its response is lost, so the client's retry answers 412; the reread finds this attempt's
    # own entry.
    real_write = joblog._write
    lost: list[int] = []

    def write_losing_the_response(blob: gcs.Blob, data: bytes, generation: int) -> bool:
        written = real_write(blob, data, generation)
        if not lost:
            lost.append(1)
            return False
        return written

    monkeypatch.setattr(joblog, '_write', write_losing_the_response)

    assert _begin(gcs_bucket, _attempt(1)) == joblog.Verdict.BEGUN
    assert len(joblog.read(gcs_bucket, _DOC_ID).entries) == 1


@pytest.mark.parametrize(
    ('age', 'verdict'),
    [(_DEADLINE, joblog.Verdict.RUNNING), (_DEADLINE + datetime.timedelta(seconds=1), joblog.Verdict.WORK)],
)
def test_a_begun_entry_dies_once_older_than_the_deadline(age: datetime.timedelta, verdict: joblog.Verdict) -> None:
    assert _assess([_begun(_attempt(1), at=_NOW - age)]) == verdict


def test_an_answered_failure_with_no_begun_entry_counts() -> None:
    entries = [
        _attempt(n).entry(at=_YOUNG, step=_STEP, inputs=(_PDF,), event=work_pb2.JOB_EVENT_FAILED, model_answered=True)
        for n in (1, 2, 3)
    ]

    assert _assess(entries) == joblog.Verdict.SPENT


def test_an_unknown_event_alone_is_no_attempt() -> None:
    unknown = [
        _attempt(n).entry(at=_YOUNG, step=_STEP, inputs=(_PDF,), event=99)  # type: ignore[arg-type]
        for n in (1, 2, 3)
    ]

    assert _assess(unknown) == joblog.Verdict.WORK


def test_naive_times_are_refused() -> None:
    naive = _NOW.replace(tzinfo=None)

    with pytest.raises(ValueError, match='time zone'):
        _attempt(1).entry(at=naive, step=_STEP, inputs=(_PDF,), event=work_pb2.JOB_EVENT_BEGUN)
    with pytest.raises(ValueError, match='time zone'):
        joblog.assess(work_pb2.JobLog(), step=_STEP, inputs={_PDF}, attempt=_ASKING, now=naive, deadline=_DEADLINE)


def test_an_empty_append_is_refused(gcs_bucket: gcs.Bucket) -> None:
    with pytest.raises(ValueError, match='nothing to append'):
        joblog.append(gcs_bucket, _DOC_ID, [])
    assert not gcs_bucket.blob(joblog.job_log_path(_DOC_ID)).exists()


@pytest.mark.parametrize(
    ('entries', 'verdict'),
    [
        pytest.param([_settled(_attempt(2))], joblog.Verdict.SETTLED, id='settled'),
        pytest.param(
            [e for n in (2, 3, 4) for e in _failed(_attempt(n), model_answered=True)], joblog.Verdict.SPENT, id='spent'
        ),
    ],
)
def test_begin_appends_nothing_when_the_step_has_no_work(
    gcs_bucket: gcs.Bucket, entries: list[work_pb2.JobLogEntry], verdict: joblog.Verdict
) -> None:
    joblog.append(gcs_bucket, _DOC_ID, entries)

    assert _begin(gcs_bucket, _attempt(1)) == verdict
    assert len(joblog.read(gcs_bucket, _DOC_ID).entries) == len(entries)


def test_an_attempt_cannot_begin_a_step_it_has_closed(gcs_bucket: gcs.Bucket) -> None:
    joblog.append(gcs_bucket, _DOC_ID, _failed(_attempt(1), model_answered=False))

    with pytest.raises(ValueError, match='already closed'):
        _begin(gcs_bucket, _attempt(1))
