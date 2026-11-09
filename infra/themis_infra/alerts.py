"""Operational alerting: the environment's alerts channel and the policies that page it.

The failures a curator would otherwise find first (docs/design/infrastructure.md, Alerting): a scheduled Job whose
latest run failed, a failed execution of a Job no schedule runs, a service answering 5xx, a saturated database, a
certificate that stopped renewing, a gRPC call ending in a server fault, an ACL version the auth service refused, and a
deploy that failed. Every policy posts to the alerts channel, which is separate from the spend monitor's channel
(`cost_alerts.CostAlerts`). The server-fault, ACL and deploy policies read metrics the services and the deploy write
through OpenTelemetry (`operational_metrics`); the rest read the platform's.

Every policy is a state, opening while something is wrong and closing once it is not, except the policies on Jobs no
schedule runs (the sandbox, one execution per agent session, the reference build, and a paused sweep). Those are events:
no later run is due to repair a failed one, so a close would mean nothing, and they only open. A state that a later
event repairs (a Job's next run, the next deploy, the next ACL version) compares the time of the latest failure with the
time of the latest success, so the incident outlasts the gap between the two.

Each Job has a policy of its own, so its page names the Job and says what its failure means; the Job's owner writes that
text as a `JobAlert`. Every page reads as `page` lays it out: what the failure means, what to do, when it closes.

- `JobAlert` — what a failed run of one Job means and what to do about it.
- `page` — a policy's documentation, laid out for Slack; `job_page` lays out a `JobAlert`'s.
- `latest_run_failed` — the query that holds while a scheduled Job's latest run failed; the cost report's alert uses it.
- `OperationalAlerts` — the alerts channel and the operational policies.
"""

from __future__ import annotations

import dataclasses
import datetime
import urllib.parse
from collections.abc import Iterable, Sequence

import pulumi
import pulumi_gcp as gcp

from themis_infra import operational_metrics as metrics
from themis_infra import promql

# A state notifies as it opens and as it closes; an event only as it opens.
STATE_PROMPTS = ('OPENED', 'CLOSED')
EVENT_PROMPTS = ('OPENED',)

_REPOSITORY_URL = 'https://github.com/populationgenomics/themis-internal'
# Monitoring substitutes these in a policy's subject and documentation as it notifies. A PromQL result's label may be a
# metric or a monitored-resource label; this form renders whichever exists.
_PROMQL_LABEL = '${{metric_or_resource.label.{0}}}'
# Kept unescaped in a link so Monitoring still finds a variable inside it.
_URL_SAFE = '${}'


def repository_url(path: str) -> str:
    """A link to `path` on the repository's `main`, for a page's steps."""
    return f'{_REPOSITORY_URL}/blob/main/{path}'


def workflow_url(workflow: str) -> str:
    """A link to the runs of the repository's workflow file `workflow`, where it is also run by hand."""
    return f'{_REPOSITORY_URL}/actions/workflows/{workflow}'


def logs_url(project: str, query: str) -> str:
    """A Logs Explorer link to the entries matching `query`.

    The console's default scope reads every entry that originated in the project, whichever log bucket holds it.
    """
    return (
        'https://console.cloud.google.com/logs/viewer?'
        f'project={project}&advancedFilter={urllib.parse.quote(query, safe=_URL_SAFE)}'
    )


def job_executions_url(*, project: str, region: str, job_name: str) -> str:
    """A link to a Cloud Run Job's executions in the console, the failed ones marked."""
    return f'https://console.cloud.google.com/run/jobs/details/{region}/{job_name}/executions?project={project}'


def job_logs_url(*, project: str, job_name: str) -> str:
    """A Logs Explorer link to every execution's entries of a Cloud Run Job."""
    return logs_url(project, f'resource.type="cloud_run_job" AND resource.labels.job_name="{job_name}"')


def in_days(duration: str) -> str:
    """A PromQL duration in days, as a page states it: `7.5`."""
    return f'{promql.duration(duration) / datetime.timedelta(days=1):g}'


def _service_errors_url(project: str, service: str) -> str:
    return logs_url(
        project,
        f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}" AND severity>=ERROR',
    )


