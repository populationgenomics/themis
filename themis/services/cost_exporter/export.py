"""One run of the exporter: every session totalled by agent, then the gauges set once.

The totals are complete before anything is set. A listing that fails on any page, or a session the
totals cannot use, aborts the run with nothing recorded: a partial sum written as the gauge would read
as spend shrinking — a silent wrong answer — where a missed point is a visible gap the freshness alert
catches (`docs/design/cost-monitoring.md`, Design).
"""

from __future__ import annotations

import dataclasses

from themis.services.cost_exporter import gauges as gauges_mod
from themis.services.cost_exporter import sessions as sessions_mod


@dataclasses.dataclass(frozen=True)
class ExportReport:
    """What a run observed and set, for the entrypoint to log.

    Attributes:
        session_count: How many sessions the totals cover.
        totals: Cumulative usage by agent name.
    """

    session_count: int
    totals: dict[str, sessions_mod.AgentTotals]


async def run(source: sessions_mod.SessionUsageSource, gauges: gauges_mod.SpendGauges) -> ExportReport:
    """Scan every session, total usage by agent, and set the gauges once.

    Args:
        source: The workspace's sessions.
        gauges: Where the per-agent totals are set.

    Raises:
        Exception: Whatever the source raises; nothing is set after a failed scan.
    """
    totals: dict[str, sessions_mod.AgentTotals] = {}
    session_count = 0
    async for usage in source.sessions():
        totals[usage.agent] = totals.get(usage.agent, sessions_mod.NO_SESSIONS).plus(usage)
        session_count += 1
    gauges.write(totals)
    return ExportReport(session_count=session_count, totals=totals)
