"""One run of the exporter: every session totalled by agent, then one gauge write.

The total is complete before anything is written. A listing that fails on any page, or a session the
total cannot use, aborts the run with nothing recorded: a partial sum written as the gauge would read as
spend shrinking — a silent wrong answer — where a missed point is a visible gap the freshness alert
catches (`docs/design/cost-monitoring.md`, Design).
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
from collections.abc import Callable

from themis.services.cost_exporter import gauge as gauge_mod
from themis.services.cost_exporter import sessions as sessions_mod


@dataclasses.dataclass(frozen=True)
class ExportReport:
    """What a run observed and wrote, for the entrypoint to log.

    Attributes:
        at: The instant the totals were observed at, which every point is stamped with.
        session_count: How many sessions the totals cover.
        totals: Cumulative list cost in cents by agent name.
    """

    at: datetime.datetime
    session_count: int
    totals: dict[str, int]


async def run(
    source: sessions_mod.SessionCostSource,
    gauge: gauge_mod.SpendGauge,
    *,
    now: Callable[[], datetime.datetime],
) -> ExportReport:
    """Scan every session, total list cost by agent, and write the gauge once.

    Args:
        source: The workspace's sessions.
        gauge: Where the per-agent totals are written.
        now: The clock the observation is stamped from, read once the scan is complete.

    Raises:
        Exception: Whatever the source or the gauge raises; nothing is written after a failed scan.
    """
    totals: collections.Counter[str] = collections.Counter()
    session_count = 0
    async for session in source.sessions():
        totals[session.agent] += session.cents
        session_count += 1
    at = now()
    await gauge.write(totals, at)
    return ExportReport(at=at, session_count=session_count, totals=dict(totals))
