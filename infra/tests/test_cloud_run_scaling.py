"""Every Cloud Run service bounds its own scale-out rather than leaving the cap to Cloud Run."""

from __future__ import annotations

from themis_infra import capture

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
