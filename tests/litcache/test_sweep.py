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


def _sweep(
    bucket: gcs.Bucket,
    client: _TasksClient,
    *,
    now: datetime.datetime = _NOW,
    free_only: bool = False,
) -> sweep.SweepReport:
    enqueuer = enqueue.Enqueuer(typing.cast('tasks_v2.CloudTasksClient', client), _TARGET)
    return sweep.sweep(bucket, enqueuer, now=now, deadline=_DEADLINE, free_only=free_only)


def _requests(client: _TasksClient) -> dict[str, list[str]]:
    parsed = [work_pb2.WorkRequest.FromString(t.http_request.body) for t in client.tasks]
    return {r.doc_id: list(r.targets) for r in parsed}


def _commit_each_kind_of_paper(bucket: gcs.Bucket) -> None:
    _commit(bucket, _manifest(1, text=True))  # free work: the XML step
    _commit(bucket, _manifest(2))  # its main text, with free work too
    _commit(bucket, _manifest(3))
    joblog.append(bucket, _doc_id(3), [_settled(produce.XML_STEP)])  # its main text, and no free work
    _commit(bucket, _manifest(4, text=True))
    joblog.append(bucket, _doc_id(4), [_settled(produce.XML_STEP)])  # nothing left
    _commit(bucket, _manifest(5, pdf=False))
    joblog.append(bucket, _doc_id(5), [_settled(produce.XML_STEP), _settled(produce.LOCATE_STEP)])  # its marker
    bucket.blob(writer.manifest_path(_doc_id(6))).upload_from_string(b'\xff\xff\xff')  # unreadable


def _task_names(client: _TasksClient) -> list[str]:
    return sorted(t.name.rsplit('/', 1)[1] for t in client.tasks)


def test_a_pass_asks_for_each_paper_with_work_under_its_utc_minute(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _manifest(1, text=True))  # free work: the XML step
    _commit(gcs_bucket, _manifest(2))  # its main text
    _commit(gcs_bucket, _manifest(3, text=True))
    joblog.append(gcs_bucket, _doc_id(3), [_settled(produce.XML_STEP)])  # nothing left
    gcs_bucket.blob(f'papers/{_doc_id(4)}/renderings/x.md').upload_from_string(b'#')  # no manifest: not a paper
    client = _TasksClient()
    started = datetime.datetime(2026, 10, 22, 2, 0, 59, tzinfo=datetime.timezone(datetime.timedelta(hours=11)))

    report = _sweep(gcs_bucket, client, now=started)

    assert report == sweep.SweepReport(papers=3, asked=2)
    assert _task_names(client) == [
        f'{_doc_id(1)}--sweep-20261021T1500',
        f'{_doc_id(2)}--main_text--sweep-20261021T1500',
    ]
    assert _requests(client) == {_doc_id(1): [], _doc_id(2): [work.MAIN_TEXT_TARGET]}


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
    # Paper 3 holds a PDF whose XML and transcription routes settled, and its marker is written: nothing left to ask.
    _commit(gcs_bucket, _manifest(3))
    joblog.append(
        gcs_bucket, _doc_id(3), [_settled(produce.XML_STEP), _settled(produce.TRANSCRIPTION_STEP, (_PDF_HASH,))]
    )
    outcome.write_outcome(
        gcs_bucket, _doc_id(3), outcome.FetchOutcome(kind=outcome.OutcomeKind.FAILED, at=_NOW, error='refused')
    )
    client = _TasksClient()

    assert _sweep(gcs_bucket, client) == sweep.SweepReport(papers=3, asked=1)
    assert client.tasks[0].name.endswith(f'/{_doc_id(1)}--sweep-20261021T1500')


