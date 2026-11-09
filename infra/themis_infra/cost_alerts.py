"""The spend alerts (`docs/design/cost-monitoring.md`): what Cloud Monitoring watches on the spend metrics for us.

Three PromQL policies, notifying one Slack channel as each incident opens and as it closes: each is a state, open
while the condition holds and closed once it does not. The spend spike is the
workspace total across every producer (`cost_promql.workspace_cents`) against a threshold: Monitoring's threshold
conditions take no window delta of a gauge, so the figure is the dashboard's own query. Freshness is
`absent_over_time` of the exporter's heartbeat — the gauge every successful run sets once, sessions in the workspace
or none, so its silence covers every way the exporter can die without a healthy exporter over an empty workspace
reading as dead; the counters are silent whenever their callers are idle, by design, and get no freshness policy.
Unpriced usage is the convert worker reporting tokens for a model the program's price table (`cost_prices`) has no
row for — spend the dashboard shows as nothing — matched statically against the table's model ids, over a week, so
the incident closes when a deploy adds the row rather than when the worker falls idle. PromQL
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

import datetime

import pulumi
import pulumi_gcp as gcp

from themis_infra import alerts, cost_metrics, cost_promql, promql

_DAY_SECONDS = 86_400
# Longer than the worker sits idle between batches, so the incident stays open until a deploy adds the model's row
# (the matcher is the table's) rather than closing once the unpriced calls stop.
_UNPRICED_WINDOW = '7d'
_DRILL_DOWN_URL = alerts.repository_url('docs/design/cost-monitoring.md') + '#appendix-per-session-drill-down'
_PRICE_TABLE_URL = alerts.repository_url('infra/themis_infra/cost_prices.py')


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
            region: The exporter Job's region, for the freshness page's links.
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

        def condition(
            query: str, *, lookback: datetime.timedelta, duration: str
        ) -> gcp.monitoring.AlertPolicyConditionConditionPrometheusQueryLanguageArgs:
            # At the exporter's tick, since its gauges change no faster, unless the lookback needs a longer interval.
            interval = max(datetime.timedelta(minutes=tick_minutes), promql.evaluation_floor(lookback))
            if lookback > promql.RETEST_LOOKBACK_LIMIT and promql.duration(duration):
                raise ValueError(f'a query reading {lookback} back takes no retest window, got {duration}')
            return gcp.monitoring.AlertPolicyConditionConditionPrometheusQueryLanguageArgs(
                query=query,
                evaluation_interval=f'{interval.total_seconds():.0f}s',
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
                    condition_prometheus_query_language=condition(
                        f'{workspace_cents} > {spike_cents}',
                        # The window's far end, read with the freshness tolerance every gauge read takes.
                        lookback=datetime.timedelta(minutes=spike_window_minutes + freshness_minutes),
                        duration='0s',
                    ),
                )
            ],
            notification_channels=[channel.name],
            # The close says the spend rate fell back under the threshold, which ends the spike.
            alert_strategy=gcp.monitoring.AlertPolicyAlertStrategyArgs(notification_prompts=['OPENED', 'CLOSED']),
            documentation=gcp.monitoring.AlertPolicyDocumentationArgs(
                mime_type='text/markdown',
                subject=f'Themis: workspace spend up more than {spike_dollars} in {spike_window_minutes} min',
                content=pulumi.Output.from_input(dashboard_url).apply(
                    lambda dashboard: alerts.page(
                        meaning=(
                            'Anthropic spend across the workspace (Managed Agents sessions, the convert worker and '
                            f'Claude Code in CI, at list price) rose by more than {spike_dollars} in the last '
                            f'{spike_window_minutes} minutes. The threshold and the window are the stack settings '
                            'themis:costSpikeAlertCents and themis:costSpikeWindowMinutes.'
                        ),
                        steps=[
                            f'See how much, and which producer and agent, on [the spend dashboard]({dashboard}).',
                            'If the rise is in Managed Agents sessions, find the session behind it with a query '
                            f'against the sessions API: [per-session drill-down]({_DRILL_DOWN_URL}).',
                        ],
                        closes=f'Once the rise over the window is back under {spike_dollars}.',
                    )
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
                    condition_prometheus_query_language=condition(
                        cost_promql.silence(
                            cost_metrics.EXPORTER_LAST_SUCCESS_TIMESTAMP_SECONDS, f'{freshness_minutes}m'
                        ),
                        lookback=datetime.timedelta(minutes=freshness_minutes),
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
                    lambda job: alerts.page(
                        meaning=(
                            f'The cost exporter has written no heartbeat for {freshness_minutes} minutes. Every '
                            'successful run writes one, sessions in the workspace or none, so the exporter is not '
                            'running, or fails before its write: in its token exchange or the sessions listing. Until '
                            "it recovers, the dashboard's session figures are stale and the spend spike alert cannot "
                            'see sessions.'
                        ),
                        steps=[
                            "Find the failed executions on [the Job's executions page]("
                            f'{alerts.job_executions_url(project=project, region=region, job_name=job)}), and their '
                            f"lines among [the Job's logs]({alerts.job_logs_url(project=project, job_name=job)}). "
                            'No executions at all means its schedule stopped firing.',
                            "An authentication error points at the exporter's Anthropic federation: [Claude API "
                            f'workload identity]({alerts.repository_url("docs/runbooks/claude-api-wif.md")}).',
                        ],
                        closes=(
                            'About ten minutes after the first heartbeat returns. While the exporter stays silent, '
                            'this posts again once a day.'
                        ),
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
                    condition_prometheus_query_language=condition(
                        f'{cost_promql.unpriced_request_tokens(_UNPRICED_WINDOW)} > 0',
                        lookback=promql.duration(_UNPRICED_WINDOW),
                        duration='0s',
                    ),
                )
            ],
            notification_channels=[channel.name],
            # The close says the table now prices every model the worker used in the window.
            alert_strategy=gcp.monitoring.AlertPolicyAlertStrategyArgs(notification_prompts=['OPENED', 'CLOSED']),
            documentation=gcp.monitoring.AlertPolicyDocumentationArgs(
                mime_type='text/markdown',
                subject='Themis: convert worker usage on a model the price table lacks',
                content=pulumi.Output.from_input(dashboard_url).apply(
                    lambda dashboard: alerts.page(
                        meaning=(
                            'The convert worker reported request tokens for a model the list-price table has no row '
                            'for, so the dashboard prices that usage at nothing and the workspace total, which the '
                            'spend spike alert reads, under-counts it.'
                        ),
                        steps=[
                            f'See which model in the convert section of [the spend dashboard]({dashboard}).',
                            f"Add the model's row to [the price table]({_PRICE_TABLE_URL}) and deploy.",
                        ],
                        closes=(
                            'Once the deploy that adds the row lands, or '
                            f'{alerts.in_days(_UNPRICED_WINDOW)} days after the last unpriced call if none does.'
                        ),
                    )
                ),
                links=[dashboard_link],
            ),
            opts=child,
        )
        self.register_outputs({'notification_channel_name': self.notification_channel_name})
