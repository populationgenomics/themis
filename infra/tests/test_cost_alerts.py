"""The spend alerts read the spend metrics, notify the stack's Slack channel, and obey the API's shape constraints.

The one constraint the Monitoring API enforces at creation, checked here so a `pulumi up` in the deploy never learns
it first: a PromQL condition's evaluation interval is a multiple of 30 seconds.
"""

from __future__ import annotations

import re

import pytest

from themis_infra import capture, cost, cost_alerts, cost_metrics, cost_prices, cost_promql

_CHANNEL_TYPE = 'gcp:monitoring/notificationChannel:NotificationChannel'
_POLICY_TYPE = 'gcp:monitoring/alertPolicy:AlertPolicy'
_EXPORTER_JOB_CHAIN = ['themis:infra:CostExporter', 'gcp:cloudrunv2/job:Job']
_EXPORTER_SCHEDULE_CHAIN = ['themis:infra:CostExporter', 'gcp:cloudscheduler/job:Job']
_EVERY_N_MINUTES = re.compile(r'\*/(\d+) \* \* \* \*')
# Pulumi's wire-protocol marker for a secret value.
_SECRET_SIGNATURE = '1b47061264138c4ac30d75fd1eb44270'  # noqa: S105 — a protocol marker, not a credential
# A documentation variable: Monitoring renders labels, never the observed value, so the texts carry none.
_DOCUMENTATION_VARIABLE = re.compile(r'\$\{')


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict), value
    return value


def _sequence(value: object) -> list[object]:
    assert isinstance(value, list), value
    return value


def _of_type(program: capture.Capture, type_: str) -> dict[str, dict[str, object]]:
    """The registered resources of a type, by resource name."""
    return {
        urn.rsplit('::', 1)[-1]: _mapping(registration.state)
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == type_
    }


def _seconds(duration: str) -> int:
    assert duration.endswith('s'), duration
    return int(duration[:-1])


def _only_condition(policy: dict[str, object]) -> dict[str, object]:
    [condition] = [_mapping(c) for c in _sequence(policy['conditions'])]
    return condition


def _promql_condition(policy: dict[str, object]) -> dict[str, object]:
    return _mapping(_only_condition(policy)['conditionPrometheusQueryLanguage'])


@pytest.fixture(scope='module')
def config() -> dict[str, str]:
    return capture.dev_stack_config()


@pytest.fixture(scope='module')
def tick_seconds(program: capture.Capture) -> int:
    # The exporter's schedule, read off its Cloud Scheduler cron rather than the constant the alerts were handed, so
    # the alerts are held to what actually fires.
    [schedule] = [r for urn, r in program.resources.items() if capture.urn_type_chain(urn) == _EXPORTER_SCHEDULE_CHAIN]
    match = _EVERY_N_MINUTES.fullmatch(str(schedule.state['schedule']))
    assert match, schedule.state['schedule']
    return int(match.group(1)) * 60


@pytest.fixture(scope='module')
def channel(program: capture.Capture) -> dict[str, object]:
    [only] = _of_type(program, _CHANNEL_TYPE).values()
    return only


@pytest.fixture(scope='module')
def policies(program: capture.Capture) -> dict[str, dict[str, object]]:
    return _of_type(program, _POLICY_TYPE)


@pytest.fixture(scope='module')
def exporter_job_name(program: capture.Capture) -> str:
    [job] = [r for urn, r in program.resources.items() if capture.urn_type_chain(urn) == _EXPORTER_JOB_CHAIN]
    return str(job.state['name'])


def test_the_channel_is_slack_to_the_stacks_channel(channel: dict[str, object], config: dict[str, str]) -> None:
    assert channel['type'] == 'slack'
    # `labels` keys are ours as written; the SDK camel-cases argument names, not the mapping's keys.
    assert _mapping(channel['labels'])['channel_name'] == config['themis:slackChannel']
    # The token reaches the program as a secret and must stay one: the state form is Pulumi's secret wrapper,
    # its special-signature marker beside the value.
    sensitive = _mapping(channel['sensitiveLabels'])
    assert _SECRET_SIGNATURE in sensitive.values(), 'the bot token is not marked secret'
    assert 'authToken' in _mapping(sensitive['value'])


def test_every_policy_notifies_the_channel_and_links_the_dashboard(
    channel: dict[str, object], policies: dict[str, dict[str, object]]
) -> None:
    assert policies
    for name, policy in policies.items():
        assert policy['notificationChannels'] == [channel['name']], name
        assert policy['combiner'] == 'OR', name
        documentation = _mapping(policy['documentation'])
        assert documentation['content'], name
        [link] = [_mapping(link) for link in _sequence(documentation['links'])]
        assert 'monitoring/dashboards' in str(link['url']), name


def test_no_alert_text_carries_a_documentation_variable(policies: dict[str, dict[str, object]]) -> None:
    for name, policy in policies.items():
        documentation = _mapping(policy['documentation'])
        for field in ('subject', 'content'):
            assert not _DOCUMENTATION_VARIABLE.search(str(documentation[field])), (name, field)


