"""Every alert reaches a channel: operational incidents the alerts channel, spend incidents the spend channel.

Also: the operational policies watch the resources the program actually declares; every scheduled Job pages on its
latest run or on its silence; every policy is a state that notifies as it closes, except the per-execution failure of
a Job no schedule runs; and the database's connection condition follows the stack's tier.
"""

from __future__ import annotations

import datetime
import re

import pytest

from themis_infra import alerts, capture, promql, sql

_CHANNEL_TYPE = 'gcp:monitoring/notificationChannel:NotificationChannel'
_POLICY_TYPE = 'gcp:monitoring/alertPolicy:AlertPolicy'
_JOB_TYPE = 'gcp:cloudrunv2/job:Job'
_SCHEDULE_TYPE = 'gcp:cloudscheduler/job:Job'
# A schedule that runs a Cloud Run Job POSTs to the Job's v2 `:run` endpoint.
_RUN_ENDPOINT = re.compile(r'https://run\.googleapis\.com/v2/projects/[^/]+/locations/[^/]+/jobs/([^/:]+):run')
# The job-name matcher `promql.among` writes; no Job name carries an RE2 metacharacter.
_JOB_MATCHER = re.compile(alerts.JOB_NAME_LABEL + r'=~"\^\(([^)"]*)\)\$"')
# A range selector or subquery, `[5m]` or `[180h:1m]`, and an offset.
_RANGE = re.compile(r'\[(\w+?)(?::(\w+))?\]')
_OFFSET = re.compile(r'\boffset (\w+)')
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


@pytest.fixture(scope='module')
def jobs(program: capture.Capture) -> set[str]:
    """Every Cloud Run Job the program declares, by name."""
    names = {
        str(_mapping(registration.state)['name'])
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == _JOB_TYPE
    }
    assert names, 'the program declares no Cloud Run Job'
    return names


def _schedule_targets(program: capture.Capture, *, paused: bool) -> set[str]:
    """The Cloud Run Jobs the program's Cloud Scheduler jobs run, read off each target: the paused ones, or the rest."""
    schedules = [
        _mapping(registration.state)
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == _SCHEDULE_TYPE
    ]
    assert schedules, 'the program schedules nothing'
    names = set()
    for schedule in schedules:
        if bool(schedule.get('paused', False)) != paused:
            continue
        target = str(_mapping(schedule['httpTarget'])['uri'])
        match = _RUN_ENDPOINT.fullmatch(target)
        assert match, f'a schedule that runs no Cloud Run Job: {target}'
        names.add(match.group(1))
    return names


@pytest.fixture(scope='module')
def scheduled_jobs(program: capture.Capture, jobs: set[str]) -> set[str]:
    """The Cloud Run Jobs a running schedule runs; a paused schedule's Job runs only by hand."""
    names = _schedule_targets(program, paused=False)
    assert names <= jobs, names - jobs
    return names


def _promql_queries(policy: dict[str, object]) -> list[str]:
    return [
        str(_mapping(_mapping(c)['conditionPrometheusQueryLanguage'])['query'])
        for c in _sequence(policy['conditions'])
        if 'conditionPrometheusQueryLanguage' in _mapping(c)
    ]


def _watched_jobs(query: str) -> set[str]:
    """The Jobs a query over Cloud Run's attempt count names; none for any other query."""
    if alerts.TASK_ATTEMPTS not in query:
        return set()
    matchers = _JOB_MATCHER.findall(query)
    assert matchers, query
    return {name for matcher in matchers for name in matcher.split('|')}


def _pages_on_silence(policy: dict[str, object], *, executions_url: str) -> bool:
    """Whether `policy` is a silence condition whose page sends the reader to the Job executions at `executions_url`."""
    silence = any(query.startswith('absent_over_time(') for query in _promql_queries(policy))
    return silence and executions_url in str(_mapping(policy['documentation'])['content'])


def _prompts(policy: dict[str, object]) -> tuple[str, ...]:
    return tuple(str(p) for p in _sequence(_mapping(policy['alertStrategy'])['notificationPrompts']))


def test_every_scheduled_job_pages_on_its_latest_run_or_on_its_silence(
    policies: dict[str, tuple[list[str], dict[str, object]]], scheduled_jobs: set[str], config: dict[str, str]
) -> None:
    latest_run: set[str] = set()
    for _, policy in policies.values():
        for query in _promql_queries(policy):
            names = _watched_jobs(query)
            if names and query == alerts.latest_run_failed(names):
                latest_run |= names
    silence = {
        job
        for job in scheduled_jobs
        if any(
            _pages_on_silence(
                policy,
                executions_url=alerts.job_executions_url(
                    project=config['gcp:project'], region=config['gcp:region'], job_name=job
                ),
            )
            for _, policy in policies.values()
        )
    }
    assert latest_run | silence == scheduled_jobs


