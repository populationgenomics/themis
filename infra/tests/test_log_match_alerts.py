"""Log-match alert policies obey the shape the Monitoring API enforces at creation.

Checked here so a `pulumi up` in the deploy never learns it first: a policy whose condition matches log
entries is refused unless it rate-limits its notifications.
"""

from __future__ import annotations

from themis_infra import capture

_POLICY_TYPE = 'gcp:monitoring/alertPolicy:AlertPolicy'


def test_every_log_match_policy_rate_limits_its_notifications(program: capture.Capture) -> None:
    log_match = {
        urn: registration.state
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == _POLICY_TYPE
        and any('conditionMatchedLog' in condition for condition in registration.state['conditions'])
    }
    assert log_match, 'the program declares no log-match policy; the scan is checking nothing'
    for urn, state in log_match.items():
        assert state['alertStrategy']['notificationRateLimit']['period'], urn
