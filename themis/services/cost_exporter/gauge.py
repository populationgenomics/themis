"""The cumulative list-cost gauge in Cloud Monitoring: one point per agent, every run.

A GAUGE of a running total, not Monitoring's CUMULATIVE kind: deleting a session steps its agent's total
down, which a gauge shows truthfully as a dip and a CUMULATIVE series would misread as a counter reset
(`docs/design/cost-monitoring.md`, Design). Deltas and rates are the dashboard's and the alert policies'
queries over this series, never the exporter's arithmetic.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses
import datetime
import logging
from collections.abc import Mapping, MutableSequence
from typing import Protocol, override

from google.api import metric_pb2
from google.cloud import monitoring_v3

# The metric the dashboard and alert policies read. Its descriptor is declared with the exporter's
# infrastructure (`infra/themis_infra/cost.py`), not minted by the first write.
METRIC_TYPE = 'custom.googleapis.com/themis/anthropic/session_list_cost_cents'
# The attribution dimension: the name of the agent whose sessions the total covers.
AGENT_LABEL = 'agent'

# `generic_task` is the writable resource for a workload Monitoring has no native type for (a Cloud
# Run Job's own type is not writable); its five labels place the series.
_RESOURCE_TYPE = 'generic_task'
# Monitoring accepts at most this many series per `createTimeSeries` request.
_MAX_SERIES_PER_WRITE = 200
# The write runs in a thread the run's deadline cannot interrupt, so this is its bound; the Job's timeout
# margin over the deadline (`infra/themis_infra/cost.py`) has to cover it.
_WRITE_TIMEOUT_SECONDS = 30.0

_logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class GaugeTarget:
    """Where the series live: the GCP project written to, and the `generic_task` the points belong to.

    Attributes:
        project: The GCP project whose Monitoring workspace holds the metric.
        location: The `generic_task` location — the exporter's region.
        namespace: The `generic_task` namespace — the product.
        job: The `generic_task` job — the exporter.
        task_id: The `generic_task` task — the Anthropic workspace scanned, so a series names its source.
    """

    project: str
    location: str
    namespace: str
    job: str
    task_id: str


class TimeSeriesWriter(Protocol):
    """The one Monitoring call the gauge makes; `monitoring_v3.MetricServiceClient` satisfies it."""

    def create_time_series(
        self, *, name: str, time_series: MutableSequence[monitoring_v3.TimeSeries], timeout: float
    ) -> None: ...


class SpendGauge(abc.ABC):
    """The per-agent gauge: one write per run carries every agent's total at one instant."""

    @abc.abstractmethod
    async def write(self, totals: Mapping[str, int], at: datetime.datetime) -> None:
        """Write one point per agent, all stamped `at`; raise rather than write part of the set."""


class CloudMonitoringGauge(SpendGauge):
    """The live gauge: one `createTimeSeries` call per run against the target project."""

    def __init__(self, client: TimeSeriesWriter, target: GaugeTarget) -> None:
        self._client = client
        self._target = target

    @override
    async def write(self, totals: Mapping[str, int], at: datetime.datetime) -> None:
        series = time_series(totals, at, self._target)
        if not series:
            # A workspace with no sessions has no agent to write a total for; Monitoring refuses an empty
            # write, and there is nothing a zero could honestly be attributed to.
            _logger.info('no sessions in the workspace; nothing to write')
            return
        # The client is synchronous: off the event loop, bounded by its own timeout rather than the run's deadline.
        await asyncio.to_thread(
            self._client.create_time_series,
            name=monitoring_v3.MetricServiceClient.common_project_path(self._target.project),
            time_series=series,
            timeout=_WRITE_TIMEOUT_SECONDS,
        )


def time_series(
    totals: Mapping[str, int], at: datetime.datetime, target: GaugeTarget
) -> list[monitoring_v3.TimeSeries]:
    """One GAUGE/INT64 series per agent, each carrying a single point stamped `at`.

    Raises:
        ValueError: `at` is naive, or there are more agents than Monitoring accepts in one write.
    """
    if at.tzinfo is None:
        raise ValueError('precondition failed: the observation time must be timezone-aware')
    if len(totals) > _MAX_SERIES_PER_WRITE:
        raise ValueError(
            f'precondition failed: {len(totals)} agents exceed the {_MAX_SERIES_PER_WRITE} series one write accepts'
        )
    resource = {
        'type': _RESOURCE_TYPE,
        'labels': {
            'project_id': target.project,
            'location': target.location,
            'namespace': target.namespace,
            'job': target.job,
            'task_id': target.task_id,
        },
    }
    return [
        monitoring_v3.TimeSeries(
            metric={'type': METRIC_TYPE, 'labels': {AGENT_LABEL: agent}},
            resource=resource,
            metric_kind=metric_pb2.MetricDescriptor.MetricKind.GAUGE,
            value_type=metric_pb2.MetricDescriptor.ValueType.INT64,
            points=[
                monitoring_v3.Point(
                    interval=monitoring_v3.TimeInterval(end_time=at),
                    value=monitoring_v3.TypedValue(int64_value=cents),
                )
            ],
        )
        for agent, cents in sorted(totals.items())
    ]
