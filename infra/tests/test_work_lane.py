"""The work lane's queues and worker agree on the bounds each relies on the other to keep."""

from __future__ import annotations

import datetime

from themis.litcache import work
from themis_infra import capture

_QUEUE_TYPE = 'gcp:cloudtasks/queue:Queue'
_SERVICE_TYPE = 'gcp:cloudrunv2/service:Service'
_WORKER = 'themis-convert-worker'


def _of_type(program: capture.Capture, resource_type: str) -> dict[str, dict[str, object]]:
    return {
        str(registration.state['name']): registration.state
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == resource_type
    }


def _worker_template(program: capture.Capture) -> dict[str, object]:
    template = _of_type(program, _SERVICE_TYPE)[_WORKER]['template']
    assert isinstance(template, dict)
    return template


def test_the_worker_admits_only_its_own_projects_queues(program: capture.Capture) -> None:
    # Cloud Tasks counts as internal traffic only from the worker's own project and only on the default
    # run.app URL, so a queue elsewhere or a disabled default URL would leave every task undeliverable.
    worker = _of_type(program, _SERVICE_TYPE)[_WORKER]
    queues = _of_type(program, _QUEUE_TYPE)

    assert queues
    assert {q['project'] for q in queues.values()} == {worker['project']}
    assert not worker.get('defaultUriDisabled')
    assert worker['ingress'] == 'INGRESS_TRAFFIC_INTERNAL_ONLY'


def test_the_worker_has_an_instance_for_every_request_the_queues_dispatch_at_once(program: capture.Capture) -> None:
    # One request per instance: fewer instances than dispatches would let a full bulk queue take the
    # instance an interactive request needs. Every queue in the program delivers to this worker.
    queues = _of_type(program, _QUEUE_TYPE)
    dispatches = sum(int(q['rateLimits']['maxConcurrentDispatches']) for q in queues.values())  # type: ignore[index]
    template = _worker_template(program)

    assert dispatches
    assert template['maxInstanceRequestConcurrency'] == 1
    assert template['scaling']['maxInstanceCount'] == dispatches  # type: ignore[index]


def test_the_worker_runs_a_request_as_long_as_a_task_may_take(program: capture.Capture) -> None:
    # The job log reads a begun attempt older than the dispatch deadline as killed; a longer timeout would
    # let a live one be read as dead, and a shorter one would abandon it while the queue waits on it.
    timeout = str(_worker_template(program)['timeout'])

    assert timeout.endswith('s')
    assert datetime.timedelta(seconds=int(timeout.removesuffix('s'))) == work.DISPATCH_DEADLINE
