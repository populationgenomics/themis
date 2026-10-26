"""The task the enqueuer builds, and what it does with an already-taken name.

No Cloud Tasks call: the client is a fake, so what is under test is the task's shape — the name that
makes a duplicate a no-op and keeps different requests apart, the deadline that keeps a dispatch from
outliving the worker's timeout, and the OIDC identity that gets it past the worker's require-auth.
"""

from __future__ import annotations

import pathlib
import re
import typing

import protovalidate
import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import tasks_v2

from themis.litcache import enqueue, work
from themis.litcache.models import work_pb2

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_QUEUE = 'projects/cpg-themis-dev/locations/australia-southeast1/queues/themis-convert'
_WORKER = 'https://themis-convert-worker-abc123-ts.a.run.app'
_INVOKER = 'themis-convert-invoker@cpg-themis-dev.iam.gserviceaccount.com'
_DOC = '000006fa-e679-4f46-a052-8fb0e69f280c'

_TARGET = enqueue.ConversionTarget(queue_path=_QUEUE, worker_url=_WORKER, invoker_service_account_email=_INVOKER)
_MAIN_TEXT = work_pb2.WorkRequest(doc_id=_DOC, targets=[work.MAIN_TEXT_TARGET])


class _FakeClient:
    """A ``CloudTasksClient`` stand-in recording each create, optionally refusing the name."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.created: list[tuple[str, tasks_v2.Task]] = []
        self._raises = raises

    def create_task(self, *, parent: str, task: tasks_v2.Task) -> tasks_v2.Task:
        if self._raises is not None:
            raise self._raises
        self.created.append((parent, task))
        return task


def _enqueuer(client: _FakeClient) -> enqueue.Enqueuer:
    return enqueue.Enqueuer(typing.cast('tasks_v2.CloudTasksClient', client), _TARGET)


@pytest.mark.parametrize(
    ('request_', 'generation', 'sweep_pass', 'expected'),
    [
        pytest.param(work_pb2.WorkRequest(doc_id=_DOC), 5, None, f'{_DOC}--gen-5', id='a writer naming no target'),
        pytest.param(_MAIN_TEXT, None, None, f'{_DOC}--main_text', id='an agent'),
        pytest.param(_MAIN_TEXT, 5, None, f'{_DOC}--main_text--gen-5', id='a deposit'),
        pytest.param(
            work_pb2.WorkRequest(doc_id=_DOC), None, '20261021T1500', f'{_DOC}--sweep-20261021T1500', id='a sweep'
        ),
        pytest.param(
            work_pb2.WorkRequest(doc_id=_DOC, targets=['render_supplements', 'extract_variants']),
            None,
            None,
            f'{_DOC}--extract_variants--render_supplements',
            id='two targets, sorted',
        ),
        pytest.param(
            work_pb2.WorkRequest(doc_id=_DOC, targets=['transcription'], run_id='20261021a'),
            None,
            None,
            f'{_DOC}--transcription--run-20261021a',
            id='a funded run',
        ),
    ],
)
def test_a_task_is_named_for_what_it_asks(
    request_: work_pb2.WorkRequest, generation: int | None, sweep_pass: str | None, expected: str
) -> None:
    assert enqueue.task_id(request_, generation=generation, sweep_pass=sweep_pass) == expected


def test_requests_that_reach_different_work_have_different_names() -> None:
    # A request whose name is already held is dropped, which is harmless only when the holder reaches
    # everything the dropped one would have.
    names = {
        enqueue.task_id(work_pb2.WorkRequest(doc_id=_DOC), generation=1),
        enqueue.task_id(work_pb2.WorkRequest(doc_id=_DOC), generation=5),
        enqueue.task_id(_MAIN_TEXT),
        enqueue.task_id(_MAIN_TEXT, generation=5),
        enqueue.task_id(work_pb2.WorkRequest(doc_id=_DOC, targets=['main_text'], run_id='a')),
        enqueue.task_id(work_pb2.WorkRequest(doc_id=_DOC, targets=['main_text'], run_id='b')),
        enqueue.task_id(_MAIN_TEXT, sweep_pass='20261021T1500'),
        enqueue.task_id(_MAIN_TEXT, sweep_pass='20261021T1600'),
        # A target spelled like another part: targets hold no hyphen, so it cannot be read as one.
        enqueue.task_id(work_pb2.WorkRequest(doc_id=_DOC, targets=['gen5'])),
        enqueue.task_id(work_pb2.WorkRequest(doc_id=_DOC, targets=['gen5']), generation=5),
    }
    assert len(names) == 10


def test_a_request_naming_no_target_needs_a_generation_or_a_pass() -> None:
    # Named by doc_id alone, it would be held for a day against every later untargeted request.
    with pytest.raises(ValueError, match='generation or a sweep pass'):
        enqueue.task_id(work_pb2.WorkRequest(doc_id=_DOC))


def test_a_malformed_sweep_pass_is_refused() -> None:
    with pytest.raises(ValueError, match='sweep pass'):
        enqueue.task_id(_MAIN_TEXT, sweep_pass='15:00')


def test_a_malformed_request_is_refused_before_any_create() -> None:
    client = _FakeClient()
    with pytest.raises(protovalidate.ValidationError):
        _enqueuer(client).enqueue(work_pb2.WorkRequest(doc_id=_DOC, targets=['Main Text']))
    assert not client.created


def test_the_task_posts_the_request_to_the_workers_convert_endpoint() -> None:
    request = enqueue.work_task(_TARGET, _MAIN_TEXT).http_request
    assert request.url == f'{_WORKER}/convert'
    assert request.http_method == tasks_v2.HttpMethod.POST
    assert request.headers['Content-Type'] == work.WORK_REQUEST_MEDIA_TYPE
    assert work_pb2.WorkRequest.FromString(request.body) == _MAIN_TEXT


def test_a_worker_url_with_a_trailing_slash_still_addresses_convert() -> None:
    # `//convert` is a 404 Cloud Run answers at dispatch, so it would cost the paper its whole retry
    # budget before anyone saw the extra slash.
    target = enqueue.ConversionTarget(
        queue_path=_QUEUE, worker_url=f'{_WORKER}/', invoker_service_account_email=_INVOKER
    )
    request = enqueue.work_task(target, _MAIN_TEXT).http_request
    assert request.url == f'{_WORKER}/convert'
    assert request.oidc_token.audience == _WORKER


def test_the_task_carries_the_invokers_oidc_token_for_the_worker_url() -> None:
    # Require-auth on the worker admits this identity alone, and Cloud Run validates the audience
    # against the service URL rather than the request path.
    token = enqueue.work_task(_TARGET, _MAIN_TEXT).http_request.oidc_token
    assert token.service_account_email == _INVOKER
    assert token.audience == _WORKER


def test_the_dispatch_deadline_matches_the_workers_request_timeout() -> None:
    # The two sides of an agreement neither language nor deploy checks: a shorter deadline abandons a
    # conversion the worker is still running and dispatches a second attempt beside it, and a longer
    # one is refused by Cloud Tasks. The infra program is read rather than imported — it needs pulumi,
    # which the test group deliberately does not carry.
    source = (_REPO_ROOT / 'infra' / 'themis_infra' / 'convert.py').read_text('utf-8')
    match = re.search(r"^_REQUEST_TIMEOUT = '(\d+)s'$", source, re.MULTILINE)
    assert match, 'the convert worker no longer declares _REQUEST_TIMEOUT as a seconds literal'
    assert enqueue.work_task(_TARGET, _MAIN_TEXT).dispatch_deadline.seconds == int(match.group(1))


def test_a_create_places_the_task_on_the_named_queue() -> None:
    client = _FakeClient()
    assert _enqueuer(client).enqueue(_MAIN_TEXT) is True
    (parent, task), *rest = client.created
    assert not rest
    assert parent == _QUEUE
    assert task.name == f'{_QUEUE}/tasks/{_DOC}--main_text'


def test_an_already_taken_name_is_dedup_not_failure() -> None:
    # The paper either has a conversion coming or has just had one; either way the caller has nothing
    # to do about it, so this must not surface as an error.
    client = _FakeClient(raises=api_exceptions.AlreadyExists('task exists'))
    assert _enqueuer(client).enqueue(_MAIN_TEXT) is False


def test_a_dedup_says_so_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    # An enqueue that did not happen has to be visible rather than inferred from a paper that never
    # advances.
    client = _FakeClient(raises=api_exceptions.AlreadyExists('task exists'))
    with caplog.at_level('INFO', logger=enqueue.__name__):
        _enqueuer(client).enqueue(_MAIN_TEXT)
    assert _DOC in caplog.text


@pytest.mark.parametrize(
    'error',
    [
        api_exceptions.PermissionDenied('no cloudtasks.enqueuer'),
        api_exceptions.NotFound('no such queue'),
        api_exceptions.InvalidArgument('bad dispatch deadline'),
        api_exceptions.ServiceUnavailable('cloud tasks is down'),
        api_exceptions.ResourceExhausted('queue create rate'),
    ],
)
def test_every_other_failure_propagates(error: Exception) -> None:
    # Only the caller knows what a retry costs it, so nothing here decides on its behalf — and nothing
    # here reports a task that was never created as though it had been.
    client = _FakeClient(raises=error)
    with pytest.raises(type(error)):
        _enqueuer(client).enqueue(_MAIN_TEXT)
