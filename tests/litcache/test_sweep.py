"""Tests for the sweep (`themis.litcache.sweep`): what it asks for each paper, and a pass over the store.

The pass runs against a fake-gcs-server bucket (Docker-gated via the shared `gcs_bucket` fixture), with the Cloud
Tasks client faked so the requests it makes can be read back.
"""

from __future__ import annotations

import datetime
import hashlib
import typing

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from google.cloud import tasks_v2

from themis.litcache import enqueue, joblog, outcome, produce, sweep, work, writer
from themis.litcache.models import litcache_pb2, work_pb2

_NOW = datetime.datetime(2026, 10, 21, 15, 0, tzinfo=datetime.UTC)
_DEADLINE = datetime.timedelta(minutes=30)
_PDF = b'%PDF-1.7 seed'
_PDF_HASH = hashlib.sha256(_PDF).hexdigest()
_TARGET = enqueue.ConversionTarget(
    queue_path='projects/p/locations/l/queues/themis-convert-bulk',
    worker_url='https://worker.example',
    invoker_service_account_email='invoker@p.iam.gserviceaccount.com',
)
_ATTEMPT = joblog.Attempt(id='00000000-0000-4000-8000-000000000001', queue='q', task='t', retry_count=0)


def _doc_id(n: int) -> str:
    return f'9f3a0000-0000-4000-8000-{n:012d}'


def _manifest(n: int, *, pdf: bool = True, text: bool = False) -> litcache_pb2.Manifest:
    manifest = litcache_pb2.Manifest(doc_id=_doc_id(n), external_ids=litcache_pb2.ExternalIds(doi=f'10.1/{n}'))
    if pdf:
        manifest.sources.add(
            handle='pdf',
            media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
            revisions=[litcache_pb2.Revision(hash=_PDF_HASH)],
        )
    if text:
        key = hashlib.sha256(b'# docling').hexdigest()
        manifest.records[key].rendering.converter = litcache_pb2.Converter.CONVERTER_DOCLING
        manifest.records[key].deps.append(_PDF_HASH)
        manifest.refs['markdown'] = key
    return manifest


def _settled(step: str, inputs: tuple[str, ...] = ()) -> work_pb2.JobLogEntry:
    return _ATTEMPT.entry(
        at=_NOW, step=step, inputs=inputs, event=work_pb2.JOB_EVENT_SETTLED, outcome=work_pb2.SETTLED_OUTCOME_NOTHING
    )


def _request(manifest: litcache_pb2.Manifest, *entries: work_pb2.JobLogEntry) -> work_pb2.WorkRequest | None:
    return sweep.request_for(manifest, work_pb2.JobLog(entries=entries), now=_NOW, deadline=_DEADLINE)


def test_a_paper_with_text_and_free_work_is_asked_for_with_no_target() -> None:
    assert _request(_manifest(1, text=True)) == work_pb2.WorkRequest(doc_id=_doc_id(1))


def test_a_paper_with_text_and_nothing_left_is_not_asked_for() -> None:
    assert _request(_manifest(1, text=True), _settled(produce.XML_STEP)) is None


def test_a_paper_without_text_whose_pdf_awaits_transcription_is_asked_for_its_main_text() -> None:
    request = _request(_manifest(1), _settled(produce.XML_STEP))

    assert request == work_pb2.WorkRequest(doc_id=_doc_id(1), targets=[work.MAIN_TEXT_TARGET])


def test_a_paper_without_text_whose_pdf_is_still_to_be_located_is_asked_for_its_main_text() -> None:
    request = _request(_manifest(1, pdf=False), _settled(produce.XML_STEP))

    assert request == work_pb2.WorkRequest(doc_id=_doc_id(1), targets=[work.MAIN_TEXT_TARGET])


def test_a_paper_whose_routes_have_all_settled_is_not_asked_for() -> None:
    manifest = _manifest(1)
    settled = (_settled(produce.XML_STEP), _settled(produce.TRANSCRIPTION_STEP, (_PDF_HASH,)))

    assert _request(manifest, *settled) is None


def test_a_transcription_running_elsewhere_is_left_to_it() -> None:
    begun = _ATTEMPT.entry(
        at=_NOW - datetime.timedelta(minutes=5),
        step=produce.TRANSCRIPTION_STEP,
        inputs=(_PDF_HASH,),
        event=work_pb2.JOB_EVENT_BEGUN,
    )

    assert _request(_manifest(1), _settled(produce.XML_STEP), begun) is None


def test_a_transcription_refused_at_the_spend_limit_is_asked_for_again() -> None:
    refused = [
        _ATTEMPT.entry(at=_NOW, step=produce.TRANSCRIPTION_STEP, inputs=(_PDF_HASH,), event=event)
        for event in (work_pb2.JOB_EVENT_BEGUN, work_pb2.JOB_EVENT_REFUSED)
    ]

    request = _request(_manifest(1), _settled(produce.XML_STEP), *refused)

    assert request == work_pb2.WorkRequest(doc_id=_doc_id(1), targets=[work.MAIN_TEXT_TARGET])


def test_a_pass_is_named_for_the_minute_it_started_in_utc() -> None:
    later = datetime.datetime(2026, 10, 22, 2, 0, 59, tzinfo=datetime.timezone(datetime.timedelta(hours=11)))

    assert sweep.sweep_pass(later) == '20261021T1500'


