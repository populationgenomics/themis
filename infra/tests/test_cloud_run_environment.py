"""Every Cloud Run service sets its execution environment, to the one `cloud_run` holds for all of them."""

from __future__ import annotations

from themis_infra import capture, cloud_run

_SERVICE_TYPE = 'gcp:cloudrunv2/service:Service'


def test_every_service_sets_the_shared_execution_environment(program: capture.Capture) -> None:
    environments = {
        str(service.state['name']): service.state['template'].get('executionEnvironment')
        for service in capture.of_type(program, _SERVICE_TYPE)
    }
    assert environments, 'no Cloud Run service captured'
    unset = sorted(name for name, environment in environments.items() if environment is None)
    assert not unset, f'services that leave their execution environment to Cloud Run: {unset}'
    other = {name: env for name, env in environments.items() if env != cloud_run.EXECUTION_ENVIRONMENT}
    assert not other, f'services not on {cloud_run.EXECUTION_ENVIRONMENT}: {other}'
