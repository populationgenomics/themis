"""The conversion task the enqueuer builds, and what it does with an already-taken name.

No Cloud Tasks call: the client is a fake, so what is under test is the task's shape — the name that
makes a duplicate a no-op, the deadline that keeps a dispatch from outliving the worker's timeout, and
the OIDC identity that gets it past the worker's require-auth.
"""

from __future__ import annotations

import json
import pathlib
import re
import typing

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import tasks_v2

from themis.litcache import enqueue

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_QUEUE = 'projects/cpg-themis-dev/locations/australia-southeast1/queues/themis-convert'
_WORKER = 'https://themis-convert-worker-abc123-ts.a.run.app'
_INVOKER = 'themis-convert-invoker@cpg-themis-dev.iam.gserviceaccount.com'
_DOC = '000006fa-e679-4f46-a052-8fb0e69f280c'

_TARGET = enqueue.ConversionTarget(queue_path=_QUEUE, worker_url=_WORKER, invoker_service_account_email=_INVOKER)


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


def test_the_task_is_named_for_the_doc_id() -> None:
    # The whole dedup story rests on this name: a second request for the same paper has to collide
    # with the first rather than start a second conversion.
    assert enqueue.conversion_task(_TARGET, _DOC).name == f'{_QUEUE}/tasks/{_DOC}'


def test_the_task_posts_the_doc_id_to_the_workers_convert_endpoint() -> None:
    request = enqueue.conversion_task(_TARGET, _DOC).http_request
    assert request.url == f'{_WORKER}/convert'
    assert request.http_method == tasks_v2.HttpMethod.POST
    assert json.loads(request.body) == {'doc_id': _DOC}


def test_a_worker_url_with_a_trailing_slash_still_addresses_convert() -> None:
    # `//convert` is a 404 Cloud Run answers at dispatch, so it would cost the paper its whole retry
    # budget before anyone saw the extra slash.
    target = enqueue.ConversionTarget(
        queue_path=_QUEUE, worker_url=f'{_WORKER}/', invoker_service_account_email=_INVOKER
    )
    request = enqueue.conversion_task(target, _DOC).http_request
    assert request.url == f'{_WORKER}/convert'
    assert request.oidc_token.audience == _WORKER


def test_the_task_carries_the_invokers_oidc_token_for_the_worker_url() -> None:
    # Require-auth on the worker admits this identity alone, and Cloud Run validates the audience
    # against the service URL rather than the request path.
    token = enqueue.conversion_task(_TARGET, _DOC).http_request.oidc_token
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
    assert enqueue.conversion_task(_TARGET, _DOC).dispatch_deadline.seconds == int(match.group(1))


def test_a_create_places_the_task_on_the_named_queue() -> None:
    client = _FakeClient()
    assert _enqueuer(client).enqueue(_DOC) is True
    (parent, task), *rest = client.created
    assert not rest
    assert parent == _QUEUE
    assert task.name.endswith(_DOC)


def test_an_already_taken_name_is_dedup_not_failure() -> None:
    # The paper either has a conversion coming or has just had one; either way the caller has nothing
    # to do about it, so this must not surface as an error.
    client = _FakeClient(raises=api_exceptions.AlreadyExists('task exists'))
    assert _enqueuer(client).enqueue(_DOC) is False


def test_a_dedup_says_so_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    # An enqueue that did not happen has to be visible rather than inferred from a paper that never
    # advances.
    client = _FakeClient(raises=api_exceptions.AlreadyExists('task exists'))
    with caplog.at_level('INFO', logger=enqueue.__name__):
        _enqueuer(client).enqueue(_DOC)
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
        _enqueuer(client).enqueue(_DOC)