class _TasksClient:
    def __init__(self, *, failing: Exception | None = None) -> None:
        self.tasks: list[tasks_v2.Task] = []
        self._failing = failing

    def create_task(self, *, parent: str, task: tasks_v2.Task) -> tasks_v2.Task:  # noqa: ARG002 — the client's signature
        if self._failing is not None:
            raise self._failing
        if any(t.name == task.name for t in self.tasks):
            raise api_exceptions.AlreadyExists(task.name)
        self.tasks.append(task)
        return task


def _commit(bucket: gcs.Bucket, manifest: litcache_pb2.Manifest) -> None:
    bucket.blob(writer.manifest_path(manifest.doc_id)).upload_from_string(manifest.SerializeToString())


def _sweep(bucket: gcs.Bucket, client: _TasksClient) -> sweep.SweepReport:
    enqueuer = enqueue.Enqueuer(typing.cast('tasks_v2.CloudTasksClient', client), _TARGET)
    return sweep.sweep(bucket, enqueuer, now=_NOW, deadline=_DEADLINE)


def test_a_pass_asks_for_each_paper_with_work_under_its_own_name(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _manifest(1, text=True))  # free work: the XML step
    _commit(gcs_bucket, _manifest(2))  # its main text
    _commit(gcs_bucket, _manifest(3, text=True))
    joblog.append(gcs_bucket, _doc_id(3), [_settled(produce.XML_STEP)])  # nothing left
    gcs_bucket.blob(f'papers/{_doc_id(4)}/renderings/x.md').upload_from_string(b'#')  # no manifest: not a paper
    client = _TasksClient()

    report = _sweep(gcs_bucket, client)

    assert report == sweep.SweepReport(papers=3, asked=2)
    assert sorted(t.name.rsplit('/', 1)[1] for t in client.tasks) == [
        f'{_doc_id(1)}--sweep-20261021T1500',
        f'{_doc_id(2)}--main_text--sweep-20261021T1500',
    ]


def test_a_pass_run_twice_asks_once(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _manifest(1))
    client = _TasksClient()

    _sweep(gcs_bucket, client)
    again = _sweep(gcs_bucket, client)

    assert again == sweep.SweepReport(papers=1, already_asked=1)
    assert len(client.tasks) == 1


def test_a_request_that_cannot_be_made_is_counted_and_the_pass_goes_on(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _manifest(1))
    _commit(gcs_bucket, _manifest(2))
    client = _TasksClient(failing=api_exceptions.ServiceUnavailable('queue quota'))

    assert _sweep(gcs_bucket, client) == sweep.SweepReport(papers=2, not_asked=2)


def test_a_refusal_every_request_would_meet_stops_the_pass(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _manifest(1))

    with pytest.raises(api_exceptions.PermissionDenied):
        _sweep(gcs_bucket, _TasksClient(failing=api_exceptions.PermissionDenied('no enqueue grant')))


def test_a_paper_that_cannot_be_read_is_passed_over(gcs_bucket: gcs.Bucket) -> None:
    gcs_bucket.blob(writer.manifest_path(_doc_id(1))).upload_from_string(_manifest(2).SerializeToString())
    gcs_bucket.blob(writer.manifest_path(_doc_id(3))).upload_from_string(b'\xff\xff\xff')
    _commit(gcs_bucket, _manifest(4))
    client = _TasksClient()

    report = _sweep(gcs_bucket, client)

    assert report == sweep.SweepReport(papers=3, asked=1, unreadable=2)
    assert [t.name.rsplit('/', 1)[1].split('--')[0] for t in client.tasks] == [_doc_id(4)]


def test_a_paper_whose_marker_was_never_written_is_asked_for(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _manifest(1, pdf=False))
    joblog.append(gcs_bucket, _doc_id(1), [_settled(produce.XML_STEP), _settled(produce.LOCATE_STEP)])
    _commit(gcs_bucket, _manifest(2, pdf=False))
    joblog.append(gcs_bucket, _doc_id(2), [_settled(produce.XML_STEP), _settled(produce.LOCATE_STEP)])
    outcome.write_outcome(gcs_bucket, _doc_id(2), outcome.FetchOutcome(kind=outcome.OutcomeKind.NO_FULL_TEXT, at=_NOW))
    client = _TasksClient()

    assert _sweep(gcs_bucket, client) == sweep.SweepReport(papers=2, asked=1)
    assert client.tasks[0].name.endswith(f'/{_doc_id(1)}--sweep-20261021T1500')


def test_a_pdf_deposited_for_a_paper_written_off_is_asked_for_its_main_text() -> None:
    # The old PDF's transcription settled; the deposited one has no entries, so its route has work again.
    deposited = hashlib.sha256(b'%PDF-1.7 deposited').hexdigest()
    manifest = _manifest(1)
    manifest.sources[0].revisions.add(hash=deposited, captured_at={'seconds': 1})
    settled = (_settled(produce.XML_STEP), _settled(produce.TRANSCRIPTION_STEP, (_PDF_HASH,)))

    request = _request(manifest, *settled)

    assert request == work_pb2.WorkRequest(doc_id=_doc_id(1), targets=[work.MAIN_TEXT_TARGET])
