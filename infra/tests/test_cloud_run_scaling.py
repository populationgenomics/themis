"""Every Cloud Run service bounds its own scale-out rather than leaving the cap to Cloud Run."""

from __future__ import annotations

import pytest

from themis_infra import capture, cloud_run

_SERVICE_TYPE = 'gcp:cloudrunv2/service:Service'


def test_every_service_declares_max_instance_count(program: capture.Capture) -> None:
    # Cloud Run fills in a default when the template omits it, and every later preview and `up` then
    # plans to remove it again, rolling the service each time.
    services = {
        str(registration.state['name']): registration.state['template']
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == _SERVICE_TYPE
    }
    assert services, 'no Cloud Run service captured'
    unbounded = sorted(name for name, template in services.items() if 'maxInstanceCount' not in template['scaling'])
    assert not unbounded, f'services without max_instance_count: {unbounded}'


def test_counts_within_bounds_are_accepted() -> None:
    cloud_run.check_instance_counts(min_key='min', min_instances=1, max_key='max', max_instances=1)
    cloud_run.check_instance_counts(max_key='max', max_instances=1)


@pytest.mark.parametrize(
    ('min_instances', 'max_instances', 'named'),
    [
        (-1, 3, 'themis:fooMinInstances'),
        (0, -1, 'themis:fooMaxInstances'),
        (0, 0, 'themis:fooMaxInstances'),
        (4, 3, 'themis:fooMinInstances'),
    ],
)
def test_counts_cloud_run_would_reject_are_refused(min_instances: int, max_instances: int, named: str) -> None:
    with pytest.raises(ValueError, match=named):
        cloud_run.check_instance_counts(
            min_key='themis:fooMinInstances',
            min_instances=min_instances,
            max_key='themis:fooMaxInstances',
            max_instances=max_instances,
        )
