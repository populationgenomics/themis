"""The morning spend report (`docs/design/cost-monitoring.md`): the trailing day's spend per producer, posted to Slack.

A second Cloud Run Job from the exporter's image, run daily and reading rather than writing: it evaluates the report's
PromQL against Monitoring's Prometheus endpoint, draws a chart of the last 24 hours, and posts both to the spend
channel through the Slack app's bot token, only when spend occurred. The Job holds one Monitoring grant (read) and one
secret (the token), and none of the exporter's Anthropic identifiers.

The queries reach the report as JSON the program renders into the Job's environment, so the program stays the one
source of PromQL over the spend metrics (`cost_promql`). The shape is fixed and flat — for the day, one query per
producer figure, the sessions split included; for the hourly chart, one per producer — and every query yields cents,
which the report divides for display. The report parses the rendering strictly, and its own tests round-trip it.

A morning the report posts nothing on is a quiet one or a failed one, and the channel cannot tell them apart, so a
failed execution pages: a policy on Cloud Run's own execution count notifies the spend monitor's Slack channel.

- `CostReport` — the report Job, its schedule, its identity and grants, the token secret, and the failure policy.
- `report_json` — the queries as the report reads them.
"""

from __future__ import annotations

import json

import pulumi
import pulumi_gcp as gcp

from themis_infra import cost_promql, grants

JOB_NAME = 'themis-cost-report'
# Every morning, Sydney time; the window is the trailing 24 hours from the run.
_SCHEDULE = '0 8 * * *'
_TIME_ZONE = 'Australia/Sydney'
# Nine PromQL queries and one Slack upload. The deadline is checked between calls, so the Job's timeout margin
# covers the last call in full: an upload is up to three requests of 30 seconds each.
_RUN_DEADLINE_SECONDS = 120
_JOB_TIMEOUT_MARGIN_SECONDS = 120
# The day's figures are read at one instant over this window; the chart's series over this one, hour by hour.
_DAY = '24h'
_HOUR = '1h'
# Cloud Run's count of finished executions by `result`, a delta written as each execution ends; summed over a window
# that covers one run and its timeout.
_COMPLETED_EXECUTIONS = 'run.googleapis.com/job/completed_execution_count'
_FAILURE_WINDOW_SECONDS = 3600


def report_queries() -> dict[str, dict[str, str]]:
    """The report's PromQL, every figure in cents.

    `day` is the trailing day's figure per producer, evaluated at one instant: Managed Agents list cost and its
    runtime, web-search and token shares, the convert worker's spend derived from the price table, and Claude Code's
    own CI figure. `hourly` is the trailing hour's figure per producer, evaluated as a range at hourly steps, for
    the chart.
    """
    return {
        'day': {
            'sessions': cost_promql.session_cents(_DAY),
            'sessions_runtime': cost_promql.session_runtime_cents(_DAY),
            'sessions_search': cost_promql.session_search_cents(_DAY),
            'sessions_tokens': cost_promql.session_token_cents(_DAY),
            'convert': cost_promql.convert_cents(_DAY),
            'ci': cost_promql.ci_cents(_DAY),
        },
        'hourly': {
            'sessions': cost_promql.session_cents(_HOUR),
            'convert': cost_promql.convert_cents(_HOUR),
            'ci': cost_promql.ci_cents(_HOUR),
        },
    }


def report_json() -> str:
    """`report_queries`, as the JSON the Job's environment carries."""
    return json.dumps(report_queries(), sort_keys=True)


def _env(name: str, value: pulumi.Input[str]) -> gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs:
    return gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs(name=name, value=value)


def _secret_env(name: str, secret_id: pulumi.Input[str]) -> gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs:
    return gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs(
        name=name,
        value_source=gcp.cloudrunv2.JobTemplateTemplateContainerEnvValueSourceArgs(
            secret_key_ref=gcp.cloudrunv2.JobTemplateTemplateContainerEnvValueSourceSecretKeyRefArgs(
                secret=secret_id, version='latest'
            ),
        ),
    )


