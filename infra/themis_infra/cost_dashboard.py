"""The spend dashboard (`docs/design/cost-monitoring.md`): every producer's Anthropic spend, read from the metrics.

One Cloud Monitoring dashboard in four sections, top to bottom: the workspace total across producers, then one
section per producer — Managed Agents sessions (the exporter's gauges), the convert worker (its request-token
counter, priced by the program's table) and Claude Code in CI (its own counters). Every query is PromQL
(`cost_promql`); the dashboard holds no data and fixes no time range, so whatever range the viewer picks is drawn
from the series (Monitoring keeps 24 months). The agent picker is a template variable only the sessions section's
queries carry: the other producers' series have no agent label, and a matcher on one would blank them.

- `CostDashboard` — the dashboard resource, and its console URL.
- `dashboard_json` — the dashboard, as the JSON the Monitoring API takes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import NamedTuple

import pulumi
import pulumi_gcp as gcp

from themis_infra import claude_code_metrics, cost_metrics, cost_prices, cost_promql

# The agent filter, a template variable the sessions queries carry so the console's picker reaches PromQL. Named apart
# from the label it filters: `${…}` is variable substitution in a query and label substitution in a legend.
_FILTER_VARIABLE = 'agent_filter'
_AGENT = '${' + _FILTER_VARIABLE + '}'
_COLUMNS = 12
_HEADER_HEIGHT = 1
_ABOUT_HEIGHT = 2
_SCORECARD_HEIGHT = 4
_CHART_HEIGHT = 6
# The rolling window every per-producer chart shares; the workspace scorecards read a day, a week and a month.
_HOUR = '1h'
_DAY = '1d'

_Widget = dict[str, object]


class _Row(NamedTuple):
    widgets: Sequence[_Widget]
    height: int


class _Section(NamedTuple):
    title: str
    subtitle: str
    about: str
    rows: Sequence[_Row]


class _Series(NamedTuple):
    query: str
    legend: str


def _label(name: str) -> str:
    return '${' + name + '}'


def _header(title: str, subtitle: str) -> _Widget:
    return {'title': title, 'sectionHeader': {'subtitle': subtitle, 'dividerBelow': True}}


def _text(content: str) -> _Widget:
    return {'text': {'content': content, 'format': 'MARKDOWN'}}


def _scorecard(title: str, query: str) -> _Widget:
    return {
        'title': title,
        'scorecard': {
            'timeSeriesQuery': {'prometheusQuery': query},
            'sparkChartView': {'sparkChartType': 'SPARK_LINE'},
        },
    }


def _chart(title: str, series: Sequence[_Series], *, unit: str, stacked: bool) -> _Widget:
    return {
        'title': title,
        'xyChart': {
            'dataSets': [
                {
                    'timeSeriesQuery': {'prometheusQuery': s.query},
                    'plotType': 'STACKED_AREA' if stacked else 'LINE',
                    'legendTemplate': s.legend,
                }
                for s in series
            ],
            'yAxis': {'label': unit, 'scale': 'LINEAR'},
            'chartOptions': {'mode': 'COLOR'},
        },
    }


def _workspace(freshness_minutes: int) -> _Section:
    def spend(duration: str) -> str:
        return cost_promql.dollars(cost_promql.workspace_cents(duration, freshness_minutes=freshness_minutes))

    by_producer = [
        _Series(cost_promql.dollars(cost_promql.session_cents(_HOUR, freshness_minutes=freshness_minutes)), 'sessions'),
        _Series(cost_promql.dollars(cost_promql.convert_cents(_HOUR)), 'convert worker'),
        _Series(cost_promql.ci_dollars(_HOUR), 'CI'),
    ]
    return _Section(
        title='Workspace',
        subtitle='Every producer, at list price',
        about=(
            'Anthropic spend in USD at list price, from three producers: Managed Agents sessions (the cumulative '
            '`list_cost` the sessions API reports, authoritative), the convert worker (its direct Messages API tokens, '
            "priced by the program's list-price table) and Claude Code in CI (the USD figure Claude Code computes "
            'itself). Window figures are exact query-time rises of running totals between two samples and increases '
            'of counters; a total guards each producer to zero, so one with no data does not blank the sum.'
        ),
        rows=[
            _Row(
                [
                    _scorecard('Spend, last 24 h (USD)', spend('24h')),
                    _scorecard('Spend, last 7 d (USD)', spend('7d')),
                    _scorecard('Spend, last 30 d (USD)', spend('30d')),
                ],
                _SCORECARD_HEIGHT,
            ),
            _Row(
                [_chart('Spend per rolling hour, by producer', by_producer, unit='USD per rolling hour', stacked=True)],
                _CHART_HEIGHT,
            ),
        ],
    )


def _sessions(tick_minutes: int, freshness_minutes: int) -> _Section:
    agent = cost_metrics.AGENT_LABEL
    by_agent = [agent]
    fresh = freshness_minutes

    def by_agent_window(duration: str) -> list[_Series]:
        cents = cost_promql.session_cents(duration, _AGENT, by=by_agent, freshness_minutes=fresh)
        return [_Series(cost_promql.dollars(cents), _label(agent))]

    components = [
        _Series(cost_promql.dollars(cost_promql.session_token_cents(_HOUR, _AGENT, freshness_minutes=fresh)), 'tokens'),
        _Series(
            cost_promql.dollars(cost_promql.session_runtime_cents(_HOUR, _AGENT, freshness_minutes=fresh)), 'runtime'
        ),
        _Series(
            cost_promql.dollars(cost_promql.session_search_cents(_HOUR, _AGENT, freshness_minutes=fresh)), 'web search'
        ),
    ]
    tokens = [
        _Series(
            cost_promql.rise(
                cost_metrics.SESSION_TOKENS, _HOUR, _AGENT, by=[cost_metrics.TYPE_LABEL], freshness_minutes=fresh
            ),
            _label(cost_metrics.TYPE_LABEL),
        )
    ]
    cumulative = [
        _Series(
            cost_promql.dollars(
                cost_promql.current_total(
                    cost_metrics.SESSION_LIST_COST_CENTS, _AGENT, by=by_agent, freshness_minutes=fresh
                )
            ),
            _label(agent),
        )
    ]
    return _Section(
        title='Managed Agents sessions',
        subtitle="The exporter's gauges; the agent picker narrows this section",
        about=(
            f"The exporter's gauges, written every {tick_minutes} minutes: each agent's cumulative `list_cost`, "
            'tokens, active seconds and web searches over every session in the workspace. Window figures are exact '
            'rises of those running totals between the newest sample and the one a window earlier (a series younger '
            "than the window: since its first sample), so a deleted session shows as a negative step in its agent's "
            f'series once the series is older than the window. A read takes the newest sample within '
            f'{freshness_minutes} minutes (the freshness window), so a stale exporter shows its last known totals '
            'until the freshness alert names the fault, and no figure beyond it. The '
            f'components price runtime at {cost_prices.SESSION_RUNTIME_CENTS_PER_HOUR} ¢ per active hour and a web '
            f'search at {cost_prices.WEB_SEARCH_CENTS_PER_REQUEST} ¢ (the flat list rates); tokens are what '
            '`list_cost` leaves after those two. Which session is behind a step is a live query against the sessions '
            'API (`docs/design/cost-monitoring.md`, appendix).'
        ),
        rows=[
            _Row(
                [
                    _chart(
                        'List cost per rolling hour, by agent',
                        by_agent_window(_HOUR),
                        unit='USD per rolling hour',
                        stacked=False,
                    ),
                    _chart(
                        'List cost per rolling day, by agent',
                        by_agent_window(_DAY),
                        unit='USD per rolling day',
                        stacked=False,
                    ),
                ],
                _CHART_HEIGHT,
            ),
            _Row(
                [
                    _chart(
                        'List cost per rolling hour, by component',
                        components,
                        unit='USD per rolling hour',
                        stacked=True,
                    ),
                    _chart('Tokens per rolling hour, by type', tokens, unit='tokens per rolling hour', stacked=True),
                ],
                _CHART_HEIGHT,
            ),
            _Row(
                [_chart('Cumulative list cost, by agent', cumulative, unit='USD, cumulative', stacked=False)],
                _CHART_HEIGHT,
            ),
        ],
    )


def _convert_worker() -> _Section:
    def tokens_by(label: str) -> list[_Series]:
        return [_Series(cost_promql.increase(cost_metrics.REQUEST_TOKENS, _HOUR, by=[label]), _label(label))]

    dollars_by_model = [
        _Series(cost_promql.dollars(cost_promql.convert_cents_by_model(_HOUR)), _label(cost_metrics.MODEL_LABEL))
    ]
    return _Section(
        title='Convert worker',
        subtitle='Direct Messages API calls; dollars derived from the price table',
        about=(
            "The convert worker's request-token counter: every direct Messages API call's tokens, by model, token type "
            'and stop reason. Dollars are derived at query time: the counter priced by the table in '
            '`infra/themis_infra/cost_prices.py` (cents per million tokens by model and type, literal in the query), '
            'so a model missing from that table prices at nothing here and raises the unpriced-usage alert. A model '
            'appears in the by-model chart once it has usage in the window.'
        ),
        rows=[
            _Row(
                [
                    _chart(
                        'Tokens per rolling hour, by type',
                        tokens_by(cost_metrics.TYPE_LABEL),
                        unit='tokens per rolling hour',
                        stacked=True,
                    ),
                    _chart(
                        'Tokens per rolling hour, by model',
                        tokens_by(cost_metrics.MODEL_LABEL),
                        unit='tokens per rolling hour',
                        stacked=True,
                    ),
                ],
                _CHART_HEIGHT,
            ),
            _Row(
                [
                    _chart(
                        'Derived spend per rolling hour, by model',
                        dollars_by_model,
                        unit='USD per rolling hour, derived',
                        stacked=True,
                    ),
                    _chart(
                        'Tokens per rolling hour, by stop reason',
                        tokens_by(cost_metrics.STOP_REASON_LABEL),
                        unit='tokens per rolling hour',
                        stacked=True,
                    ),
                ],
                _CHART_HEIGHT,
            ),
        ],
    )


def _ci() -> _Section:
    workflow = claude_code_metrics.NAMESPACE_LABEL
    tokens = [
        _Series(
            cost_promql.increase(claude_code_metrics.TOKEN_USAGE, _HOUR, by=[workflow, cost_metrics.TYPE_LABEL]),
            f'{_label(workflow)}: {_label(cost_metrics.TYPE_LABEL)}',
        )
    ]
    spend = [
        _Series(
            cost_promql.ci_dollars(_HOUR, by=[workflow, cost_metrics.MODEL_LABEL]),
            f'{_label(workflow)}: {_label(cost_metrics.MODEL_LABEL)}',
        )
    ]
    return _Section(
        title='CI (Claude Code)',
        subtitle="Claude Code's own counters, from the GitHub workflows that run it",
        about=(
            "Claude Code's own OpenTelemetry counters, exported by the GitHub workflows that run it, each workflow's "
            "name as the series' `namespace` label: tokens by type and model, and cost in USD as Claude Code "
            'computes it from its own price table — not derived here, so the workspace total takes it as given, and a '
            "price the two tables disagree on shows as Claude Code's figure."
        ),
        rows=[
            _Row(
                [
                    _chart(
                        'Tokens per rolling hour, by workflow and type',
                        tokens,
                        unit='tokens per rolling hour',
                        stacked=True,
                    ),
                    _chart(
                        'Spend per rolling hour, by workflow and model',
                        spend,
                        unit="USD per rolling hour, Claude Code's figure",
                        stacked=True,
                    ),
                ],
                _CHART_HEIGHT,
            ),
        ],
    )


def _tiles(sections: Sequence[_Section]) -> list[dict[str, object]]:
    """Lay the sections out top to bottom on the grid, each row's widgets sharing its width evenly."""
    tiles: list[dict[str, object]] = []
    y = 0

    def row(widgets: Sequence[_Widget], height: int) -> None:
        nonlocal y
        if _COLUMNS % len(widgets):
            raise ValueError(f'{len(widgets)} widgets do not share {_COLUMNS} columns evenly')
        width = _COLUMNS // len(widgets)
        for i, widget in enumerate(widgets):
            tiles.append({'xPos': i * width, 'yPos': y, 'width': width, 'height': height, 'widget': widget})
        y += height

    for section in sections:
        row([_header(section.title, section.subtitle)], _HEADER_HEIGHT)
        row([_text(section.about)], _ABOUT_HEIGHT)
        for widgets, height in section.rows:
            row(widgets, height)
    return tiles


