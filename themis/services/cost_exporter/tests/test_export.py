"""A run's contract: totals by agent from a complete scan, one write stamped once, nothing on a failed scan.

The source and the gauge are in-memory fakes; the SDK and Monitoring adapters have their own tests.
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import override

import pytest

from themis.services.cost_exporter import export, gauge, sessions

_AT = datetime.datetime(2026, 9, 4, 6, 0, tzinfo=datetime.UTC)


class _Source(sessions.SessionCostSource):
    def __init__(self, costs: Iterable[sessions.SessionCost], *, fail_after: int | None = None) -> None:
        self._costs = list(costs)
        self._fail_after = fail_after

    @override
    async def sessions(self) -> AsyncIterator[sessions.SessionCost]:
        for index, cost in enumerate(self._costs):
            if self._fail_after is not None and index == self._fail_after:
                raise sessions.SessionRecordError('precondition failed: a page the total cannot use')
            yield cost


class _Gauge(gauge.SpendGauge):
    def __init__(self) -> None:
        self.writes: list[tuple[dict[str, int], datetime.datetime]] = []

    @override
    async def write(self, totals: Mapping[str, int], at: datetime.datetime) -> None:
        self.writes.append((dict(totals), at))


def _cost(agent: str, cents: int) -> sessions.SessionCost:
    return sessions.SessionCost(agent=agent, cents=cents)


def test_totals_sum_every_session_of_an_agent_and_keep_zero_cost_agents() -> None:
    source = _Source(
        [
            _cost('classifier', 215),
            _cost('classifier', 1637),
            _cost('curator', 100),
            _cost('harness demo', 0),
        ]
    )
    sink = _Gauge()

    report = asyncio.run(export.run(source, sink, now=lambda: _AT))

    assert report.totals == {'classifier': 1852, 'curator': 100, 'harness demo': 0}
    assert report.session_count == 4
    assert report.at == _AT
    assert sink.writes == [({'classifier': 1852, 'curator': 100, 'harness demo': 0}, _AT)]


def test_the_observation_time_is_read_once_after_the_scan() -> None:
    clock = iter([_AT, _AT + datetime.timedelta(minutes=1)])
    sink = _Gauge()

    report = asyncio.run(export.run(_Source([_cost('a', 1)]), sink, now=lambda: next(clock)))

    assert report.at == _AT
    assert [at for _, at in sink.writes] == [_AT]


def test_a_failed_scan_writes_nothing() -> None:
    source = _Source([_cost('a', 1), _cost('a', 2)], fail_after=1)
    sink = _Gauge()

    with pytest.raises(sessions.SessionRecordError):
        asyncio.run(export.run(source, sink, now=lambda: _AT))

    assert sink.writes == []


def test_an_empty_workspace_is_a_write_of_no_totals() -> None:
    sink = _Gauge()

    report = asyncio.run(export.run(_Source([]), sink, now=lambda: _AT))

    assert report.totals == {}
    assert report.session_count == 0
    assert sink.writes == [({}, _AT)]
