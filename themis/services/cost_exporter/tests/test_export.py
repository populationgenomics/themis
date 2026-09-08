"""A run's contract: totals by agent from a complete scan, one write, nothing on a failed scan.

The source and the gauges are in-memory fakes; the SDK and Telemetry adapters have their own tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import override

import pytest

from themis.services.cost_exporter import export, gauges, sessions
from themis.telemetry import names


class _Source(sessions.SessionUsageSource):
    def __init__(self, usages: Iterable[sessions.SessionUsage], *, fail_after: int | None = None) -> None:
        self._usages = list(usages)
        self._fail_after = fail_after

    @override
    async def sessions(self) -> AsyncIterator[sessions.SessionUsage]:
        for index, usage in enumerate(self._usages):
            if self._fail_after is not None and index == self._fail_after:
                raise sessions.SessionRecordError('precondition failed: a page the totals cannot use')
            yield usage


class _Gauges(gauges.SpendGauges):
    def __init__(self) -> None:
        self.writes: list[dict[str, sessions.AgentTotals]] = []

    @override
    def write(self, totals: Mapping[str, sessions.AgentTotals]) -> None:
        self.writes.append(dict(totals))


def _usage(
    agent: str, cents: int, *, tokens: int = 0, seconds: float = 0.0, searches: int = 0
) -> sessions.SessionUsage:
    return sessions.SessionUsage(
        agent=agent,
        cents=cents,
        tokens=dict.fromkeys(names.TokenType, tokens),
        active_seconds=seconds,
        web_search_requests=searches,
    )


def test_totals_sum_every_session_of_an_agent_and_keep_idle_agents() -> None:
    source = _Source(
        [
            _usage('classifier', 215, tokens=10, seconds=1.5, searches=1),
            _usage('classifier', 1637, tokens=5, seconds=2.0, searches=2),
            _usage('curator', 100, tokens=7),
            _usage('harness demo', 0),
        ]
    )
    sink = _Gauges()

    report = asyncio.run(export.run(source, sink))

    assert report.session_count == 4
    assert set(report.totals) == {'classifier', 'curator', 'harness demo'}
    classifier = report.totals['classifier']
    assert classifier.cents == 1852
    assert classifier.tokens == dict.fromkeys(names.TokenType, 15)
    assert classifier.active_seconds == 3.5
    assert classifier.web_search_requests == 3
    assert report.totals['harness demo'] == sessions.NO_SESSIONS
    assert sink.writes == [report.totals]


def test_a_failed_scan_writes_nothing() -> None:
    source = _Source([_usage('a', 1), _usage('a', 2)], fail_after=1)
    sink = _Gauges()

    with pytest.raises(sessions.SessionRecordError):
        asyncio.run(export.run(source, sink))

    assert sink.writes == []


def test_an_empty_workspace_is_one_write_of_no_totals() -> None:
    # The write still happens: the heartbeat rides it, sessions or none.
    sink = _Gauges()

    report = asyncio.run(export.run(_Source([]), sink))

    assert report.totals == {}
    assert report.session_count == 0
    assert sink.writes == [{}]
