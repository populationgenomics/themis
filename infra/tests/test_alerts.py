"""Every alert reaches a channel: operational incidents the alerts channel, spend incidents the spend channel.

Also: the operational policies watch the resources the program actually declares, and the database's connection
condition follows the stack's tier.
"""

from __future__ import annotations

import pytest

from themis_infra import alerts, capture, sql

_CHANNEL_TYPE = 'gcp:monitoring/notificationChannel:NotificationChannel'
_POLICY_TYPE = 'gcp:monitoring/alertPolicy:AlertPolicy'
_JOB_TYPE = 'gcp:cloudrunv2/job:Job'
_SPEND_COMPONENTS = frozenset({'themis:infra:CostAlerts', 'themis:infra:CostReport'})
# Pulumi's wire-protocol marker for a secret value.
_SECRET_SIGNATURE = '1b47061264138c4ac30d75fd1eb44270'  # noqa: S105 — a protocol marker, not a credential


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict), value
    return value


def _sequence(value: object) -> list[object]:
    assert isinstance(value, list), value
    return value


@pytest.fixture(scope='module')
def config() -> dict[str, str]:
    return capture.stack_config('dev')


def _channel(program: capture.Capture, component: str) -> dict[str, object]:
    [only] = [
        _mapping(registration.state)
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn) == [component, _CHANNEL_TYPE]
    ]
    return only


@pytest.fixture(scope='module')
def alerts_channel(program: capture.Capture) -> dict[str, object]:
    return _channel(program, 'themis:infra:OperationalAlerts')


@pytest.fixture(scope='module')
def spend_channel(program: capture.Capture) -> dict[str, object]:
    return _channel(program, 'themis:infra:CostAlerts')


@pytest.fixture(scope='module')
def policies(program: capture.Capture) -> dict[str, tuple[list[str], dict[str, object]]]:
    """Every alert policy in the program, by name, with the type chain it was declared under."""
    return {
        urn.rsplit('::', 1)[-1]: (capture.urn_type_chain(urn), _mapping(registration.state))
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == _POLICY_TYPE
    }


def test_the_alerts_channel_is_slack_to_the_stacks_alerts_channel(
    alerts_channel: dict[str, object], spend_channel: dict[str, object], config: dict[str, str]
) -> None:
    assert alerts_channel['type'] == 'slack'
    assert _mapping(alerts_channel['labels'])['channel_name'] == config['themis:alertsSlackChannel']
    assert config['themis:alertsSlackChannel'] != config['themis:slackChannel']
    assert _SECRET_SIGNATURE in _mapping(alerts_channel['sensitiveLabels']).values(), 'the bot token is not secret'
    assert alerts_channel['name'] != spend_channel['name']


def test_every_policy_notifies_its_concerns_channel(
    policies: dict[str, tuple[list[str], dict[str, object]]],
    alerts_channel: dict[str, object],
    spend_channel: dict[str, object],
) -> None:
    assert policies
    for name, (chain, policy) in policies.items():
        expected = spend_channel if _SPEND_COMPONENTS & set(chain) else alerts_channel
        assert policy['notificationChannels'] == [expected['name']], name
        assert str(_mapping(policy['documentation'])['content']), name


def test_every_job_pages_on_a_failed_execution(
    program: capture.Capture, policies: dict[str, tuple[list[str], dict[str, object]]]
) -> None:
    jobs = {
        str(_mapping(registration.state)['name'])
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == _JOB_TYPE
    }
    assert jobs, 'the program declares no Cloud Run Job'
    thresholds = [
        str(_mapping(_mapping(condition)['conditionThreshold'])['filter'])
        for _, policy in policies.values()
        for condition in _sequence(policy['conditions'])
        if 'conditionThreshold' in _mapping(condition)
    ]
    filters = ' '.join(
        f for f in thresholds if alerts.COMPLETED_EXECUTIONS in f and 'metric.labels.result="failed"' in f
    )
    for job in jobs:
        assert f'resource.labels.job_name="{job}"' in filters, job


def test_the_connection_condition_follows_the_stacks_tier(
    policies: dict[str, tuple[list[str], dict[str, object]]], config: dict[str, str]
) -> None:
    _, policy = policies['themis-sql-saturated']
    [connections] = [
        _mapping(_mapping(c)['conditionThreshold'])
        for c in _sequence(policy['conditions'])
        if 'num_backends' in str(_mapping(_mapping(c)['conditionThreshold'])['filter'])
    ]
    assert 0 < float(str(connections['thresholdValue'])) < sql.max_connections(config['themis:sqlTier'])


def test_the_certificate_check_reads_the_stacks_hostname(program: capture.Capture, config: dict[str, str]) -> None:
    [check] = [
        _mapping(registration.state)
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == 'gcp:monitoring/uptimeCheckConfig:UptimeCheckConfig'
    ]
    assert _mapping(_mapping(check['monitoredResource'])['labels'])['host'] == config['themis:domain']
    assert _mapping(check['httpCheck'])['validateSsl'] is True


def test_the_certificate_alert_also_fires_when_the_check_cannot_read_it(
    policies: dict[str, tuple[list[str], dict[str, object]]],
) -> None:
    # When the hostname stops reaching the load balancer, the expiry metric has no points for this certificate, so
    # the check failing has to page as well.
    _, policy = policies['themis-certificate-expiring']
    filters = [
        str(_mapping(_mapping(c)['conditionThreshold'])['filter'])
        for c in _sequence(policy['conditions'])
        if 'conditionThreshold' in _mapping(c)
    ]
    assert any('time_until_ssl_cert_expires' in f for f in filters)
    assert any('uptime_check/check_passed' in f for f in filters)
    assert policy['combiner'] == 'OR'


@pytest.mark.parametrize(
    ('tier', 'limit'),
    [('db-f1-micro', 25), ('db-g1-small', 50), ('db-custom-1-3840', 100), ('db-custom-2-7680', 400)],
)
def test_a_tiers_connection_limit_follows_its_memory(tier: str, limit: int) -> None:
    assert sql.max_connections(tier) == limit


@pytest.mark.parametrize('tier', ['db-n1-standard-1', 'db-perf-optimized-N-2', ''])
def test_a_tier_whose_memory_the_name_does_not_give_is_refused(tier: str) -> None:
    with pytest.raises(ValueError, match='connection limit'):
        sql.max_connections(tier)