def test_no_policy_renotifies_more_than_daily(policies: dict[str, dict[str, object]]) -> None:
    # Monitoring's floor is 30 minutes; a reminder per tick would be the spam the channel is meant to avoid.
    for name, policy in policies.items():
        strategy = _mapping(policy['alertStrategy'])
        for channel_strategy in _sequence(strategy.get('notificationChannelStrategies') or []):
            assert _seconds(str(_mapping(channel_strategy)['renotifyInterval'])) >= 24 * 3600, name
        assert 'OPENED' in _sequence(strategy['notificationPrompts']), name


def test_every_promql_condition_evaluates_at_the_tick_with_no_metric_validation(
    policies: dict[str, dict[str, object]], tick_seconds: int
) -> None:
    promql = [
        name for name, policy in policies.items() if 'conditionPrometheusQueryLanguage' in _only_condition(policy)
    ]
    assert promql
    for name in promql:
        condition = _promql_condition(policies[name])
        interval = _seconds(str(condition['evaluationInterval']))
        assert interval == tick_seconds, name
        assert interval % 30 == 0, name
        assert _seconds(str(condition['duration'])) % interval == 0, name
        assert condition['disableMetricValidation'] is True, name
        assert 'rate(' not in str(condition['query']), name


def test_the_spike_is_the_workspace_total_over_the_configured_window(
    policies: dict[str, dict[str, object]], config: dict[str, str]
) -> None:
    spike = policies['themis-cost-spike']
    query = str(_promql_condition(spike)['query'])
    window = f'{config["themis:costSpikeWindowMinutes"]}m'
    # The dashboard's own workspace figure over the configured window, against the configured threshold; every
    # window in it is that one.
    assert query == f'{cost_promql.workspace_cents(window)} > {config["themis:costSpikeAlertCents"]}'
    assert set(re.findall(r'\[(\w+)\]', query)) == {window}
    assert _promql_condition(spike)['duration'] == '0s'
    assert _sequence(_mapping(spike['alertStrategy'])['notificationPrompts']) == ['OPENED']


def test_the_freshness_condition_is_the_heartbeat_silent_for_the_configured_window(
    policies: dict[str, dict[str, object]], config: dict[str, str], exporter_job_name: str, tick_seconds: int
) -> None:
    # The heartbeat, not an agent's series: every successful run sets it, sessions in the workspace or none, so an
    # empty workspace under a healthy exporter is not silence.
    freshness = policies['themis-cost-exporter-freshness']
    condition = _promql_condition(freshness)
    window = f'[{config["themis:costFreshnessMinutes"]}m]'
    query = str(condition['query'])
    heartbeat = cost_promql.selector(cost_metrics.EXPORTER_LAST_SUCCESS_TIMESTAMP_SECONDS)
    assert query == f'absent_over_time({heartbeat}{window})'
    # A never-written metric is absent too: the condition holds one tick before firing, so a fresh environment's first
    # run lands inside it.
    assert _seconds(str(condition['duration'])) == tick_seconds
    strategy = _mapping(freshness['alertStrategy'])
    assert {'OPENED', 'CLOSED'} <= set(_sequence(strategy['notificationPrompts']))
    assert strategy['notificationChannelStrategies'], 'a silent exporter is re-notified, not forgotten'
    # A PromQL incident ignores auto-close; declaring one would promise a close that never comes.
    assert not strategy.get('autoClose')
    content = str(_mapping(freshness['documentation'])['content'])
    assert f'--job {exporter_job_name}' in content
    assert 'gcloud run jobs executions list' in content


def test_the_unpriced_condition_is_usage_on_a_model_outside_the_table(policies: dict[str, dict[str, object]]) -> None:
    unpriced = policies['themis-cost-unpriced-usage']
    query = str(_promql_condition(unpriced)['query'])
    assert cost_metrics.REQUEST_TOKENS in query
    # The table's models, and only those, are what the matcher excludes.
    assert cost_promql.not_among(cost_metrics.MODEL_LABEL, cost_prices.LIST_PRICES_CENTS_PER_MTOK) in query
    assert query.endswith(') > 0'), query
    assert _promql_condition(unpriced)['duration'] == '0s'
    assert _sequence(_mapping(unpriced['alertStrategy'])['notificationPrompts']) == ['OPENED']
    assert 'infra/themis_infra/cost_prices.py' in str(_mapping(unpriced['documentation'])['content'])


@pytest.mark.parametrize(
    ('window', 'freshness', 'match'),
    [
        (2 * cost.TICK_MINUTES - 1, 10 * cost.TICK_MINUTES, 'two samples'),
        (10 * cost.TICK_MINUTES, 2 * cost.TICK_MINUTES, 'two missed ticks'),
    ],
)
def test_tuning_the_conditions_cannot_evaluate_is_refused(window: int, freshness: int, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        cost_alerts.check_tuning(
            spike_window_minutes=window, freshness_minutes=freshness, tick_minutes=cost.TICK_MINUTES
        )
