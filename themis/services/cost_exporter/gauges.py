"""The exporter's gauges: each agent's cumulative usage and the heartbeat the freshness alert watches, set per run.

Gauges of running totals, not counters: deleting a session steps its agent's total down, which a gauge
shows truthfully as a dip and a counter's reset semantics would misread as a restart
(`docs/design/cost-monitoring.md`, Design). Deltas and rates are the dashboard's and the alert
policies' queries over these series, never the exporter's arithmetic. The heartbeat is the one series
every successful run writes, sessions or none, so a silent exporter shows as its absence; and it means
the run's export was accepted as a whole, since gauges and heartbeat go in one request and the OTLP
exporter does not inspect a partial-success response. Setting a gauge exports nothing; the entrypoint
flushes the whole set once the run is complete.
"""

from __future__ import annotations

import abc
import time
from collections.abc import Callable, Mapping
from typing import override

from opentelemetry import metrics

from themis.services.cost_exporter import sessions
from themis.telemetry import names


class SpendGauges(abc.ABC):
    """The per-agent gauges and the heartbeat: one write per run carries every agent's totals."""

    @abc.abstractmethod
    def write(self, totals: Mapping[str, sessions.AgentTotals]) -> None:
        """Set every agent's gauges from its totals, then the heartbeat to now."""


class TelemetryGauges(SpendGauges):
    """The live gauges, on one meter of the provider the entrypoint flushes."""

    def __init__(self, meter: metrics.Meter, *, clock: Callable[[], float] = time.time) -> None:
        """Create the instruments on `meter`.

        Args:
            meter: The meter the gauges are created on.
            clock: Returns the Unix time the heartbeat is set to; the system clock outside tests.
        """
        self._clock = clock
        self._list_cost = meter.create_gauge(
            names.SESSION_LIST_COST_CENTS,
            description='Cumulative Anthropic list cost of every Managed Agents session, in USD cents, by agent.',
        )
        self._tokens = meter.create_gauge(
            names.SESSION_TOKENS,
            description='Cumulative tokens of every Managed Agents session, by agent and token type.',
        )
        self._active_seconds = meter.create_gauge(
            names.SESSION_ACTIVE_SECONDS,
            description=(
                'Cumulative active seconds of every Managed Agents session — what runtime is priced on — by agent.'
            ),
        )
        self._web_search_requests = meter.create_gauge(
            names.SESSION_WEB_SEARCH_REQUESTS,
            description='Cumulative server-side web searches of every Managed Agents session, by agent.',
        )
        self._last_success = meter.create_gauge(
            names.EXPORTER_LAST_SUCCESS_TIMESTAMP_SECONDS,
            description="Unix time of the cost exporter's last successful run; what the freshness alert watches.",
        )

    @override
    def write(self, totals: Mapping[str, sessions.AgentTotals]) -> None:
        for agent, agent_totals in totals.items():
            self._list_cost.set(agent_totals.cents, {names.AGENT_LABEL: agent})
            for token_type, count in agent_totals.tokens.items():
                self._tokens.set(count, {names.AGENT_LABEL: agent, names.TYPE_LABEL: token_type.value})
            self._active_seconds.set(agent_totals.active_seconds, {names.AGENT_LABEL: agent})
            self._web_search_requests.set(agent_totals.web_search_requests, {names.AGENT_LABEL: agent})
        self._last_success.set(self._clock())