def page(*, meaning: str, steps: Sequence[str], closes: str) -> str:
    """A policy's documentation, in the Markdown Monitoring renders in Slack.

    Slack renders bold, lists and `[text](url)` links from it, but not code spans, so a command or a log line a step
    quotes stays plain text.

    Args:
        meaning: What the failure leaves broken, and for whom.
        steps: What to do, in order.
        closes: When the incident closes, and what its close does and does not mean.

    Raises:
        ValueError: `steps` is empty.
    """
    if not steps:
        raise ValueError('a page needs at least one step')
    return '\n'.join(
        [
            '**What it means**',
            meaning,
            '',
            '**What to do**',
            *(f'- {step}' for step in steps),
            '',
            '**When it closes**',
            closes,
        ]
    )


@dataclasses.dataclass(frozen=True)
class JobAlert:
    """What a failed run of one Cloud Run Job means and what to do about it: the text of that Job's page.

    The page opens with links to the Job's executions and logs, and, for a Job that retries, what a retry means for the
    reader; `steps` start from a failed execution's log.

    Attributes:
        job_name: The Job.
        title: What failed, as the page's subject names it, such as `the reference build`.
        meaning: What a failed run leaves undone, and for whom.
        steps: What to do once the failed execution's log is open, in order.
        max_retries: The Job's own `max_retries`. A page opens on a failed attempt, so with a retry the execution
            may still be running, or may have succeeded, by the time someone reads it.

    Raises:
        ValueError: `max_retries` is negative.
    """

    job_name: str
    title: str
    meaning: str
    steps: Sequence[str]
    max_retries: int

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError(f'{self.job_name}: max_retries is {self.max_retries}, not a count of retries')

    @property
    def subject(self) -> str:
        """The page's subject, which is also its policy's display name."""
        return f'Themis: {self.title} failed'


def job_page(job: JobAlert, *, project: str, region: str, scheduled: bool) -> str:
    """The documentation of the page a failed run of `job` posts.

    Args:
        job: The Job and its page text.
        project: The Job's project.
        region: Where the Job runs.
        scheduled: Whether a schedule runs the Job, so a later successful run closes the incident; otherwise the page
            is an event that posts no close.
    """
    executions = job_executions_url(project=project, region=region, job_name=job.job_name)
    logs = job_logs_url(project=project, job_name=job.job_name)
    steps = [
        f"Find the failed execution on [the Job's executions page]({executions}), and its lines among "
        f"[the Job's logs]({logs})."
    ]
    if job.max_retries:
        retries = 'once' if job.max_retries == 1 else f'up to {job.max_retries} times'
        steps.append(
            f'Cloud Run retries a failed attempt {retries}. If the execution is still running, a retry is under way: '
            'wait for it. If it succeeded, the retry did the work and nothing more is needed.'
        )
    steps.extend(job.steps)
    if scheduled:
        closes = 'About ten minutes after a later run, scheduled or by hand, succeeds.'
    else:
        after_retry = ', even after a retry succeeds' if job.max_retries else ''
        closes = (
            f'No close follows{after_retry}: this message is the whole incident. Failures of this Job within about '
            'ten minutes share it, so the executions page may show more than one.'
        )
    return page(meaning=job.meaning, steps=steps, closes=closes)


# Cloud Run's count of finished task attempts, labelled by result. Every Themis Job runs one task, so its attempts are
# its executions' runs, a retry included. Cloud Run's execution count is not read: it records only some executions.
TASK_ATTEMPTS = 'run_googleapis_com:job_completed_task_attempt_count'
JOB_NAME_LABEL = 'job_name'
# The count's points end on whole minutes, so a one-minute step resolves each attempt to the minute it ended, as
# exactly as the count can; a coarser step can put a failure and a later success in one step and tie them.
_ATTEMPT_STEP = '1m'
# How far back a Job's latest run, the latest ACL load and the latest deploy are looked for: longer than the weekly
# refresh's gap between runs, and inside Monitoring's five-minute evaluation band (`promql.evaluation_floor`).
# A deploy outcome is renewed only by the next deploy, so a failed one with no deploy after it closes after this long.
_OUTCOME_LOOKBACK = '180h'
# Cloud Run writes the count up to 180 s after the attempt ends and the policy evaluates every 60 s; the window holds a
# failure for at least one evaluation after its point lands. Monitoring closes the incident 270 s or more after the
# query last held and the count names no execution, so failures of one Job within about ten minutes share one incident.
_EVENT_FAILURE_WINDOW = '5m'