class CostReport(pulumi.ComponentResource):
    """The morning report: a viewer-only Job on a daily schedule, the Slack token it posts with, its failure page."""

    def __init__(
        self,
        *,
        project: str,
        region: str,
        image: pulumi.Input[str],
        slack_channel_id: str,
        slack_bot_token: pulumi.Input[str],
        slack_notification_channel: pulumi.Input[str],
        dashboard_url: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the Job, its schedule, its identity, the secret and the failure policy.

        Args:
            project: The GCP project the metrics live in.
            region: The Job's region.
            image: The exporter image; the report is another entrypoint in it.
            slack_channel_id: The Slack channel the report posts to, by id (a file upload addresses a channel by id).
            slack_bot_token: The bot token of the Slack app that posts; a secret, kept in Secret Manager for the Job.
            slack_notification_channel: The spend monitor's Monitoring notification channel
                (`cost_alerts.CostAlerts.notification_channel_name`), which a failed execution pages.
            dashboard_url: The spend dashboard's console URL, linked from the report's message and from the page.
            opts: Resource options (dependency wiring).
        """
        super().__init__('themis:infra:CostReport', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            'themis-cost-report-runtime',
            project=project,
            account_id='themis-cost-report',
            display_name='Themis cost report runtime (the morning spend report)',
            opts=child,
        )
        grants.MetricReader('themis-cost-report', member=service_account.member, project=project, opts=child)

        secret = gcp.secretmanager.Secret(
            'themis-slack-bot-token',
            project=project,
            secret_id='slack-bot-token',  # noqa: S106 — the secret's name, not its value
            replication=gcp.secretmanager.SecretReplicationArgs(
                user_managed=gcp.secretmanager.SecretReplicationUserManagedArgs(
                    replicas=[gcp.secretmanager.SecretReplicationUserManagedReplicaArgs(location=region)],
                ),
            ),
            opts=child,
        )
        version = gcp.secretmanager.SecretVersion(
            'themis-slack-bot-token-current',
            secret=secret.id,
            secret_data=slack_bot_token,
            opts=pulumi.ResourceOptions(parent=secret),
        )
        reader = grants.SecretReader(
            'themis-cost-report',
            member=service_account.member,
            secret=secret.secret_id,
            project=project,
            target='slack-bot-token',
            opts=child,
        )

        job = gcp.cloudrunv2.Job(
            JOB_NAME,
            project=project,
            location=region,
            name=JOB_NAME,
            deletion_protection=False,
            template=gcp.cloudrunv2.JobTemplateArgs(
                template=gcp.cloudrunv2.JobTemplateTemplateArgs(
                    service_account=service_account.email,
                    timeout=f'{_RUN_DEADLINE_SECONDS + _JOB_TIMEOUT_MARGIN_SECONDS}s',
                    # Not retried: a retry after a post whose completion response was lost could post twice, and a
                    # lost morning pages (`themis-cost-report-failed` below); the next morning stands alone.
                    max_retries=0,
                    containers=[
                        gcp.cloudrunv2.JobTemplateTemplateContainerArgs(
                            name='report',
                            image=image,
                            commands=['python', '-m', 'themis.services.cost_exporter.report'],
                            envs=[
                                _env('THEMIS_COST_REPORT_QUERIES', report_json()),
                                _env('THEMIS_COST_REPORT_PROJECT', project),
                                _env('THEMIS_COST_REPORT_SLACK_CHANNEL_ID', slack_channel_id),
                                _env('THEMIS_COST_REPORT_DASHBOARD_URL', dashboard_url),
                                _env('THEMIS_COST_REPORT_DEADLINE_SECONDS', str(_RUN_DEADLINE_SECONDS)),
                                # The schedule's zone, so the report dates its window as the reader's morning.
                                _env('THEMIS_COST_REPORT_TIME_ZONE', _TIME_ZONE),
                                _secret_env('THEMIS_COST_REPORT_SLACK_BOT_TOKEN', secret.secret_id),
                            ],
                            resources=gcp.cloudrunv2.JobTemplateTemplateContainerResourcesArgs(
                                limits={'cpu': '1', 'memory': '512Mi'}
                            ),
                        ),
                    ],
                ),
            ),
            # The secret's version and the Job's read of it precede the Job, which references `latest`.
            opts=pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(depends_on=[version, reader])),
        )

        scheduler_account = gcp.serviceaccount.Account(
            'themis-cost-report-scheduler',
            project=project,
            account_id='themis-cost-report-sched',
            display_name='Themis cost report scheduler',
            opts=child,
        )
        grants.JobRunner(
            'themis-cost-report-scheduler',
            member=scheduler_account.member,
            job=job.name,
            project=project,
            location=region,
            target='cost-report',
            opts=child,
        )
        gcp.cloudscheduler.Job(
            'themis-cost-report-schedule',
            project=project,
            region=region,
            name=JOB_NAME,
            schedule=_SCHEDULE,
            time_zone=_TIME_ZONE,
            http_target=gcp.cloudscheduler.JobHttpTargetArgs(
                uri=pulumi.Output.format(
                    'https://run.googleapis.com/v2/projects/{0}/locations/{1}/jobs/{2}:run', project, region, job.name
                ),
                http_method='POST',
                oauth_token=gcp.cloudscheduler.JobHttpTargetOauthTokenArgs(
                    service_account_email=scheduler_account.email,
                    scope='https://www.googleapis.com/auth/cloud-platform',
                ),
            ),
            opts=child,
        )

        gcp.monitoring.AlertPolicy(
            'themis-cost-report-failed',
            project=project,
            display_name='Themis cost: morning report failed',
            combiner='OR',
            severity='ERROR',
            conditions=[
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name=f'A {JOB_NAME} execution failed in the last {_FAILURE_WINDOW_SECONDS // 60} min',
                    condition_threshold=gcp.monitoring.AlertPolicyConditionConditionThresholdArgs(
                        filter=(
                            f'metric.type="{_COMPLETED_EXECUTIONS}" AND resource.type="cloud_run_job"'
                            f' AND resource.labels.job_name="{JOB_NAME}" AND metric.labels.result="failed"'
                        ),
                        comparison='COMPARISON_GT',
                        threshold_value=0,
                        # A missing-data setting is refused with a zero duration, and Monitoring takes whole minutes;
                        # one minute inside an hour-long aligned window changes nothing about when a failure pages.
                        duration='60s',
                        aggregations=[
                            gcp.monitoring.AlertPolicyConditionConditionThresholdAggregationArgs(
                                alignment_period=f'{_FAILURE_WINDOW_SECONDS}s',
                                per_series_aligner='ALIGN_SUM',
                                cross_series_reducer='REDUCE_SUM',
                            )
                        ],
                        # The count is written only when an execution ends, so the series is empty most of the day; an
                        # empty window closes the incident, and the next failure opens — and pages — a new one.
                        evaluation_missing_data='EVALUATION_MISSING_DATA_INACTIVE',
                    ),
                )
            ],
            notification_channels=[slack_notification_channel],
            alert_strategy=gcp.monitoring.AlertPolicyAlertStrategyArgs(notification_prompts=['OPENED']),
            documentation=gcp.monitoring.AlertPolicyDocumentationArgs(
                mime_type='text/markdown',
                subject='Themis: the morning spend report failed',
                content=(
                    f'An execution of the `{JOB_NAME}` Job failed, so this morning has no report in the channel — the '
                    f'same silence as a morning with nothing to report, which is why the failure pages. The '
                    f'execution and its logs: `gcloud run jobs executions list --job {JOB_NAME} --region {region} '
                    f"--project {project}`. The dashboard is unaffected; the day's figures are there. The run is "
                    f'not retried; the next morning runs on its own.'
                ),
                links=[
                    gcp.monitoring.AlertPolicyDocumentationLinkArgs(display_name='Spend dashboard', url=dashboard_url)
                ],
            ),
            opts=child,
        )
        self.register_outputs({})
