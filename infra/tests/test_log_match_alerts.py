"""No alert policy matches log entries: every alert reads a metric (docs/design/infrastructure.md, Alerting).

A log-match condition reads text a service happens to log, which a change to the wording silently breaks; the
operational alerts read the metrics the services and the deploy write instead.
"""

from __future__ import annotations

from themis_infra import capture

_POLICY_TYPE = 'gcp:monitoring/alertPolicy:AlertPolicy'


def test_no_policy_matches_log_entries(program: capture.Capture) -> None:
    policies = {
        urn: registration.state
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == _POLICY_TYPE
    }
    assert policies, 'the program declares no alert policy; the scan is checking nothing'
    log_match = [
        urn
        for urn, state in policies.items()
        if any('conditionMatchedLog' in condition for condition in state['conditions'])
    ]
    assert log_match == []