# More than this many 5xx responses from one service in one window pages.
_SERVER_ERRORS_PER_WINDOW = 5
_SERVER_ERROR_WINDOW_SECONDS = 300
_SQL_UTILIZATION_LIMIT = 0.9
_SQL_SUSTAINED = '900s'
# The share of the tier's connection limit in use that pages, before the services start failing to connect.
_SQL_CONNECTION_SHARE = 0.8
_CERTIFICATE_PERIOD = '900s'
# The statuses a server returns for its own fault, not the caller's.
_SERVER_FAULTS = ('INTERNAL', 'UNKNOWN', 'UNAVAILABLE', 'DATA_LOSS')
# The services export every minute, so a PromQL policy has nothing newer to read between; a query reading further back
# evaluates as often as Monitoring allows.
_PROMQL_EVALUATION = datetime.timedelta(seconds=60)
# A managed certificate renews about 30 days before it expires; still under this many days means renewal is failing.
_CERTIFICATE_DAYS_LEFT = 14
# A check failing this long means the hostname no longer reaches the load balancer with a valid certificate; longer
# than a fresh certificate takes to provision, so a bring-up does not page.
_CERTIFICATE_UNREACHABLE = '7200s'
# Uptime checks run from several regions; more than one failing rules out a single checker's fault.
_CHECK_REGIONS_FAILING = 1


def _promql(
    query: pulumi.Input[str], *, lookback: datetime.timedelta
) -> gcp.monitoring.AlertPolicyConditionConditionPrometheusQueryLanguageArgs:
    """A PromQL condition that holds as soon as `query` returns, its query reading `lookback` back."""
    interval = max(_PROMQL_EVALUATION, promql.evaluation_floor(lookback))
    return gcp.monitoring.AlertPolicyConditionConditionPrometheusQueryLanguageArgs(
        query=query,
        evaluation_interval=f'{interval.total_seconds():.0f}s',
        # Zero also because Monitoring refuses a retest window past `promql.RETEST_LOOKBACK_LIMIT`.
        duration='0s',
        # The policies are declared with the producers, before the first point on a fresh environment.
        disable_metric_validation=True,
    )


def _attempts(result: str, job_names: Iterable[str]) -> str:
    return promql.selector(
        TASK_ATTEMPTS,
        promql.equals('monitored_resource', 'cloud_run_job'),
        promql.among(JOB_NAME_LABEL, job_names),
        promql.equals('result', result),
    )


def _last_attempt(result: str, job_names: Iterable[str]) -> str:
    """Per Job, the end time of its latest attempt with `result` inside the lookback."""
    ended = f'sum by ({JOB_NAME_LABEL}) (increase({_attempts(result, job_names)}[{_ATTEMPT_STEP}])) > 0'
    return f'max by ({JOB_NAME_LABEL}) (max_over_time((timestamp({ended}))[{_OUTCOME_LOOKBACK}:{_ATTEMPT_STEP}]))'


def latest_run_failed(job_names: Iterable[str]) -> str:
    """A query that holds, per Job, while the latest run of one of `job_names` failed.

    It holds while a Job's latest failed attempt is newer than its latest successful one, or the Job has failed with
    no success inside the lookback; a later success closes it.

    Raises:
        ValueError: `job_names` is empty.
    """
    names = sorted(job_names)
    if not names:
        raise ValueError('a latest-run condition needs at least one Job')
    failed, succeeded = _last_attempt('failed', names), _last_attempt('succeeded', names)
    on = f'on ({JOB_NAME_LABEL})'
    return f'({failed} - {on} {succeeded} > 0) or ({failed} unless {on} {succeeded})'