def test_a_revisionless_pdf_lineage_is_ignored_without_crashing(gcs_bucket: gcs.Bucket) -> None:
    manifest = _manifest(1, pdf=False)
    manifest.sources.add(handle='empty', media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF)
    _commit(gcs_bucket, manifest)
    joblog.append(gcs_bucket, _doc_id(1), [_settled(produce.XML_STEP), _settled(produce.LOCATE_STEP)])
    outcome.write_outcome(gcs_bucket, _doc_id(1), outcome.FetchOutcome(kind=outcome.OutcomeKind.NO_FULL_TEXT, at=_NOW))
    client = _TasksClient()

    assert _sweep(gcs_bucket, client) == sweep.SweepReport(papers=1, asked=0)


def test_a_free_only_pass_asks_no_paper_for_its_main_text(gcs_bucket: gcs.Bucket) -> None:
    _commit_each_kind_of_paper(gcs_bucket)
    client = _TasksClient()

    report = _sweep(gcs_bucket, client, free_only=True)

    assert report == sweep.SweepReport(papers=6, asked=3, unreadable=1)
    assert _task_names(client) == [f'{_doc_id(n)}--sweep-20261021T1500' for n in (1, 2, 5)]


@pytest.mark.parametrize(('free_only', 'main_text', 'free'), [(False, 2, 2), (True, 0, 3)])
def test_a_survey_counts_what_the_same_pass_asks(
    gcs_bucket: gcs.Bucket, free_only: bool, main_text: int, free: int
) -> None:
    _commit_each_kind_of_paper(gcs_bucket)
    client = _TasksClient()

    result = sweep.survey(gcs_bucket, now=_NOW, deadline=_DEADLINE, free_only=free_only)
    report = _sweep(gcs_bucket, client, free_only=free_only)

    assert (result.papers, result.unreadable, result.main_text, result.free) == (6, 1, main_text, free)
    assert (report.papers, report.unreadable, report.asked) == (6, 1, main_text + free)
    assert sum(work.MAIN_TEXT_TARGET in name for name in _task_names(client)) == main_text


def test_a_survey_counts_a_spending_step_only_on_papers_asked_for_their_main_text(gcs_bucket: gcs.Bucket) -> None:
    _commit_each_kind_of_paper(gcs_bucket)

    scheduled = sweep.survey(gcs_bucket, now=_NOW, deadline=_DEADLINE)
    free_only = sweep.survey(gcs_bucket, now=_NOW, deadline=_DEADLINE, free_only=True)

    assert scheduled.work[produce.TRANSCRIPTION_STEP] == 2  # papers 2 and 3; paper 1's text is met
    assert free_only.work[produce.TRANSCRIPTION_STEP] == 0
    assert free_only.work[produce.XML_STEP] == scheduled.work[produce.XML_STEP] == 2


@pytest.mark.parametrize(
    ('pdf', 'entries'),
    [
        (True, [_settled(produce.XML_STEP)]),
        (False, [_settled(produce.XML_STEP)]),
        (
            True,
            [
                _settled(produce.XML_STEP),
                _ATTEMPT.entry(
                    at=_NOW, step=produce.TRANSCRIPTION_STEP, inputs=(_PDF_HASH,), event=work_pb2.JOB_EVENT_BEGUN
                ),
                _ATTEMPT.entry(
                    at=_NOW, step=produce.TRANSCRIPTION_STEP, inputs=(_PDF_HASH,), event=work_pb2.JOB_EVENT_REFUSED
                ),
            ],
        ),
    ],
    ids=[
        'held-pdf-awaiting-transcription',
        'pdf-awaiting-locate',
        'transcription-refused-at-spend-limit',
    ],
)
def test_a_paper_whose_main_text_is_still_to_run_is_asked_for_it(
    gcs_bucket: gcs.Bucket, pdf: bool, entries: list[work_pb2.JobLogEntry]
) -> None:
    _commit(gcs_bucket, _manifest(1, pdf=pdf))
    joblog.append(gcs_bucket, _doc_id(1), entries)
    client = _TasksClient()

    assert _sweep(gcs_bucket, client) == sweep.SweepReport(papers=1, asked=1)
    assert _requests(client) == {_doc_id(1): [work.MAIN_TEXT_TARGET]}


