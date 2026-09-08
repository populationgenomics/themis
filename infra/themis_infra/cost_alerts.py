"""The spend alerts (`docs/design/cost-monitoring.md`): what Cloud Monitoring watches on the spend metrics for us.

Three PromQL policies, notifying one Slack channel through Monitoring's incident lifecycle. The spend spike is the
workspace total across every producer (`cost_promql.workspace_cents`) against a threshold: Monitoring's threshold
conditions take no window delta of a gauge, so the figure is the dashboard's own query. Freshness is
`absent_over_time` of the exporter's heartbeat — the gauge every successful run sets once, sessions in the workspace
or none, so its silence covers every way the exporter can die without a healthy exporter over an empty workspace
reading as dead; the counters are silent whenever their callers are idle, by design, and get no freshness policy.
Unpriced usage is the convert worker reporting tokens for a model the program's price table (`cost_prices`) has no
row for — spend the dashboard shows as nothing — matched statically against the table's model ids. PromQL
throughout, rather than Monitoring's absence condition for freshness,
because only a PromQL condition can be declared before its metric exists: a fresh environment's first `pulumi up`
runs before the first point. For the same reason the freshness condition must hold for one tick before it fires:
`absent_over_time` over a metric that has never existed is 1, so at a zero duration the policy would open at its first
evaluation after that `pulumi up`, before the exporter's first run; a silent exporter is therefore detected one tick
after the freshness window. A PromQL incident ignores `auto_close`: Monitoring closes it the larger of 270 s and twice
the evaluation interval after the condition was last met, so the freshness incident stays open while the exporter is
silent and closes about ten minutes after the first returning point.

- `CostAlerts` — the Slack notification channel and the three alert policies.
- `check_tuning` — the windows the conditions can evaluate.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import cost_metrics, cost_promql

_DAY_SECONDS = 86_400
# Long enough that a whole transcription's worth of calls lands inside one window, short enough that the incident
# closes on its own within the day the table gets its row.
_UNPRICED_WINDOW = '1h'


def check_tuning(*, spike_window_minutes: int, freshness_minutes: int, tick_minutes: int) -> None:
    """Refuse tuning the conditions cannot evaluate against gauges written every `tick_minutes`.

    Raises:
        ValueError: The spike window holds fewer than two samples (a rise needs one at each end), the freshness
            window is under two missed ticks (one is scheduler jitter), or the spike window does not exceed the
            freshness window, which every read of a gauge tolerates as staleness — a rise's two ends could then
            read one sample.
    """
    if spike_window_minutes < 2 * tick_minutes:
        raise ValueError(
            f'a {spike_window_minutes}-minute spike window holds fewer than two samples at a {tick_minutes}-minute tick'
        )
    if freshness_minutes <= 2 * tick_minutes:
        raise ValueError(
            f'a {freshness_minutes}-minute freshness window is under two missed ticks at a {tick_minutes}-minute tick'
        )
    if spike_window_minutes <= freshness_minutes:
        raise ValueError(
            f'a {spike_window_minutes}-minute spike window does not exceed the {freshness_minutes}-minute freshness '
            f'window; a rise over it could read one sample at both ends'
        )


class CostAlerts(pulumi.ComponentResource):
    """The spend monitor's alerting: a Slack channel, and the spike, freshness and unpriced-usage policies.

    Attributes:
        notification_channel_name: The Slack notification channel's resource name, for a policy declared elsewhere.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        slack_channel: str,
        slack_bot_token: pulumi.Input[str],
        spike_cents: int,
        spike_window_minutes: int,
        freshness_minutes: int,
        tick_minutes: int,
        exporter_job_name: pulumi.Input[str],
        dashboard_url: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the channel and the policies.

        Args:
            project: The GCP project the metrics live in.
            region: The exporter Job's region, for the freshness runbook line.
            slack_channel: The Slack channel notifications post to (`#name`).
            slack_bot_token: The bot token of the Slack app that posts there; a secret.
            spike_cents: Workspace spend rising by this many cents within the window is a spike; the sessions share
                is the exact rise between the newest sample and the newest one a window earlier.
            spike_window_minutes: The spike's rolling window.
            freshness_minutes: How long the exporter's heartbeat may go unwritten before the exporter counts as dead,
                and so how long every read of its gauges takes the last sample as current.
            tick_minutes: The exporter's schedule (`cost.TICK_MINUTES`); the conditions evaluate at its cadence.
            exporter_job_name: The exporter's Cloud Run Job, named in the freshness message.
            dashboard_url: The spend dashboard's console URL, linked from every notification.
            opts: Resource options (dependency wiring).

        Raises:
            ValueError: See `check_tuning`.
        """
        check_tuning(
            spike_window_minutes=spike_window_minutes, freshness_minutes=freshness_minutes, tick_minutes=tick_minutes
        )
        super().__init__('themis:infra:CostAlerts', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)
        tick = f'{tick_minutes * 60}s'

        channel = gcp.monitoring.NotificationChannel(
            'themis-cost-slack',
            project=project,
            type='slack',
            display_name=f'Themis cost: Slack {slack_channel}',
            labels={'channel_name': slack_channel},
            sensitive_labels=gcp.monitoring.NotificationChannelSensitiveLabelsArgs(auth_token=slack_bot_token),
            opts=child,
        )
        self.notification_channel_name = channel.name
        dashboard_link = gcp.monitoring.AlertPolicyDocumentationLinkArgs(
            display_name='Spend dashboard', url=dashboard_url
        )

        def promql(
            query: str, *, duration: str
        ) -> gcp.monitoring.AlertPolicyConditionConditionPrometheusQueryLanguageArgs:
            return gcp.monitoring.AlertPolicyConditionConditionPrometheusQueryLanguageArgs(
                query=query,
                # Evaluated at the exporter's tick: its gauges change no faster.
                evaluation_interval=tick,
                duration=duration,
                # The policies are declared with the producers, before the first point on a fresh environment.
                disable_metric_validation=True,
            )

        spike_dollars = f'${spike_cents / 100:.2f}'
        workspace_cents = cost_promql.workspace_cents(f'{spike_window_minutes}m', freshness_minutes=freshness_minutes)
        gcp.monitoring.AlertPolicy(
            'themis-cost-spike',
            project=project,
            display_name='Themis cost: spend spike',
            combiner='OR',
            severity='WARNING',
            conditions=[
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name=f'Workspace spend up more than {spike_cents} cents in {spike_window_minutes} min',
                    condition_prometheus_query_language=promql(f'{workspace_cents} > {spike_cents}', duration='0s'),
                )
            ],
            notification_channels=[channel.name],
            # One message per spike: the incident closes on its own once the window rolls past, which is no news.
            alert_strategy=gcp.monitoring.AlertPolicyAlertStrategyArgs(notification_prompts=['OPENED']),
            documentation=gcp.monitoring.AlertPolicyDocumentationArgs(
                mime_type='text/markdown',
                subject=f'Themis: workspace spend up more than {spike_dollars} in {spike_window_minutes} min',
                content=(
                    f'Anthropic spend across the workspace — Managed Agents sessions, the convert worker and Claude '
                    f'Code in CI, at list price — rose by more than {spike_dollars} in the last {spike_window_minutes} '
                    f'minutes (`themis:costSpikeAlertCents`, `themis:costSpikeWindowMinutes`). The dashboard shows '
                    f'how much, and which producer and agent; a session behind it is a live query against the '
                    f'sessions API (`docs/design/cost-monitoring.md`, appendix).'
                ),
                links=[dashboard_link],
            ),
            opts=child,
        )

        gcp.monitoring.AlertPolicy(
            'themis-cost-exporter-freshness',
            project=project,
            display_name='Themis cost: exporter silent',
            combiner='OR',
            severity='ERROR',
            conditions=[
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name=f'No exporter heartbeat for {freshness_minutes} min',
                    condition_prometheus_query_language=promql(
                        cost_promql.silence(
                            cost_metrics.EXPORTER_LAST_SUCCESS_TIMESTAMP_SECONDS, f'{freshness_minutes}m'
                        ),
                        # One tick, so a fresh environment's first run lands before the never-existed metric fires it.
                        duration=tick,
                    ),
                )
            ],
            notification_channels=[channel.name],
            # Recovery is news, so the close notifies; while the exporter stays silent the incident is re-notified once
            # a day. No auto-close: a PromQL incident closes on its own once the condition stops holding.
            alert_strategy=gcp.monitoring.AlertPolicyAlertStrategyArgs(
                notification_prompts=['OPENED', 'CLOSED'],
                notification_channel_strategies=[
                    gcp.monitoring.AlertPolicyAlertStrategyNotificationChannelStrategyArgs(
                        notification_channel_names=[channel.name],
                        renotify_interval=f'{_DAY_SECONDS}s',
                    )
                ],
            ),
            documentation=gcp.monitoring.AlertPolicyDocumentationArgs(
                mime_type='text/markdown',
                subject=f'Themis: cost exporter silent for {freshness_minutes} min',
                content=pulumi.Output.from_input(exporter_job_name).apply(
                    lambda job: (
                        f'No heartbeat has been written for {freshness_minutes} minutes. Every successful run of '
                        f'the `{job}` Job sets it, sessions in the workspace or none, so the exporter is not '
                        f'running or is failing before its write — its token exchange, or the '
                        f"sessions listing. Until it recovers the dashboard's session figures are stale and the spend "
                        f'alert is blind to sessions. Its executions: `gcloud run jobs executions list --job {job} '
                        f'--region {region} --project {project}`. This incident closes only once a point returns, '
                        f'within about ten minutes.'
                    )
                ),
                links=[dashboard_link],
            ),
            opts=child,
        )

        gcp.monitoring.AlertPolicy(
            'themis-cost-unpriced-usage',
            project=project,
            display_name='Themis cost: unpriced convert usage',
            combiner='OR',
            severity='WARNING',
            conditions=[
                gcp.monitoring.AlertPolicyConditionArgs(
                    display_name=f'Request tokens in the last {_UNPRICED_WINDOW} for a model the price table lacks',
                    condition_prometheus_query_language=promql(
                        f'{cost_promql.unpriced_request_tokens(_UNPRICED_WINDOW)} > 0', duration='0s'
                    ),
                )
            ],
            notification_channels=[channel.name],
            # One message per episode: the incident closes once the window rolls past the unpriced calls.
            alert_strategy=gcp.monitoring.AlertPolicyAlertStrategyArgs(notification_prompts=['OPENED']),
            documentation=gcp.monitoring.AlertPolicyDocumentationArgs(
                mime_type='text/markdown',
                subject='Themis: convert worker usage on a model the price table lacks',
                content=(
                    f'The convert worker reported request tokens for a model the list-price table in '
                    f'`infra/themis_infra/cost_prices.py` has no row for, so the dashboard prices that usage at '
                    f"nothing and the workspace total under-counts it. Add the model's row to the table; the "
                    f"dashboard's convert section shows which model. The incident closes on its own "
                    f'{_UNPRICED_WINDOW} after the last unpriced call, priced or not.'
                ),
                links=[dashboard_link],
            ),
            opts=child,
        )
        self.register_outputs({'notification_channel_name': self.notification_channel_name})
