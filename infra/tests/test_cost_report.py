"""The report Job reads the rendered queries, posts with the secret, runs on Sydney mornings, and pages when it fails.

The exact key set the rendering carries is the report's contract, held by the round trip in
`themis/services/cost_exporter/tests/test_report_queries.py`; here the rendering's shape and windows are checked.
"""

from __future__ import annotations

import json
import re

import pytest

from themis_infra import capture, cost_promql, cost_report

_JOB_CHAIN = ['themis:infra:CostReport', 'gcp:cloudrunv2/job:Job']
_SCHEDULE_CHAIN = ['themis:infra:CostReport', 'gcp:cloudscheduler/job:Job']
_POLICY_CHAIN = ['themis:infra:CostReport', 'gcp:monitoring/alertPolicy:AlertPolicy']
_CHANNEL_CHAIN = ['themis:infra:CostAlerts', 'gcp:monitoring/notificationChannel:NotificationChannel']
# A metric name as it can appear in a query: bare, or quoted inside the braces.
_LEGACY_NAME = re.compile(r'\bthemis_[A-Za-z0-9_]+')
_QUOTED_NAME = re.compile(r'\{"([^"]+)"')
# The cents-to-dollars division, as distinct from the per-million-tokens one.
_CENTS_TO_DOLLARS = re.compile(r'/ 100(?!\d)')


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict), value
    return value


def _sequence(value: object) -> list[object]:
    assert isinstance(value, list), value
    return value


def _one(program: capture.Capture, chain: list[str]) -> dict[str, object]:
    [registration] = [r for urn, r in program.resources.items() if capture.urn_type_chain(urn) == chain]
    return _mapping(registration.state)


@pytest.fixture(scope='module')
def container(program: capture.Capture) -> dict[str, object]:
    template = _mapping(_mapping(_one(program, _JOB_CHAIN)['template'])['template'])
    [only] = [_mapping(c) for c in _sequence(template['containers'])]
    return only


def _envs(container: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(_mapping(e)['name']): _mapping(e) for e in _sequence(container['envs'])}


def test_the_job_runs_the_report_entrypoint_over_the_rendered_queries(container: dict[str, object]) -> None:
    assert container['commands'] == ['python', '-m', 'themis.services.cost_exporter.report']
    envs = _envs(container)
    assert envs['THEMIS_COST_REPORT_QUERIES']['value'] == cost_report.report_json()
    assert envs['THEMIS_COST_REPORT_SLACK_CHANNEL_ID']['value'] == capture.dev_stack_config()['themis:slackChannelId']


def test_the_message_links_the_dashboard_the_failure_page_links(
    program: capture.Capture, container: dict[str, object]
) -> None:
    url = _envs(container)['THEMIS_COST_REPORT_DASHBOARD_URL']['value']
    [link] = [_mapping(link) for link in _sequence(_mapping(_one(program, _POLICY_CHAIN)['documentation'])['links'])]
    assert url == link['url']
    assert 'monitoring/dashboards' in str(url)


def test_the_token_reaches_the_job_from_secret_manager_never_as_a_value(container: dict[str, object]) -> None:
    token = _envs(container)['THEMIS_COST_REPORT_SLACK_BOT_TOKEN']
    assert 'value' not in token
    assert 'secretKeyRef' in _mapping(token['valueSource'])


def test_the_schedule_is_a_sydney_morning(program: capture.Capture) -> None:
    schedule = _one(program, _SCHEDULE_CHAIN)
    assert schedule['timeZone'] == 'Australia/Sydney'
    minute, hour, *rest = str(schedule['schedule']).split()
    assert rest == ['*', '*', '*']
    assert minute == '0'
    assert 5 <= int(hour) <= 10


def test_the_rendering_is_two_flat_groups_of_cents_queries_over_their_windows() -> None:
    rendered = json.loads(cost_report.report_json())
    assert set(rendered) == {'day', 'hourly'}
    day, hourly = rendered['day'], rendered['hourly']
    assert day
    assert hourly
    for query in day.values():
        assert '[24h]' in query, query
        assert '[1h]' not in query, query
    for query in hourly.values():
        assert '[1h]' in query, query
        assert '[24h]' not in query, query
    for query in (*day.values(), *hourly.values()):
        assert isinstance(query, str)
        # Cents throughout: the report divides once at display, so no query converts.
        assert not _CENTS_TO_DOLLARS.search(query), query
        names = set(_LEGACY_NAME.findall(query)) | set(_QUOTED_NAME.findall(query))
        assert names, query
        assert names <= cost_promql.METRICS, (query, names - cost_promql.METRICS)


def test_the_sessions_split_reads_the_same_window_as_its_total() -> None:
    day = json.loads(cost_report.report_json())['day']
    assert day['sessions_tokens'] == f'({day["sessions"]} - {day["sessions_runtime"]} - {day["sessions_search"]})'


def test_a_failed_execution_pages_the_spend_channel_once(program: capture.Capture) -> None:
    policy = _one(program, _POLICY_CHAIN)
    channel = _one(program, _CHANNEL_CHAIN)
    [condition] = [_mapping(c) for c in _sequence(policy['conditions'])]
    threshold = _mapping(condition['conditionThreshold'])
    filter_ = str(threshold['filter'])
    assert 'metric.type="run.googleapis.com/job/completed_execution_count"' in filter_
    assert f'resource.labels.job_name="{cost_report.JOB_NAME}"' in filter_
    assert 'metric.labels.result="failed"' in filter_
    assert threshold['comparison'] == 'COMPARISON_GT'
    assert threshold['thresholdValue'] == 0
    # A count that is only written when an execution ends: the window has to cover a whole run, and an empty window
    # has to read as no failure, or the incident never closes and the next failure never pages.
    [aggregation] = [_mapping(a) for a in _sequence(threshold['aggregations'])]
    assert int(str(aggregation['alignmentPeriod']).removesuffix('s')) >= 240
    assert threshold['evaluationMissingData'] == 'EVALUATION_MISSING_DATA_INACTIVE'
    # Monitoring refuses a missing-data setting on a zero duration, and takes the duration in whole minutes.
    duration = int(str(threshold['duration']).removesuffix('s'))
    assert duration > 0
    assert duration % 60 == 0
    assert policy['notificationChannels'] == [channel['name']]
    assert _sequence(_mapping(policy['alertStrategy'])['notificationPrompts']) == ['OPENED']
    content = str(_mapping(policy['documentation'])['content'])
    assert f'gcloud run jobs executions list --job {cost_report.JOB_NAME}' in content


def test_the_report_identity_reads_metrics_and_the_token_and_nothing_else(program: capture.Capture) -> None:
    runtime = [b for b in program.bindings if b.member.startswith('serviceAccount:themis-cost-report@')]
    assert {b.capability for b in runtime} == {'MetricReader', 'SecretReader'}
    scheduler = [b for b in program.bindings if b.member.startswith('serviceAccount:themis-cost-report-sched@')]
    assert [(b.capability, b.target) for b in scheduler] == [('JobRunner', cost_report.JOB_NAME)]