def test_a_transcription_running_elsewhere_is_left_to_it(gcs_bucket: gcs.Bucket) -> None:
    _commit(gcs_bucket, _manifest(1))
    begun = _ATTEMPT.entry(
        at=_NOW - datetime.timedelta(minutes=5),
        step=produce.TRANSCRIPTION_STEP,
        inputs=(_PDF_HASH,),
        event=work_pb2.JOB_EVENT_BEGUN,
    )
    joblog.append(gcs_bucket, _doc_id(1), [_settled(produce.XML_STEP), begun])
    client = _TasksClient()

    assert _sweep(gcs_bucket, client) == sweep.SweepReport(papers=1, asked=0)
    assert client.tasks == []


def test_a_pdf_deposited_for_a_paper_written_off_is_asked_for_its_main_text(gcs_bucket: gcs.Bucket) -> None:
    # The old PDF's transcription settled and its marker was written; the deposited revision has work again.
    deposited = hashlib.sha256(b'%PDF-1.7 deposited').hexdigest()
    manifest = _manifest(1)
    manifest.sources[0].revisions.add(hash=deposited, captured_at={'seconds': 1})
    _commit(gcs_bucket, manifest)
    joblog.append(
        gcs_bucket, _doc_id(1), [_settled(produce.XML_STEP), _settled(produce.TRANSCRIPTION_STEP, (_PDF_HASH,))]
    )
    outcome.write_outcome(gcs_bucket, _doc_id(1), outcome.FetchOutcome(kind=outcome.OutcomeKind.NO_FULL_TEXT, at=_NOW))
    client = _TasksClient()

    assert _sweep(gcs_bucket, client) == sweep.SweepReport(papers=1, asked=1)
    assert _requests(client) == {_doc_id(1): [work.MAIN_TEXT_TARGET]}


def _set_main_env(monkeypatch: pytest.MonkeyPatch, bucket: gcs.Bucket) -> None:
    assert bucket.name is not None
    monkeypatch.setenv('THEMIS_FULLTEXT_BUCKET', bucket.name)
    monkeypatch.setenv('THEMIS_WORK_BULK_QUEUE', _TARGET.queue_path)
    monkeypatch.setenv('THEMIS_WORK_WORKER_URL', _TARGET.worker_url)
    monkeypatch.setenv('THEMIS_WORK_INVOKER_SA_EMAIL', _TARGET.invoker_service_account_email)


def test_main_enqueues_work_and_exits_zero_when_clean(gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch) -> None:
    _commit(gcs_bucket, _manifest(1, text=True))
    _set_main_env(monkeypatch, gcs_bucket)
    client = _TasksClient()

    exit_code = sweep.main(
        [],
        clock=lambda: _NOW,
        bucket_factory=gcs_bucket.client.bucket,
        tasks_client_factory=lambda: typing.cast('tasks_v2.CloudTasksClient', client),
    )

    assert exit_code == 0
    assert _requests(client) == {_doc_id(1): []}


def test_main_exits_one_when_a_request_could_not_be_created(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(gcs_bucket, _manifest(1, text=True))
    _set_main_env(monkeypatch, gcs_bucket)
    client = _TasksClient(failing=api_exceptions.ServiceUnavailable('queue quota'))

    exit_code = sweep.main(
        [],
        clock=lambda: _NOW,
        bucket_factory=gcs_bucket.client.bucket,
        tasks_client_factory=lambda: typing.cast('tasks_v2.CloudTasksClient', client),
    )

    assert exit_code == 1


def test_main_dry_run_exits_one_when_a_paper_is_unreadable(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    gcs_bucket.blob(writer.manifest_path(_doc_id(1))).upload_from_string(b'\xff\xff\xff')
    _set_main_env(monkeypatch, gcs_bucket)

    exit_code = sweep.main(['--dry-run'], clock=lambda: _NOW, bucket_factory=gcs_bucket.client.bucket)

    assert exit_code == 1
