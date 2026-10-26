"""An unauthenticated Cloud Run service is reachable only through the load balancer, behind a deny-by-default policy."""

from __future__ import annotations

import pytest
from pulumi.runtime import mocks

from themis_infra import capture, sandbox, web

_SERVICE_TYPE = 'gcp:cloudrunv2/service:Service'
_NEG_TYPE = 'gcp:compute/regionNetworkEndpointGroup:RegionNetworkEndpointGroup'
_BACKEND_TYPE = 'gcp:compute/backendService:BackendService'
_POLICY_TYPE = 'gcp:compute/securityPolicy:SecurityPolicy'
_URL_MAP_TYPE = 'gcp:compute/uRLMap:URLMap'
_DISPATCHER = 'themis-dispatcher'


def _of_type(program: capture.Capture, type_: str) -> list[mocks.MockMonitor.ResourceRegistration]:
    return [registration for urn, registration in program.resources.items() if capture.urn_type_chain(urn)[-1] == type_]


def _public_services(program: capture.Capture) -> set[str]:
    public = {
        binding.target
        for binding in program.bindings
        if binding.member == 'allUsers' and binding.role == 'roles/run.invoker'
    }
    assert public, 'no allUsers invoker captured'
    return public


def _backends_of(program: capture.Capture, service: str) -> list[mocks.MockMonitor.ResourceRegistration]:
    """The backend services whose serverless NEG points at the Cloud Run service."""
    negs = {neg.id for neg in _of_type(program, _NEG_TYPE) if neg.state.get('cloudRun', {}).get('service') == service}
    return [
        backend
        for backend in _of_type(program, _BACKEND_TYPE)
        if any(entry['group'] in negs for entry in backend.state['backends'])
    ]


def _policy_of(
    program: capture.Capture, backend: mocks.MockMonitor.ResourceRegistration
) -> mocks.MockMonitor.ResourceRegistration:
    assert 'securityPolicy' in backend.state, f'backend {backend.state["name"]} has no security policy'
    (policy,) = [
        policy
        for policy in _of_type(program, _POLICY_TYPE)
        if policy.state['self_link'] == backend.state['securityPolicy']
    ]
    return policy


def test_public_services_take_traffic_only_from_the_load_balancer(program: capture.Capture) -> None:
    # A run.app URL open to the internet would bypass the Cloud Armor policy on the backend service.
    ingress = {str(service.state['name']): service.state['ingress'] for service in _of_type(program, _SERVICE_TYPE)}
    exposed = sorted(
        name for name in _public_services(program) if ingress[name] != 'INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER'
    )
    assert not exposed, f'public services reachable past the load balancer: {exposed}'


def test_public_service_backends_end_in_a_deny_rule(program: capture.Capture) -> None:
    # Whether the rule takes effect is the enforcement switch, tested on its own below.
    for service in _public_services(program):
        backends = _backends_of(program, service)
        assert backends, f'{service}: no load-balancer backend'
        for backend in backends:
            rules = _policy_of(program, backend).state['rules']
            (default,) = [rule for rule in rules if rule['priority'] == sandbox.DEFAULT_RULE_PRIORITY]
            assert default['match']['config']['srcIpRanges'] == ['*']
            assert str(default['action']).startswith('deny'), f'{service}: default rule is {default["action"]}'


def test_dispatcher_admits_only_anthropic_outbound_range(program: capture.Capture) -> None:
    (backend,) = _backends_of(program, _DISPATCHER)
    rules = _policy_of(program, backend).state['rules']
    allowed = [rule for rule in rules if rule['action'] == 'allow']
    assert [rule['match']['config']['srcIpRanges'] for rule in allowed] == [[sandbox.ANTHROPIC_OUTBOUND_RANGE]]
    # A previewed allow rule admits nothing on its own, leaving the outcome to the default rule.
    assert not any(rule['preview'] for rule in allowed)


def test_only_the_webhook_path_routes_to_the_dispatcher(program: capture.Capture) -> None:
    dispatcher_backend_ids = {backend.id for backend in _backends_of(program, _DISPATCHER)}
    assert dispatcher_backend_ids
    routed, defaults = [], []
    for url_map in _of_type(program, _URL_MAP_TYPE):
        defaults.append(url_map.state.get('defaultService'))
        for matcher in url_map.state.get('pathMatchers', []):
            defaults.append(matcher['defaultService'])
            routed += [
                path
                for rule in matcher['pathRules']
                if rule['service'] in dispatcher_backend_ids
                for path in rule['paths']
            ]
    assert routed == [sandbox.DISPATCHER_WEBHOOK_PATH]
    assert not dispatcher_backend_ids & set(defaults), 'the dispatcher is a default service'


@pytest.mark.parametrize('enforce', [True, False])
def test_enforcement_switch_previews_only_the_deny_rule(enforce: bool) -> None:
    rules = sandbox.dispatcher_policy_rules(enforce=enforce)
    previewed = [rule.priority for rule in rules if rule.preview]
    assert previewed == ([] if enforce else [sandbox.DEFAULT_RULE_PRIORITY])


@pytest.mark.parametrize('path', ['webhooks/anthropic', '/webhooks/*', '/*', '/webhooks/anthropic?x=1'])
def test_route_takes_only_an_exact_absolute_path(path: str) -> None:
    with pytest.raises(ValueError, match='exact path'):
        web.Route(path=path, backend_service='backend')