def latest_run_failed_condition(job_names: Sequence[pulumi.Input[str]]) -> gcp.monitoring.AlertPolicyConditionArgs:
    """A condition that holds, one incident per Job, while the latest run of one of `job_names` failed."""
    return gcp.monitoring.AlertPolicyConditionArgs(
        display_name="A scheduled Job's latest run failed",
        condition_prometheus_query_language=_promql(
            pulumi.Output.all(*job_names).apply(latest_run_failed),
            lookback=promql.duration(_OUTCOME_LOOKBACK) + promql.duration(_ATTEMPT_STEP),
        ),
    )


def _latest_outcome_failed(metric: str, *, outcome_label: str, success: str, lookback: str, by: str) -> str:
    """A query that holds, per `by`, while `metric`'s latest failure is newer than its latest success.

    `metric` is a gauge set to the Unix time of each outcome, labelled by it; with no success inside the lookback,
    any failure holds.
    """
    failures = promql.selector(metric, promql.not_among(outcome_label, [success]))
    successes = promql.selector(metric, promql.equals(outcome_label, success))
    # `max_over_time`, not `last_over_time`: Monitoring's `last_over_time` returns nothing for a series last written
    # hours earlier, and a timestamp only grows, so the two agree wherever both answer.
    return (
        f'max by ({by}) (max_over_time({failures}[{lookback}]))'
        f' > scalar(max(max_over_time({successes}[{lookback}])) or vector(0))'
    )


