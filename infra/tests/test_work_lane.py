"""The work lane's queues and worker agree on the bounds each relies on the other to keep."""

from __future__ import annotations

import datetime
import typing

import pytest

from themis.litcache import work
from themis_infra import capture

_QUEUE_TYPE = 'gcp:cloudtasks/queue:Queue'
_SERVICE_TYPE = 'gcp:cloudrunv2/service:Service'
_SCHEDULER_TYPE = 'gcp:cloudscheduler/job:Job'
# Each queue and the worker service it delivers to.
_LANES = {'themis-convert': 'themis-convert-worker', 'themis-convert-bulk': 'themis-convert-bulk-worker'}
_SWEEP_SCHEDULE = 'themis-work-sweep-schedule'


def _of_type(program: capture.Capture, resource_type: str) -> dict[str, dict[str, object]]:
    return {
        str(registration.state['name']): registration.state
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == resource_type
    }


def _template(service: dict[str, object]) -> dict[str, typing.Any]:
    template = service['template']
    assert isinstance(template, dict)
    return template


def _env(template: dict[str, typing.Any], name: str) -> str:
    (container,) = template['containers']
    return next(env['value'] for env in container['envs'] if env['name'] == name)


def test_every_queue_has_a_worker_service_of_its_own(program: capture.Capture) -> None:
    queues = _of_type(program, _QUEUE_TYPE)
    services = _of_type(program, _SERVICE_TYPE)

    assert set(queues) == set(_LANES)
    assert set(_LANES.values()) <= set(services)
    assert len(set(_LANES.values())) == len(_LANES)


@pytest.mark.parametrize(('queue_name', 'service_name'), sorted(_LANES.items()))
def test_every_queue_can_reach_its_internal_only_worker(
    program: capture.Capture, queue_name: str, service_name: str
) -> None:
    # Cloud Tasks counts as internal traffic only from the worker's own project and only on the default
    # run.app URL, so a queue elsewhere or a disabled default URL would leave every task undeliverable.
    queue = _of_type(program, _QUEUE_TYPE)[queue_name]
    worker = _of_type(program, _SERVICE_TYPE)[service_name]

    assert queue['project'] == worker['project']
    assert not worker.get('defaultUriDisabled')
    assert worker['ingress'] == 'INGRESS_TRAFFIC_INTERNAL_ONLY'


@pytest.mark.parametrize(('queue_name', 'service_name'), sorted(_LANES.items()))
def test_a_worker_serves_every_request_its_queue_dispatches_at_once(
    program: capture.Capture, queue_name: str, service_name: str
) -> None:
    # Fewer request slots than dispatches would leave tasks waiting on an instance; more would be
    # capacity the queue never fills.
    dispatches = int(_of_type(program, _QUEUE_TYPE)[queue_name]['rateLimits']['maxConcurrentDispatches'])  # type: ignore[index]
    template = _template(_of_type(program, _SERVICE_TYPE)[service_name])

    assert dispatches
    assert template['scaling']['maxInstanceCount'] * template['maxInstanceRequestConcurrency'] == dispatches


@pytest.mark.parametrize('service_name', sorted(_LANES.values()))
def test_a_worker_sizes_its_request_pool_to_the_requests_cloud_run_sends_it(
    program: capture.Capture, service_name: str
) -> None:
    # A pool smaller than the per-instance limit would cap the instance below it without saying so.
    template = _template(_of_type(program, _SERVICE_TYPE)[service_name])

    assert int(_env(template, 'THEMIS_CONVERT_CONCURRENCY')) == template['maxInstanceRequestConcurrency']


def test_an_agent_s_paper_has_an_instance_to_itself(program: capture.Capture) -> None:
    # The interactive service never puts an agent's paper beside another request (evidence-fulltext.md).
    template = _template(_of_type(program, _SERVICE_TYPE)[_LANES['themis-convert']])

    assert template['maxInstanceRequestConcurrency'] == 1


@pytest.mark.parametrize('service_name', sorted(_LANES.values()))
def test_a_worker_runs_a_request_as_long_as_a_task_may_take(program: capture.Capture, service_name: str) -> None:
    # The job log reads a begun attempt older than the dispatch deadline as killed; a longer timeout would
    # let a live one be read as dead, and a shorter one would abandon it while the queue waits on it.
    timeout = str(_template(_of_type(program, _SERVICE_TYPE)[service_name])['timeout'])

    assert timeout.endswith('s')
    assert datetime.timedelta(seconds=int(timeout.removesuffix('s'))) == work.DISPATCH_DEADLINE


def _env_values(value: object, name: str) -> list[str]:
    """Every value an env entry named `name` takes anywhere in a resource's state."""
    found: list[str] = []
    if isinstance(value, dict):
        if value.get('name') == name and 'value' in value:
            found.append(str(value['value']))
        for item in value.values():
            found.extend(_env_values(item, name))
    elif isinstance(value, list):
        for item in value:
            found.extend(_env_values(item, name))
    return found


@pytest.mark.parametrize(
    ('env_name', 'queue_name'),
    [('THEMIS_WORK_WORKER_URL', 'themis-convert-bulk'), ('THEMIS_LITERATURE_CONVERT_WORKER_URL', 'themis-convert')],
)
def test_each_enqueuer_targets_the_service_its_queue_delivers_to(
    program: capture.Capture, env_name: str, queue_name: str
) -> None:
    # The sweep enqueues on the bulk queue and the evidence service on the interactive one; a task that
    # names the other queue's service lands on instances sized for the other kind of work.
    services = _of_type(program, _SERVICE_TYPE)
    named = {
        value for registration in program.resources.values() for value in _env_values(registration.state, env_name)
    }

    assert named == {services[_LANES[queue_name]]['uri']}


def test_the_sweep_runs_on_its_schedule_only_where_the_stack_says_so(program: capture.Capture) -> None:
    # A pass can ask for paid transcriptions across the corpus, so an unscheduled stack's schedule is paused.
    scheduled = capture.stack_config('dev')['themis:workSweepScheduled'] == 'true'

    assert _of_type(program, _SCHEDULER_TYPE)[_SWEEP_SCHEDULE]['paused'] is not scheduled
