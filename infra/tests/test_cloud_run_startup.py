"""Every Cloud Run service probes a new instance's start at least once a second."""

from __future__ import annotations

from collections.abc import Callable

import pulumi_gcp as gcp
import pytest

from themis_infra import capture, cloud_run

_SERVICE_TYPE = 'gcp:cloudrunv2/service:Service'
# A ready instance waits at most this long for its next probe; Cloud Run's shortest period.
_MAX_PROBE_PERIOD_SECONDS = 1
# What Cloud Run probes at when a probe names no period.
_CLOUD_RUN_DEFAULT_PERIOD_SECONDS = 10


def test_every_service_probes_its_start_each_second(program: capture.Capture) -> None:
    # A request that scales a service up from zero waits for the probe's next success, so a ready instance
    # that is probed less often sits idle with the request pending.
    containers = {
        f'{registration.state["name"]}/{index}': container
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == _SERVICE_TYPE
        for index, container in enumerate(registration.state['template']['containers'])
    }
    assert containers, 'no Cloud Run service captured'
    unprobed = sorted(name for name, container in containers.items() if 'startupProbe' not in container)
    assert not unprobed, f'containers without a startup probe: {unprobed}'
    slow = sorted(
        name
        for name, container in containers.items()
        if container['startupProbe'].get('periodSeconds', _CLOUD_RUN_DEFAULT_PERIOD_SECONDS) > _MAX_PROBE_PERIOD_SECONDS
    )
    assert not slow, f'containers probed less often than every {_MAX_PROBE_PERIOD_SECONDS} s: {slow}'


@pytest.mark.parametrize('budget_seconds', [0, -1, 601])
def test_startup_probe_rejects_a_budget_outside_cloud_runs_range(budget_seconds: int) -> None:
    with pytest.raises(ValueError, match='startup budget'):
        cloud_run.http_startup_probe('/healthz', budget_seconds=budget_seconds)
    with pytest.raises(ValueError, match='startup budget'):
        cloud_run.grpc_startup_probe(8080, budget_seconds=budget_seconds)


@pytest.mark.parametrize('budget_seconds', [1, 60, 600])
@pytest.mark.parametrize(
    'probe',
    [
        lambda budget: cloud_run.http_startup_probe('/healthz', budget_seconds=budget),
        lambda budget: cloud_run.grpc_startup_probe(8080, budget_seconds=budget),
    ],
    ids=['http', 'grpc'],
)
def test_startup_probe_spends_its_budget_on_attempts_within_the_period(
    probe: Callable[[int], gcp.cloudrunv2.ServiceTemplateContainerStartupProbeArgs], budget_seconds: int
) -> None:
    args = probe(budget_seconds)
    period, timeout, attempts = args.period_seconds, args.timeout_seconds, args.failure_threshold
    assert isinstance(period, int)
    assert isinstance(timeout, int)
    assert isinstance(attempts, int)
    assert attempts * period == budget_seconds
    assert timeout <= period
