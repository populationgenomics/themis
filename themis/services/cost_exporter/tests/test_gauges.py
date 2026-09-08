"""The gauges as they read back: one series per agent per metric, the heartbeat beside them, last set wins."""

from __future__ import annotations

import time

from opentelemetry.sdk import metrics as sdk_metrics
from opentelemetry.sdk.metrics import export as metrics_export

from themis.services.cost_exporter import gauges, sessions
from themis.telemetry import names
from themis.testing import in_memory_metrics

_NOW = 1_757_300_000.0


def _gauges(clock: float = _NOW) -> tuple[gauges.TelemetryGauges, metrics_export.InMemoryMetricReader]:
    reader = metrics_export.InMemoryMetricReader()
    provider = sdk_metrics.MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    return gauges.TelemetryGauges(provider.get_meter('test'), clock=lambda: clock), reader


def _totals(cents: int, *, tokens: dict[names.TokenType, int], seconds: float, searches: int) -> sessions.AgentTotals:
    return sessions.AgentTotals(cents=cents, tokens=tokens, active_seconds=seconds, web_search_requests=searches)


def test_each_agent_is_one_series_per_gauge_and_one_per_token_type() -> None:
    sink, reader = _gauges()
    tokens = {
        names.TokenType.INPUT: 10,
        names.TokenType.OUTPUT: 20,
        names.TokenType.CACHE_READ: 30,
        names.TokenType.CACHE_CREATION: 40,
    }

    sink.write(
        {
            'classifier': _totals(1852, tokens=tokens, seconds=90.5, searches=3),
            'curator': _totals(100, tokens=dict.fromkeys(names.TokenType, 0), seconds=0.0, searches=0),
        }
    )

    collected = in_memory_metrics.collected(reader)
    agent = in_memory_metrics.labels
    assert collected[names.SESSION_LIST_COST_CENTS] == {agent(agent='classifier'): 1852, agent(agent='curator'): 100}
    assert collected[names.SESSION_ACTIVE_SECONDS] == {agent(agent='classifier'): 90.5, agent(agent='curator'): 0.0}
    assert collected[names.SESSION_WEB_SEARCH_REQUESTS] == {agent(agent='classifier'): 3, agent(agent='curator'): 0}
    assert collected[names.SESSION_TOKENS][agent(agent='classifier', type='input')] == 10
    assert collected[names.SESSION_TOKENS][agent(agent='classifier', type='cacheCreation')] == 40
    assert collected[names.SESSION_TOKENS][agent(agent='curator', type='output')] == 0
    assert len(collected[names.SESSION_TOKENS]) == 2 * len(names.TokenType)


def test_the_heartbeat_is_the_run_time_without_labels_and_the_only_series_of_an_empty_workspace() -> None:
    sink, reader = _gauges()

    sink.write({})

    assert in_memory_metrics.collected(reader) == {
        names.EXPORTER_LAST_SUCCESS_TIMESTAMP_SECONDS: {in_memory_metrics.labels(): _NOW}
    }


def test_the_heartbeat_reads_the_system_clock_outside_tests() -> None:
    reader = metrics_export.InMemoryMetricReader()
    provider = sdk_metrics.MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    before = time.time()

    gauges.TelemetryGauges(provider.get_meter('test')).write({})

    (heartbeat,) = in_memory_metrics.collected(reader)[names.EXPORTER_LAST_SUCCESS_TIMESTAMP_SECONDS].values()
    assert before <= heartbeat <= time.time()


def test_a_later_write_replaces_an_agents_value() -> None:
    sink, reader = _gauges()
    zero = dict.fromkeys(names.TokenType, 0)

    sink.write({'a': _totals(5, tokens=zero, seconds=0.0, searches=0)})
    sink.write({'a': _totals(3, tokens=zero, seconds=0.0, searches=0)})

    assert in_memory_metrics.collected(reader)[names.SESSION_LIST_COST_CENTS] == {
        in_memory_metrics.labels(agent='a'): 3
    }
