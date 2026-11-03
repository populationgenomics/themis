"""Operational alerting: the environment's alerts channel and the policies that page it.

The failures a curator would otherwise find first (docs/design/infrastructure.md, Alerting): a Job execution that
failed, a service answering 5xx, a saturated database, a certificate that stopped renewing, a gRPC call ending in a
server fault, a version of the ACL the auth service refused, and a deploy that failed. Every policy posts to the alerts
channel, which is separate from the spend monitor's channel (`cost_alerts.CostAlerts`). The last three read metrics
the services and the deploy write through OpenTelemetry (`operational_metrics`); the rest read the platform's.

- `failed_execution_condition` — a failed execution of one Cloud Run Job; the cost report's own failure alert uses it.
- `OperationalAlerts` — the alerts channel and the operational policies.
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi
import pulumi_gcp as gcp

from themis_infra import audit, promql
from themis_infra import operational_metrics as metrics

# Written once per execution as it ends, labelled with its result.
COMPLETED_EXECUTIONS = 'run.googleapis.com/job/completed_execution_count'
# The count is written when an execution ends, so the window only sets how long an incident stays open, and so how
# long a repeat failure stays inside the same incident rather than paging again.
FAILURE_WINDOW_SECONDS = 3600

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
# The services export every minute, so a PromQL policy has nothing newer to read between.
_PROMQL_EVALUATION = '60s'
_ACL_WINDOW = '1h'
_DEPLOY_WINDOW = '10m'
# A managed certificate renews about 30 days before it expires; still under this many days means renewal is failing.
_CERTIFICATE_DAYS_LEFT = 14
# A check failing this long means the hostname no longer reaches the load balancer with a valid certificate; longer
# than a fresh certificate takes to provision, so a bring-up does not page.
_CERTIFICATE_UNREACHABLE = '7200s'
# Uptime checks run from several regions; more than one failing rules out a single checker's fault.
_CHECK_REGIONS_FAILING = 1


def _promql(query: str) -> gcp.monitoring.AlertPolicyConditionConditionPrometheusQueryLanguageArgs:
    return gcp.monitoring.AlertPolicyConditionConditionPrometheusQueryLanguageArgs(
        query=query,
        evaluation_interval=_PROMQL_EVALUATION,
        duration='0s',
        # The policies are declared with the producers, before the first point on a fresh environment.
        disable_metric_validation=True,
    )


def failed_execution_condition(job_name: pulumi.Input[str]) -> gcp.monitoring.AlertPolicyConditionArgs:
    """A condition that fires when an execution of a Cloud Run Job ends failed, once per failure."""
    return gcp.monitoring.AlertPolicyConditionArgs(
        display_name=pulumi.Output.format(
            'A {0} execution failed in the last {1} min', job_name, FAILURE_WINDOW_SECONDS // 60
        ),
        condition_threshold=gcp.monitoring.AlertPolicyConditionConditionThresholdArgs(
            filter=pulumi.Output.format(
                'metric.type="{0}" AND resource.type="cloud_run_job" AND resource.labels.job_name="{1}"'
                ' AND metric.labels.result="failed"',
                COMPLETED_EXECUTIONS,
                job_name,
            ),
            comparison='COMPARISON_GT',
            threshold_value=0,
            # A missing-data setting is refused with a zero duration, and Monitoring takes whole minutes; one minute
            # inside an hour-long aligned window changes nothing about when a failure pages.
            duration='60s',
            aggregations=[
                gcp.monitoring.AlertPolicyConditionConditionThresholdAggregationArgs(
                    alignment_period=f'{FAILURE_WINDOW_SECONDS}s',
                    per_series_aligner='ALIGN_SUM',
                    cross_series_reducer='REDUCE_SUM',
                )
            ],
            # The count is written only when an execution ends, so the series is empty most of the time; an empty
            # window closes the incident, and the next failure opens, and pages, a new one.
            evaluation_missing_data='EVALUATION_MISSING_DATA_INACTIVE',
        ),
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
        jobs: Sequence[pulumi.Input[str]],
        services: Sequence[pulumi.Input[str]],
        sql_instance: pulumi.Input[str],
        sql_tier_connections: int,
        domain: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the alerts channel and the operational policies.

        Args:
            project: The project the policies watch.
            region: Where the project's regional log bucket is.
            slack_channel: The alerts channel's name, `#` included.
            slack_bot_token: The bot token of the environment's Slack app, invited to the channel; a secret.
            jobs: The Cloud Run Jobs whose failed executions page.
            services: The Cloud Run services whose 5xx responses page.
            sql_instance: The Cloud SQL instance's name.
            sql_tier_connections: The connection limit of the instance's tier (`sql.max_connections`).
            domain: The hostname the load balancer's certificate covers.
            opts: Resource options (dependency wiring).
        """
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
            subject: str,
            content: pulumi.Input[str],
            alert_strategy: gcp.monitoring.AlertPolicyAlertStrategyArgs | None = None,
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
                alert_strategy=alert_strategy
                or gcp.monitoring.AlertPolicyAlertStrategyArgs(notification_prompts=['OPENED']),
                documentation=gcp.monitoring.AlertPolicyDocumentationArgs(
                    mime_type='text/markdown', subject=subject, content=content
                ),
                opts=pulumi.ResourceOptions(parent=self, aliases=list(aliases)),
            )

        policy(
            'themis-job-failed',
            display_name='Themis: a Job execution failed',
            conditions=[failed_execution_condition(job) for job in jobs],
            subject='Themis: a Job execution failed',
            content=(
                'An execution of a Cloud Run Job ended failed; the condition names the Job. A failed sandbox execution '
                'is a session whose agent lost its sandbox; a failed gene-disease refresh leaves the evidence service '
                "on last week's dumps. The executions and their logs: `gcloud run jobs executions list --job <job> "
                f'--project {project}`.'
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
            subject='Themis: a service is answering 5xx',
            content=(
                'One Cloud Run service returned server errors repeatedly; the incident names it. For a gRPC service '
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
                        f'sum by (job) (increase({faults}[{window}])) > {_SERVER_ERRORS_PER_WINDOW}'
                    ),
                )
            ],
            subject='Themis: a service is failing calls',
            content=(
                f'Calls to one gRPC service ended in {", ".join(_SERVER_FAULTS)}; the incident names the service as '
                f'`job`. The method and status of each failed call are labels on `{metrics.RPC_SERVER_CALL_DURATION}`, '
                f'in Metrics Explorer. Its logs: `gcloud logging read \'resource.type="cloud_run_revision" '
                'AND resource.labels.service_name="<service>" AND severity>=ERROR\' '
                f'--project {project} --bucket {audit.LOG_BUCKET_ID} --location {region} --view _AllLogs --limit 20`.'
            ),
        )

        policy(
            'themis-auth-acl-rejected',
            display_name='Themis auth: ACL rejected',
            conditions=[
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name='The auth service rejected the ACL',
                    condition_prometheus_query_language=_promql(
                        f'sum(increase({metrics.ACL_REJECTIONS}[{_ACL_WINDOW}])) > 0'
                    ),
                )
            ],
            subject='Themis: the auth service rejected the ACL',
            content=(
                'The auth service could not use the latest version of the ACL: it was unreadable, or it broke one of '
                f'the rules the service checks. `{metrics.ACL_REJECTIONS}` counts it by `{metrics.REASON_LABEL}`, and '
                "the service's ERROR log line, which starts `acl rejected:`, names the rule. The service answers "
                'from its last good copy until the staleness limit (`themis:aclStalenessLimitSeconds`), then refuses '
                'everyone until a valid version loads. Runbook: `docs/runbooks/access-control.md`.'
            ),
            # The policy predates the alerts component, as a top-level resource of the same name.
            aliases=[pulumi.Alias(parent=None)],
        )

        policy(
            'themis-deploy-failed',
            display_name='Themis: a deploy failed',
            conditions=[
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name='A deploy run failed or was cancelled',
                    condition_prometheus_query_language=_promql(
                        f'max by (instance) (max_over_time({metrics.DEPLOY_FAILED}[{_DEPLOY_WINDOW}])) > 0'
                    ),
                )
            ],
            subject='Themis: a deploy failed',
            content=(
                'A run of the deploy workflow failed or was cancelled after it signed in to Google Cloud; the incident '
                'names the run as `instance`, its id and attempt. The run, and which step failed: '
                '`gh run view <run id> --repo populationgenomics/themis-internal`. A failure before the sign-in shows '
                "in the workflow's run list only."
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
