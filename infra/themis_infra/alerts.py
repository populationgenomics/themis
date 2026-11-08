"""Operational alerting: the environment's alerts channel and the policies that page it.

The failures a curator would otherwise find first (docs/design/infrastructure.md, Alerting): a scheduled Job whose
latest run failed, a failed execution of a Job no schedule runs, a service answering 5xx, a saturated database, a
certificate that stopped renewing, a gRPC call ending in a server fault, an ACL version the auth service refused, and a
deploy that failed. Every policy posts to the alerts channel, which is separate from the spend monitor's channel
(`cost_alerts.CostAlerts`). The server-fault, ACL and deploy policies read metrics the services and the deploy write
through OpenTelemetry (`operational_metrics`); the rest read the platform's.

Every policy but one is a state: it opens while something is wrong and closes once it is not, and both transitions
notify. The policy on Jobs no schedule runs (the sandbox, one execution per agent session, the reference build, and a
paused sweep) is an event: no later run is due to repair a failed one, so a close would mean nothing, and it only
opens. A state that a later event repairs (a Job's next run, the next deploy, the next ACL version) compares the
time of the latest failure with the time of the latest success, so the incident outlasts the gap between the two.

- `latest_run_failed` — the query that holds while a scheduled Job's latest run failed; the cost report's alert uses it.
- `OperationalAlerts` — the alerts channel and the operational policies.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence

import pulumi
import pulumi_gcp as gcp

from themis_infra import audit, promql
from themis_infra import operational_metrics as metrics

# A state notifies as it opens and as it closes; an event only as it opens.
STATE_PROMPTS = ('OPENED', 'CLOSED')
EVENT_PROMPTS = ('OPENED',)

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
        scheduled_jobs: Sequence[pulumi.Input[str]],
        unscheduled_jobs: Sequence[pulumi.Input[str]],
        services: Sequence[pulumi.Input[str]],
        sql_instance: pulumi.Input[str],
        sql_tier_connections: int,
        domain: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the alerts channel and the operational policies.

        Args:
            project: The project the policies watch.
            region: Where the project's Jobs and its regional log bucket are.
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

        policy(
            'themis-job-failed',
            display_name="Themis: a scheduled Job's latest run failed",
            conditions=[latest_run_failed_condition(scheduled_jobs)],
            notification_prompts=STATE_PROMPTS,
            subject="Themis: a scheduled Job's latest run failed",
            content=(
                'The latest run of a scheduled Cloud Run Job failed; the incident names the Job as '
                f'`{JOB_NAME_LABEL}`, and closes about ten minutes after a later run, scheduled or by hand, that '
                "succeeds. A failed gene-disease refresh leaves the evidence service on last week's dumps, a failed "
                'sweep leaves papers unconverted until the next, and a failing cost exporter leaves the spend '
                'dashboard stale. A Job that retries opens on a failed attempt and closes if the retry succeeds. Its '
                'executions, and the logs of each: '
                f'`gcloud run jobs executions list --job <job> --region {region} --project {project}`.'
            ),
        )

        unscheduled_failures = pulumi.Output.all(*unscheduled_jobs).apply(
            lambda names: (
                f'sum by ({JOB_NAME_LABEL}) (increase({_attempts("failed", names)}[{_EVENT_FAILURE_WINDOW}])) > 0'
            )
        )
        policy(
            'themis-unscheduled-job-failed',
            display_name='Themis: an execution of an unscheduled Job failed',
            conditions=[
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name='An execution of a Job no schedule runs failed',
                    condition_prometheus_query_language=_promql(
                        unscheduled_failures, lookback=promql.duration(_EVENT_FAILURE_WINDOW)
                    ),
                )
            ],
            notification_prompts=EVENT_PROMPTS,
            subject='Themis: an execution of an unscheduled Job failed',
            content=(
                'An execution of a Cloud Run Job that runs on demand or by hand failed; the incident names the Job '
                f'as `{JOB_NAME_LABEL}`. A failed sandbox execution is one agent session that lost its sandbox; a '
                'failed sweep run by hand left its papers unasked for; a failed reference build left some pinned '
                'reference data unmirrored or unbuilt until it is run again, and its log names each stage that failed. '
                'Each is a one-off that no later run repairs, so '
                'the incident closes on its own a few minutes later, not when anything recovers. Failures of one Job '
                'within about ten minutes share this one incident and this one message, so list them all: the '
                'failed executions, newest first, are '
                f'`gcloud run jobs executions list --job <job> --region {region} --project {project} '
                "--filter='status.failedCount>0'`, and every one that ended in the minutes before this message and "
                "until the incident closes belongs to it. An execution's logs say why it ended, and a sandbox "
                "execution's name the session it served in a line `serving session <id>`: `gcloud logging read "
                '\'labels."run.googleapis.com/execution_name"="<execution>"\' '
                f'--project {project} --bucket {audit.LOG_BUCKET_ID} --location {region} --view _AllLogs`.'
            ),
        )

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
            subject='Themis: a service is answering 5xx',
            content=(
                'One Cloud Run service returned server errors repeatedly; the incident names it, and closes once '
                f'{_SERVER_ERROR_WINDOW_SECONDS // 60} minutes pass with no more than {_SERVER_ERRORS_PER_WINDOW}, '
                'or with no traffic. For a gRPC service '
                '(auth, evidence, sheaf) this sees transport-level failures only: an application error travels as a '
                'non-OK status in the trailers of an HTTP 200, which the server-fault policy counts instead. Its logs: '
                '`gcloud logging read \'resource.type="cloud_run_revision" '
                'AND resource.labels.service_name="<service>" AND severity>=ERROR\' '
                f'--project {project} --bucket {audit.LOG_BUCKET_ID} --location {region} --view _AllLogs --limit 20`. '
                'Runbook: `docs/runbooks/reading-a-request-trace.md`.'
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
            subject='Themis: a service is failing calls',
            content=(
                f'Calls to one gRPC service ended in {", ".join(_SERVER_FAULTS)}; the incident names the service as '
                f'`job`, and closes once {_SERVER_ERROR_WINDOW_SECONDS // 60} minutes pass with no more than '
                f'{_SERVER_ERRORS_PER_WINDOW}, or with no calls. The method and status of each failed call are '
                f'labels on `{metrics.RPC_SERVER_CALL_DURATION}`, in Metrics Explorer. Its logs: '
                '`gcloud logging read \'resource.type="cloud_run_revision" '
                'AND resource.labels.service_name="<service>" AND severity>=ERROR\' '
                f'--project {project} --bucket {audit.LOG_BUCKET_ID} --location {region} --view _AllLogs --limit 20`.'
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
            content=(
                'The auth service could not use the latest version of the ACL: it was unreadable, or it broke one of '
                f'the rules the service checks. The incident names why as `{metrics.OUTCOME_LABEL}`, and closes about '
                "ten minutes after any instance loads a version cleanly; the service's ERROR log line, which starts "
                '`acl rejected:`, names the rule. The service answers from its last good copy until the staleness '
                'limit (`themis:aclStalenessLimitSeconds`), then refuses everyone until a valid version loads; an '
                'instance that starts on a rejected version does not start at all. Runbook: '
                '`docs/runbooks/access-control.md`.'
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
            content=(
                'A run of the deploy workflow failed or was cancelled after it signed in to Google Cloud, and no '
                'deploy has succeeded since; the incident names the run as `instance`, its id and attempt, and closes '
                'about ten minutes after the next successful deploy of this environment (a run that only builds the '
                f'images does not count), or {_OUTCOME_LOOKBACK} after the failure if none comes. The run, and which '
                'step failed: `gh run view <run id> --repo populationgenomics/themis-internal`. A failure before the '
                "sign-in shows in the workflow's run list only."
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

        sql_content = pulumi.Output.format(
            'The Cloud SQL instance `{0}` is near a limit of its tier; the condition names which. The tier is the '
            "stack's `themis:sqlTier`, and a larger one is an in-place update with a restart of a few minutes "
            "(docs/design/infrastructure.md, What differs between dev and prod). Its metrics: the instance's "
            'Monitoring page in the console.',
            sql_instance,
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
            subject='Themis: the database is saturated',
            content=sql_content,
        )

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
            subject='Themis: the certificate is not renewing',
            content=(
                f'Either fewer than {_CERTIFICATE_DAYS_LEFT} days are left on the Google-managed certificate for '
                f'`{domain}`, which renews about 30 days before it expires, or `https://{domain}/` has not answered '
                'with a valid certificate for two hours; the condition says which. Both usually mean the A record no '
                'longer points at the load balancer, which also stops renewal. Its status: '
                f'`gcloud compute ssl-certificates list --project {project}`. '
                'Runbook: `docs/runbooks/fresh-environment.md`, DNS handoff.'
            ),
        )

        self.register_outputs({})