class OperationalAlerts(pulumi.ComponentResource):
    """The environment's alerts channel, and a policy for each failure a curator would otherwise find first."""

    def __init__(
        self,
        *,
        project: str,
        region: str,
        slack_channel: str,
        slack_bot_token: pulumi.Input[str],
        scheduled_jobs: Sequence[JobAlert],
        unscheduled_jobs: Sequence[JobAlert],
        services: Sequence[pulumi.Input[str]],
        sql_instance: pulumi.Input[str],
        sql_tier_connections: int,
        domain: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the alerts channel and the operational policies.

        Args:
            project: The project the policies watch.
            region: Where the project's Jobs are.
            slack_channel: The alerts channel's name, `#` included.
            slack_bot_token: The bot token of the environment's Slack app, invited to the channel; a secret.
            scheduled_jobs: The Cloud Run Jobs a schedule runs, whose latest run failing pages until one succeeds.
            unscheduled_jobs: The Cloud Run Jobs no schedule runs (the sandbox, the reference build, and a sweep
                whose schedule is paused), whose failed executions page as they happen and never close with news.
            services: The Cloud Run services whose 5xx responses page.
            sql_instance: The Cloud SQL instance's name.
            sql_tier_connections: The connection limit of the instance's tier (`sql.max_connections`).
            domain: The hostname the load balancer's certificate covers.
            opts: Resource options (dependency wiring).

        Raises:
            ValueError: `scheduled_jobs` or `unscheduled_jobs` is empty.
        """
        if not scheduled_jobs or not unscheduled_jobs:
            raise ValueError('the operational alerts need at least one scheduled and one unscheduled Job')
        super().__init__('themis:infra:OperationalAlerts', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        channel = gcp.monitoring.NotificationChannel(
            'themis-alerts-slack',
            project=project,
            type='slack',
            display_name=f'Themis alerts: Slack {slack_channel}',
            labels={'channel_name': slack_channel},
            sensitive_labels=gcp.monitoring.NotificationChannelSensitiveLabelsArgs(auth_token=slack_bot_token),
            opts=child,
        )

        def policy(
            name: str,
            *,
            display_name: str,
            conditions: Sequence[gcp.monitoring.AlertPolicyConditionArgs],
            notification_prompts: Sequence[str],
            subject: str,
            content: pulumi.Input[str],
            aliases: Sequence[pulumi.Alias] = (),
        ) -> None:
            gcp.monitoring.AlertPolicy(
                name,
                project=project,
                display_name=display_name,
                combiner='OR',
                severity='ERROR',
                conditions=list(conditions),
                notification_channels=[channel.name],
                alert_strategy=gcp.monitoring.AlertPolicyAlertStrategyArgs(
                    notification_prompts=list(notification_prompts)
                ),
                documentation=gcp.monitoring.AlertPolicyDocumentationArgs(
                    mime_type='text/markdown', subject=subject, content=content
                ),
                opts=pulumi.ResourceOptions(parent=self, aliases=list(aliases)),
            )

        def job_policy(job: JobAlert, *, scheduled: bool) -> None:
            if scheduled:
                condition = latest_run_failed_condition([job.job_name])
            else:
                attempts = _attempts('failed', [job.job_name])
                failures = f'sum by ({JOB_NAME_LABEL}) (increase({attempts}[{_EVENT_FAILURE_WINDOW}])) > 0'
                condition = gcp.monitoring.AlertPolicyConditionArgs(
                    display_name=f'An attempt of {job.job_name} failed',
                    condition_prometheus_query_language=_promql(
                        failures, lookback=promql.duration(_EVENT_FAILURE_WINDOW)
                    ),
                )
            policy(
                f'themis-{job.job_name.removeprefix("themis-")}-failed',
                display_name=job.subject,
                conditions=[condition],
                notification_prompts=STATE_PROMPTS if scheduled else EVENT_PROMPTS,
                subject=job.subject,
                content=job_page(job, project=project, region=region, scheduled=scheduled),
            )

        for job in scheduled_jobs:
            job_policy(job, scheduled=True)
        for job in unscheduled_jobs:
            job_policy(job, scheduled=False)

        service_filter = pulumi.Output.all(*services).apply(lambda names: ', '.join(f'"{n}"' for n in names))
        policy(
            'themis-service-5xx',
            display_name='Themis: a service is answering 5xx',
            conditions=[
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name=(
                        f'More than {_SERVER_ERRORS_PER_WINDOW} 5xx responses from one service in '
                        f'{_SERVER_ERROR_WINDOW_SECONDS // 60} min'
                    ),
                    condition_threshold=gcp.monitoring.AlertPolicyConditionConditionThresholdArgs(
                        filter=pulumi.Output.format(
                            'metric.type="run.googleapis.com/request_count" AND resource.type="cloud_run_revision"'
                            ' AND metric.labels.response_code_class="5xx"'
                            ' AND resource.labels.service_name=one_of({0})',
                            service_filter,
                        ),
                        comparison='COMPARISON_GT',
                        threshold_value=_SERVER_ERRORS_PER_WINDOW,
                        duration='60s',
                        aggregations=[
                            gcp.monitoring.AlertPolicyConditionConditionThresholdAggregationArgs(
                                alignment_period=f'{_SERVER_ERROR_WINDOW_SECONDS}s',
                                per_series_aligner='ALIGN_SUM',
                                cross_series_reducer='REDUCE_SUM',
                                group_by_fields=['resource.labels.service_name'],
                            )
                        ],
                        # A service that serves nothing writes no count; silence is not an error.
                        evaluation_missing_data='EVALUATION_MISSING_DATA_INACTIVE',
                    ),
                )
            ],
            notification_prompts=STATE_PROMPTS,
            subject='Themis: ${resource.label.service_name} is answering 5xx',
            content=page(
                meaning=(
                    f'The service returned more than {_SERVER_ERRORS_PER_WINDOW} server errors in '
                    f'{_SERVER_ERROR_WINDOW_SECONDS // 60} minutes, so requests through it are failing. For a gRPC '
                    'service (auth, evidence, sheaf) this counts transport failures only; the "failing calls" alert '
                    'counts the errors its handlers return.'
                ),
                steps=[
                    f'Read [its error logs]({_service_errors_url(project, "${resource.label.service_name}")}).',
                    'Follow one failing request through the services: '
                    f'[reading a request trace]({repository_url("docs/runbooks/reading-a-request-trace.md")}).',
                ],
                closes=(
                    f'Once {_SERVER_ERROR_WINDOW_SECONDS // 60} minutes pass with no more than '
                    f'{_SERVER_ERRORS_PER_WINDOW} server errors, or with no traffic.'
                ),
            ),
        )

        faults = promql.selector(
            f'{metrics.RPC_SERVER_CALL_DURATION}_count',
            promql.among(metrics.RPC_RESPONSE_STATUS_CODE_LABEL, _SERVER_FAULTS),
        )
        window = f'{_SERVER_ERROR_WINDOW_SECONDS // 60}m'
        policy(
            'themis-rpc-server-faults',
            display_name='Themis: a service is failing calls',
            conditions=[
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name=(
                        f'More than {_SERVER_ERRORS_PER_WINDOW} calls to one service ended in a server fault in '
                        f'{_SERVER_ERROR_WINDOW_SECONDS // 60} min'
                    ),
                    condition_prometheus_query_language=_promql(
                        f'sum by (job) (increase({faults}[{window}])) > {_SERVER_ERRORS_PER_WINDOW}',
                        lookback=promql.duration(window),
                    ),
                )
            ],
            notification_prompts=STATE_PROMPTS,
            subject=f'Themis: {_PROMQL_LABEL.format("job")} is failing calls',
            content=page(
                meaning=(
                    f'More than {_SERVER_ERRORS_PER_WINDOW} calls to the service in '
                    f'{_SERVER_ERROR_WINDOW_SECONDS // 60} minutes ended in a server fault '
                    f'({", ".join(_SERVER_FAULTS)}), so its callers got errors.'
                ),
                steps=[
                    f'Read [its error logs]({_service_errors_url(project, _PROMQL_LABEL.format("job"))}).',
                    'See which method failed with which status: Metrics Explorer, metric '
                    f'"{metrics.RPC_SERVER_CALL_DURATION}", grouped by its method and status labels.',
                ],
                closes=(
                    f'Once {_SERVER_ERROR_WINDOW_SECONDS // 60} minutes pass with no more than '
                    f'{_SERVER_ERRORS_PER_WINDOW} faults, or with no calls.'
                ),
            ),
        )

        acl_rejected = _latest_outcome_failed(
            metrics.ACL_ATTEMPT,
            outcome_label=metrics.OUTCOME_LABEL,
            success=metrics.ACL_LOADED,
            lookback=_OUTCOME_LOOKBACK,
            by=metrics.OUTCOME_LABEL,
        )
        policy(
            'themis-auth-acl-rejected',
            display_name='Themis auth: ACL rejected',
            conditions=[
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name='The version of the ACL the auth service last tried to load was rejected',
                    condition_prometheus_query_language=_promql(
                        acl_rejected, lookback=promql.duration(_OUTCOME_LOOKBACK)
                    ),
                )
            ],
            notification_prompts=STATE_PROMPTS,
            subject='Themis: the auth service rejected the ACL',
            content=page(
                meaning=(
                    'The auth service could not use the latest version of the ACL (reason: '
                    f'{_PROMQL_LABEL.format(metrics.OUTCOME_LABEL)}). Running instances answer from their last good '
                    'copy until the staleness limit (the stack setting themis:aclStalenessLimitSeconds), then refuse '
                    'everyone until a valid version loads. An instance that starts on a rejected version does not '
                    'start at all.'
                ),
                steps=[
                    'Find the broken rule in the auth service\'s log line that starts "acl rejected:", among '
                    f'[its error logs]({_service_errors_url(project, "themis-auth")}).',
                    'Fix the ACL and publish a new version: '
                    f'[when the auth service rejects the ACL]({repository_url("docs/runbooks/access-control.md")}'
                    '#when-the-auth-service-rejects-the-acl).',
                ],
                closes='About ten minutes after any instance loads a version cleanly.',
            ),
            # The policy predates the alerts component, as a top-level resource of the same name.
            aliases=[pulumi.Alias(parent=None)],
        )

        deploy_failed = _latest_outcome_failed(
            metrics.DEPLOY_FINISHED,
            outcome_label=metrics.RESULT_LABEL,
            success=metrics.DEPLOY_SUCCEEDED,
            lookback=_OUTCOME_LOOKBACK,
            by='instance',
        )
        policy(
            'themis-deploy-failed',
            display_name='Themis: a deploy failed',
            conditions=[
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name='A deploy run failed or was cancelled since the last successful deploy',
                    condition_prometheus_query_language=_promql(
                        deploy_failed, lookback=promql.duration(_OUTCOME_LOOKBACK)
                    ),
                )
            ],
            notification_prompts=STATE_PROMPTS,
            subject='Themis: a deploy failed',
            content=page(
                meaning=(
                    f'Deploy run {_PROMQL_LABEL.format("instance")} (run id, then attempt) failed or was cancelled, '
                    'and no deploy of this environment has succeeded since. The environment still runs what the last '
                    'successful deploy put live.'
                ),
                steps=[
                    f"Find the run among [the deploy workflow's runs]({workflow_url('deploy.yml')}) and see which "
                    'step failed.',
                    'Fix the cause and merge, or rerun the run if the failure was transient.',
                ],
                closes=(
                    'About ten minutes after the next successful deploy of this environment (a run that only builds '
                    f'the images does not count), or {in_days(_OUTCOME_LOOKBACK)} days after the failure if none comes.'
                ),
            ),
        )

        database = pulumi.Output.format('{0}:{1}', project, sql_instance)
        connection_limit = int(sql_tier_connections * _SQL_CONNECTION_SHARE)

        def sql_condition(
            display_name: str, metric: str, threshold: float, reducer: str
        ) -> gcp.monitoring.AlertPolicyConditionArgs:
            return gcp.monitoring.AlertPolicyConditionArgs(
                display_name=display_name,
                condition_threshold=gcp.monitoring.AlertPolicyConditionConditionThresholdArgs(
                    filter=pulumi.Output.format(
                        'metric.type="cloudsql.googleapis.com/database/{0}" AND resource.type="cloudsql_database"'
                        ' AND resource.labels.database_id="{1}"',
                        metric,
                        database,
                    ),
                    comparison='COMPARISON_GT',
                    threshold_value=threshold,
                    duration=_SQL_SUSTAINED,
                    aggregations=[
                        gcp.monitoring.AlertPolicyConditionConditionThresholdAggregationArgs(
                            alignment_period='300s', per_series_aligner='ALIGN_MEAN', cross_series_reducer=reducer
                        )
                    ],
                ),
            )

        sql_content = pulumi.Output.from_input(sql_instance).apply(
            lambda instance: page(
                meaning=(
                    f'The Cloud SQL instance {instance} is close to a limit of its tier; the condition above says '
                    'which. At the limit, queries slow down or new connections fail.'
                ),
                steps=[
                    "Check what drives it on [the instance's overview]("
                    f'https://console.cloud.google.com/sql/instances/{instance}/overview?project={project}).',
                    "If the load is expected, raise themis:sqlTier in this environment's stack file "
                    '(infra/Pulumi.<environment>.yaml) and deploy. A larger tier is an in-place update that restarts '
                    'the instance for a few minutes.',
                ],
                closes='Once the condition no longer holds.',
            )
        )
        policy(
            'themis-sql-saturated',
            display_name='Themis: the database is saturated',
            conditions=[
                sql_condition(
                    f'CPU above {_SQL_UTILIZATION_LIMIT:.0%} for 15 min',
                    'cpu/utilization',
                    _SQL_UTILIZATION_LIMIT,
                    'REDUCE_MAX',
                ),
                sql_condition(
                    f'Disk above {_SQL_UTILIZATION_LIMIT:.0%} for 15 min',
                    'disk/utilization',
                    _SQL_UTILIZATION_LIMIT,
                    'REDUCE_MAX',
                ),
                sql_condition(
                    f"More than {connection_limit} of the tier's {sql_tier_connections} connections for 15 min",
                    'postgresql/num_backends',
                    connection_limit,
                    'REDUCE_SUM',
                ),
            ],
            notification_prompts=STATE_PROMPTS,
            subject='Themis: the database is near a limit of its tier',
            content=sql_content,
        )

        unreachable_hours = promql.duration(_CERTIFICATE_UNREACHABLE) / datetime.timedelta(hours=1)
        dns_handoff = f'{repository_url("docs/runbooks/fresh-environment.md")}#dns-handoff-external--it-team'
        check = gcp.monitoring.UptimeCheckConfig(
            'themis-certificate',
            project=project,
            display_name=f'Themis: {domain} certificate',
            period=_CERTIFICATE_PERIOD,
            timeout='10s',
            monitored_resource=gcp.monitoring.UptimeCheckConfigMonitoredResourceArgs(
                type='uptime_url', labels={'project_id': project, 'host': domain}
            ),
            # The check exists for the certificate it reads: IAP answers an anonymous request with a redirect or a
            # refusal, and either is a served response.
            http_check=gcp.monitoring.UptimeCheckConfigHttpCheckArgs(
                path='/',
                port=443,
                use_ssl=True,
                validate_ssl=True,
                accepted_response_status_codes=[
                    gcp.monitoring.UptimeCheckConfigHttpCheckAcceptedResponseStatusCodeArgs(status_class=status_class)
                    for status_class in ('STATUS_CLASS_2XX', 'STATUS_CLASS_3XX', 'STATUS_CLASS_4XX')
                ],
            ),
            opts=child,
        )
        policy(
            'themis-certificate-expiring',
            display_name='Themis: the certificate is not renewing',
            conditions=[
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name=f"Fewer than {_CERTIFICATE_DAYS_LEFT} days before {domain}'s certificate expires",
                    condition_threshold=gcp.monitoring.AlertPolicyConditionConditionThresholdArgs(
                        filter=pulumi.Output.format(
                            'metric.type="monitoring.googleapis.com/uptime_check/time_until_ssl_cert_expires"'
                            ' AND resource.type="uptime_url" AND metric.labels.check_id="{0}"',
                            check.uptime_check_id,
                        ),
                        comparison='COMPARISON_LT',
                        threshold_value=_CERTIFICATE_DAYS_LEFT,
                        duration='0s',
                        aggregations=[
                            gcp.monitoring.AlertPolicyConditionConditionThresholdAggregationArgs(
                                alignment_period='3600s',
                                per_series_aligner='ALIGN_MIN',
                                cross_series_reducer='REDUCE_MIN',
                            )
                        ],
                    ),
                ),
                # When the A record no longer points here, the check never reads this certificate, so the expiry
                # metric above has no points; the check failing is what that case looks like.
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name=f'{domain} does not answer with a valid certificate',
                    condition_threshold=gcp.monitoring.AlertPolicyConditionConditionThresholdArgs(
                        filter=pulumi.Output.format(
                            'metric.type="monitoring.googleapis.com/uptime_check/check_passed"'
                            ' AND resource.type="uptime_url" AND metric.labels.check_id="{0}"',
                            check.uptime_check_id,
                        ),
                        comparison='COMPARISON_GT',
                        threshold_value=_CHECK_REGIONS_FAILING,
                        duration=_CERTIFICATE_UNREACHABLE,
                        aggregations=[
                            gcp.monitoring.AlertPolicyConditionConditionThresholdAggregationArgs(
                                alignment_period='1200s',
                                per_series_aligner='ALIGN_NEXT_OLDER',
                                cross_series_reducer='REDUCE_COUNT_FALSE',
                                group_by_fields=['resource.label.host'],
                            )
                        ],
                    ),
                ),
            ],
            notification_prompts=STATE_PROMPTS,
            subject=f'Themis: the certificate for {domain} is not renewing',
            content=page(
                meaning=(
                    f'Either fewer than {_CERTIFICATE_DAYS_LEFT} days are left on the Google-managed certificate for '
                    f'{domain}, which renews about 30 days before it expires, or https://{domain}/ has not answered '
                    f'with a valid certificate for {unreachable_hours:g} hours; the condition above says which. Once '
                    'the certificate expires, the site stops loading.'
                ),
                steps=[
                    'Check whether the DNS A record still points at the load balancer: either condition usually means '
                    f'it does not, which also stops renewal. See [DNS handoff]({dns_handoff}).',
                    f"The certificate's status: gcloud compute ssl-certificates list --project {project}",
                ],
                closes=(
                    f'Once more than {_CERTIFICATE_DAYS_LEFT} days are left and https://{domain}/ answers with a '
                    'valid certificate.'
                ),
            ),
        )

        self.register_outputs({})