def dashboard_json(*, tick_minutes: int, freshness_minutes: int) -> str:
    """The dashboard, as the JSON the Monitoring API takes.

    Args:
        tick_minutes: The exporter's schedule (`cost.TICK_MINUTES`), as the sessions section states it.
        freshness_minutes: The freshness window the alerts page the exporter on; every read of its gauges takes the
            newest sample within it.
    """
    sections = [_workspace(freshness_minutes), _sessions(tick_minutes, freshness_minutes), _convert_worker(), _ci()]
    dashboard = {
        'displayName': 'Themis: Anthropic spend',
        # No default value: an omitted default is the wildcard, which resolves to a no-op matcher in every query.
        'dashboardFilters': [
            {
                'labelKey': cost_metrics.AGENT_LABEL,
                'filterType': 'METRIC_LABEL',
                'templateVariable': _FILTER_VARIABLE,
            }
        ],
        'mosaicLayout': {'columns': _COLUMNS, 'tiles': _tiles(sections)},
    }
    return json.dumps(dashboard, indent=2, sort_keys=True)


class CostDashboard(pulumi.ComponentResource):
    """The spend dashboard over the producers' metrics; a view, holding nothing the metrics do not.

    Attributes:
        console_url: Where the dashboard renders in the Cloud console.
    """

    def __init__(
        self, *, project: str, tick_minutes: int, freshness_minutes: int, opts: pulumi.ResourceOptions | None = None
    ) -> None:
        super().__init__('themis:infra:CostDashboard', 'themis', None, opts)
        dashboard = gcp.monitoring.Dashboard(
            'themis-cost-dashboard',
            project=project,
            dashboard_json=dashboard_json(tick_minutes=tick_minutes, freshness_minutes=freshness_minutes),
            opts=pulumi.ResourceOptions(parent=self),
        )
        # The resource id is `projects/<project>/dashboards/<id>`; the console addresses the dashboard by the id alone.
        self.console_url = dashboard.id.apply(
            lambda resource_id: (
                f'https://console.cloud.google.com/monitoring/dashboards/builder/'
                f'{resource_id.rsplit("/", 1)[-1]}?project={project}'
            )
        )
        self.register_outputs({'console_url': self.console_url})