def test_every_unscheduled_job_pages_on_each_failed_execution(
    policies: dict[str, tuple[list[str], dict[str, object]]], jobs: set[str], scheduled_jobs: set[str]
) -> None:
    unscheduled = jobs - scheduled_jobs
    assert unscheduled, 'every Job is scheduled, so no policy is an event'
    watched = {
        name
        for _, policy in policies.values()
        if _prompts(policy) == alerts.EVENT_PROMPTS
        for query in _promql_queries(policy)
        if 'result="failed"' in query and 'result="succeeded"' not in query
        for name in _watched_jobs(query)
    }
    assert watched == unscheduled


def test_a_paused_schedules_job_pages_on_each_failure_not_on_its_latest_run(
    program: capture.Capture, policies: dict[str, tuple[list[str], dict[str, object]]], scheduled_jobs: set[str]
) -> None:
    # A Job whose schedule is paused runs only by hand, so no later run is due to close a latest-run incident.
    paused = _schedule_targets(program, paused=True)
    assert paused, 'the dev stack pauses no schedule (themis:workSweepScheduled), so this checks nothing'
    assert not paused & scheduled_jobs
    event = {
        name
        for _, policy in policies.values()
        if _prompts(policy) == alerts.EVENT_PROMPTS
        for query in _promql_queries(policy)
        for name in _watched_jobs(query)
    }
    assert paused <= event


def test_only_a_policy_on_an_unscheduled_job_skips_the_close(
    policies: dict[str, tuple[list[str], dict[str, object]]], jobs: set[str], scheduled_jobs: set[str]
) -> None:
    # A failed execution of a Job no schedule runs is an event no later execution repairs; every other policy is a
    # state whose close means the failure is over, so the close is news.
    for name, (_, policy) in policies.items():
        watched = {job for query in _promql_queries(policy) for job in _watched_jobs(query)}
        event = bool(watched) and watched <= jobs - scheduled_jobs
        assert _prompts(policy) == (alerts.EVENT_PROMPTS if event else alerts.STATE_PROMPTS), name


def _lookback(query: str) -> datetime.timedelta:
    """How far back a query reads, at most: its longest range, a subquery's inner range and its longest offset."""
    ranges = _RANGE.findall(query)
    assert ranges, query
    outer = [promql.duration(span) for span, step in ranges if step]
    inner = [promql.duration(span) for span, step in ranges if not step]
    offset = max((promql.duration(o) for o in _OFFSET.findall(query)), default=datetime.timedelta())
    return max(outer, default=datetime.timedelta()) + max(inner, default=datetime.timedelta()) + offset


def test_every_promql_condition_evaluates_no_faster_than_its_lookback_allows(
    policies: dict[str, tuple[list[str], dict[str, object]]],
) -> None:
    # Monitoring refuses, at `pulumi up`, an interval under its floor for the lookback, and a retest window on a query
    # reading past 25 h.
    checked = 0
    for name, (_, policy) in policies.items():
        for c in _sequence(policy['conditions']):
            if 'conditionPrometheusQueryLanguage' not in _mapping(c):
                continue
            condition = _mapping(_mapping(c)['conditionPrometheusQueryLanguage'])
            lookback = _lookback(str(condition['query']))
            interval = promql.duration(str(condition['evaluationInterval']))
            assert interval >= promql.evaluation_floor(lookback), (name, lookback, interval)
            if lookback > promql.RETEST_LOOKBACK_LIMIT:
                assert not promql.duration(str(condition['duration'])), name
            checked += 1
    assert checked


def test_every_job_policy_watches_one_job(policies: dict[str, tuple[list[str], dict[str, object]]]) -> None:
    # One Job per policy is what lets its page name the Job and say what that Job's failure means.
    watched = [
        (name, _watched_jobs(query)) for name, (_, policy) in policies.items() for query in _promql_queries(policy)
    ]
    job_policies = [(name, jobs) for name, jobs in watched if jobs]
    assert job_policies
    for name, jobs in job_policies:
        assert len(jobs) == 1, (name, jobs)


def test_every_page_is_laid_out_for_slack(policies: dict[str, tuple[list[str], dict[str, object]]]) -> None:
    # Slack drops a Markdown code span's backticks and runs its text into the prose. The layout check also proves the
    # content resolved: an Output's placeholder would hold no backtick either.
    assert policies
    for name, (_, policy) in policies.items():
        documentation = _mapping(policy['documentation'])
        content = str(documentation['content'])
        assert content.startswith('**What it means**'), name
        assert '`' not in str(documentation['subject']) + content, name


def test_a_page_without_a_step_is_refused() -> None:
    with pytest.raises(ValueError, match='at least one step'):
        alerts.page(meaning='Something broke.', steps=[], closes='Never.')


def test_a_job_alert_with_negative_retries_is_refused() -> None:
    with pytest.raises(ValueError, match='max_retries'):
        alerts.JobAlert(job_name='themis-x', title='x', meaning='Broke.', steps=['Fix it.'], max_retries=-1)


def test_a_latest_run_condition_over_no_job_is_refused() -> None:
    with pytest.raises(ValueError, match='at least one Job'):
        alerts.latest_run_failed([])


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
