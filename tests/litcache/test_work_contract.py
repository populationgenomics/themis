"""Guard the work contract: the job log's append-by-concatenation, and its messages' protovalidate rules."""

from __future__ import annotations

import datetime

import protovalidate
import pytest
from google.protobuf import timestamp_pb2

from themis.litcache.models import work_pb2

_ATTEMPT = '0b9e2c4a-5d1f-4e8a-9c3b-7a6d5e4f3c2b'
_DOC_ID = '7c1e0000-0000-4000-8000-000000000000'
_PDF_HASH = '9f3a' + '0' * 60


def _entry(
    *,
    attempt: str = _ATTEMPT,
    task: str = '7c1e--main_text',
    retry_count: int | None = 0,
    step: str = 'transcription',
    inputs: tuple[str, ...] = (_PDF_HASH,),
    event: work_pb2.JobEvent = work_pb2.JOB_EVENT_BEGUN,
    outcome: work_pb2.SettledOutcome = work_pb2.SETTLED_OUTCOME_UNSPECIFIED,
    model_answered: bool = False,
) -> work_pb2.JobLogEntry:
    at = timestamp_pb2.Timestamp()
    at.FromDatetime(datetime.datetime(2026, 10, 21, 14, 0, tzinfo=datetime.UTC))
    return work_pb2.JobLogEntry(
        at=at,
        attempt=attempt,
        queue='themis-work-interactive',
        task=task,
        retry_count=retry_count,
        step=step,
        inputs=inputs,
        event=event,
        outcome=outcome,
        model_answered=model_answered,
    )


def _varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def test_appending_an_encoded_log_joins_the_entries() -> None:
    first = work_pb2.JobLog(entries=[_entry(), _entry(event=work_pb2.JOB_EVENT_REFUSED)])
    second = work_pb2.JobLog(entries=[_entry(retry_count=1)])

    joined = work_pb2.JobLog.FromString(first.SerializeToString() + second.SerializeToString())

    assert list(joined.entries) == [*first.entries, *second.entries]


def test_a_newer_writers_field_survives_an_older_writers_append() -> None:
    # Field 99 of an entry stands for one a newer schema added: length-delimited, value b'new'.
    newer_entry = _entry().SerializeToString() + b'\x9a\x06\x03new'
    newer_log = b'\x0a' + _varint(len(newer_entry)) + newer_entry

    appended = newer_log + work_pb2.JobLog(entries=[_entry(retry_count=1)]).SerializeToString()

    entry = work_pb2.JobLog.FromString(appended).entries[0]
    assert entry.SerializeToString().endswith(b'\x9a\x06\x03new')


@pytest.mark.parametrize(
    'entry',
    [
        _entry(),
        _entry(event=work_pb2.JOB_EVENT_REFUSED),
        _entry(event=work_pb2.JOB_EVENT_FAILED, model_answered=True),
        _entry(step='xml', inputs=(), event=work_pb2.JOB_EVENT_SETTLED, outcome=work_pb2.SETTLED_OUTCOME_NOTHING),
    ],
)
def test_a_well_formed_entry_passes(entry: work_pb2.JobLogEntry) -> None:
    protovalidate.validate(entry)  # does not raise


@pytest.mark.parametrize(
    ('entry', 'rule'),
    [
        (_entry(event=work_pb2.JOB_EVENT_SETTLED), 'job_log_entry.outcome_iff_settled'),
        (_entry(outcome=work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE), 'job_log_entry.outcome_iff_settled'),
        (_entry(model_answered=True), 'job_log_entry.model_answered_only_on_failed'),
        (_entry(event=work_pb2.JOB_EVENT_UNSPECIFIED), 'enum.not_in'),
        (_entry(attempt='7c1e--main_text#0'), 'string.uuid'),
        (_entry(retry_count=None), 'required'),
        (_entry(step='Transcription'), 'string.pattern'),
        (_entry(inputs=('9f3a',)), 'string.pattern'),
        (_entry(inputs=(_PDF_HASH, _PDF_HASH)), 'repeated.unique'),
        (_entry(task=''), 'string.min_len'),
    ],
)
def test_a_malformed_entry_is_refused(entry: work_pb2.JobLogEntry, rule: str) -> None:
    with pytest.raises(protovalidate.ValidationError) as raised:
        protovalidate.validate(entry)
    assert rule in {v.proto.rule_id for v in raised.value.violations}


def test_a_well_formed_request_passes() -> None:
    protovalidate.validate(work_pb2.WorkRequest(doc_id=_DOC_ID, targets=['main_text'], run_id='run-20261021a'))


@pytest.mark.parametrize(
    ('request_', 'rule'),
    [
        (work_pb2.WorkRequest(doc_id='7c1e'), 'string.uuid'),
        (work_pb2.WorkRequest(doc_id=_DOC_ID, targets=['main_text', 'main_text']), 'repeated.unique'),
        (work_pb2.WorkRequest(doc_id=_DOC_ID, targets=['Main-Text']), 'string.pattern'),
        (work_pb2.WorkRequest(doc_id=_DOC_ID, run_id='Run 1'), 'string.pattern'),
        (work_pb2.WorkRequest(doc_id=_DOC_ID, run_id='a--g5'), 'string.pattern'),
        (work_pb2.WorkRequest(doc_id=_DOC_ID, targets=[f'step_{n}' for n in range(9)]), 'repeated.max_items'),
        (work_pb2.WorkRequest(doc_id=_DOC_ID, targets=['s' * 33]), 'string.max_len'),
    ],
)
def test_a_malformed_request_is_refused(request_: work_pb2.WorkRequest, rule: str) -> None:
    with pytest.raises(protovalidate.ValidationError) as raised:
        protovalidate.validate(request_)
    assert rule in {v.proto.rule_id for v in raised.value.violations}
