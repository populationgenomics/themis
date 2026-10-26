"""The dispatcher's edge: Anthropic's range alone past Cloud Armor, on the webhook path alone.

That the dispatcher is reached only through the load balancer, behind a policy that denies by default, is the
rule every named exception to IAP is held to (`test_public_ingress.py`); these are the dispatcher's own terms.
"""

from __future__ import annotations

import pytest

from themis_infra import capture, sandbox, web

_URL_MAP_TYPE = 'gcp:compute/uRLMap:URLMap'
_DISPATCHER = 'themis-dispatcher'


def _dispatcher_policy(program: capture.Capture) -> capture.Registration:
    (backend,) = capture.backends_fronting(program, _DISPATCHER)
    policy = capture.security_policy(program, backend)
    assert policy is not None, 'the dispatcher backend has no Cloud Armor policy'
    return policy


def test_dispatcher_admits_only_anthropic_outbound_range(program: capture.Capture) -> None:
    rules = _dispatcher_policy(program).state['rules']
    # The default rule is left out: while the switch is off it allows, below the previewed catch-all.
    allowed = [
        rule for rule in rules if rule['action'] == 'allow' and rule['priority'] != sandbox.DEFAULT_RULE_PRIORITY
    ]
    assert [rule['match']['config']['srcIpRanges'] for rule in allowed] == [[sandbox.ANTHROPIC_OUTBOUND_RANGE]]
    # A previewed allow rule admits nothing on its own, leaving the outcome to the default rule.
    assert not any(rule['preview'] for rule in allowed)


def test_only_the_webhook_path_routes_to_the_dispatcher(program: capture.Capture) -> None:
    dispatcher_backend_ids = {backend.id for backend in capture.backends_fronting(program, _DISPATCHER)}
    assert dispatcher_backend_ids
    routed, defaults = [], []
    for url_map in capture.of_type(program, _URL_MAP_TYPE):
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
def test_enforcement_switch_previews_only_the_catch_all_deny(enforce: bool) -> None:
    rules = sandbox.dispatcher_policy_rules(enforce=enforce)
    previewed = [rule.priority for rule in rules if rule.preview]
    assert previewed == ([] if enforce else [sandbox.CATCH_ALL_PRIORITY])
    # Cloud Armor rejects a policy whose default rule previews.
    (default,) = [rule for rule in rules if rule.priority == sandbox.DEFAULT_RULE_PRIORITY]
    assert not default.preview
    assert str(default.action).startswith('deny') == enforce


@pytest.mark.parametrize('path', ['webhooks/anthropic', '/webhooks/*', '/*', '/webhooks/anthropic?x=1'])
def test_route_takes_only_an_exact_absolute_path(path: str) -> None:
    with pytest.raises(ValueError, match='exact path'):
        web.Route(path=path, backend_service='backend')
