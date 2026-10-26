"""Every caller granted invoke on a service that takes only internal traffic can reach it.

Internal ingress, and internal-and-load-balancer ingress, admit a request from a caller only if it arrives over a
same-project VPC or from a Google event source such as Cloud Tasks. A `run.app` URL resolves to a public address,
so a Cloud Run workload reaches the service only with Direct VPC egress on the services network for all traffic.
An invoker grant to any other caller is a call Cloud Run refuses before IAM. Two invokers arrive another way, through
the load balancer, and only where the ingress admits it: IAP's service agent, and the public invoker of a named
exception, the dispatcher, which `test_dispatcher_edge.py` holds to its own controls.
"""

from __future__ import annotations

from themis_infra import capture

_NETWORK_TYPE = 'gcp:compute/network:Network'
_SERVICES_NETWORK_TYPE = 'themis:infra:ServicesNetwork'
_PROJECT_BINDING_TYPE = 'gcp:projects/iAMMember:IAMMember'
_LOAD_BALANCER = 'INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER'
_INTERNAL = frozenset({'INGRESS_TRAFFIC_INTERNAL_ONLY', _LOAD_BALANCER})
_INVOKER = 'roles/run.invoker'
_IAP_AGENT_DOMAIN = '@gcp-sa-iap.iam.gserviceaccount.com'
_PUBLIC_MEMBERS = frozenset({'allUsers', 'allAuthenticatedUsers'})
_AUTH = 'themis-auth'


def _services_network_id(program: capture.Capture) -> str:
    ids = [
        registration.id
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn) == [_SERVICES_NETWORK_TYPE, _NETWORK_TYPE]
    ]
    assert len(ids) == 1, f'expected one services network, found {ids}'
    return ids[0]


def _services_with_ingress(program: capture.Capture, ingress: frozenset[str]) -> set[str]:
    return {w.name for w in program.workloads if w.kind == 'service' and w.ingress in ingress}


def _task_identities(program: capture.Capture) -> set[str]:
    """The accounts a Cloud Tasks task calls as: those a queue's enqueuer may act as."""
    enqueuers = {b.member for b in program.bindings if b.capability == 'TaskEnqueuer'}
    return {
        f'serviceAccount:{b.target.rpartition("/")[2]}'
        for b in program.bindings
        if b.capability == 'AccountUser' and b.member in enqueuers
    }


def _through_the_load_balancer(member: str) -> bool:
    return member.endswith(_IAP_AGENT_DOMAIN) or member in _PUBLIC_MEMBERS


def test_every_invoker_of_an_internal_service_is_on_the_services_network(program: capture.Capture) -> None:
    internal = _services_with_ingress(program, _INTERNAL)
    behind_load_balancer = _services_with_ingress(program, frozenset({_LOAD_BALANCER}))
    network = _services_network_id(program)
    attached = {f'serviceAccount:{w.service_account}' for w in program.workloads if network in w.all_traffic_networks}
    tasks = _task_identities(program)
    # A project-level invoker reaches every service, the internal ones included.
    invokers = [
        (b.target if b.type_ != _PROJECT_BINDING_TYPE else 'every service', b.member)
        for b in program.bindings
        if b.role == _INVOKER and (b.target in internal or b.type_ == _PROJECT_BINDING_TYPE)
    ]
    assert invokers, 'no invoker grant on an internal service; the check is vacuous'
    unreachable = sorted(
        {
            (target, member)
            for target, member in invokers
            if member not in attached | tasks
            and not (target in behind_load_balancer and _through_the_load_balancer(member))
        }
    )
    assert not unreachable, f'invokers of internal services with no all-traffic egress on its VPC: {unreachable}'


def test_auth_is_internal_only(program: capture.Capture) -> None:
    services = {w.name: w for w in program.workloads if w.kind == 'service'}
    assert _AUTH in services, f'no {_AUTH!r} service in the program'
    assert services[_AUTH].ingress == 'INGRESS_TRAFFIC_INTERNAL_ONLY', f'{_AUTH} is not internal-only'
