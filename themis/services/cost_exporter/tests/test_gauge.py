"""The gauge write: one GAUGE/INT64 series per agent on the exporter's `generic_task`, in one call.

The Monitoring client is a fake recording the request; the series it receives are the real
`monitoring_v3` messages, so what is asserted is what would go over the wire.
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Sequence

import pytest
from google.api import metric_pb2
from google.cloud import monitoring_v3

from themis.services.cost_exporter import gauge

_AT = datetime.datetime(2026, 9, 4, 6, 0, 30, tzinfo=datetime.UTC)
_TARGET = gauge.GaugeTarget(
    project='themis-test',
    location='australia-southeast1',
    namespace='themis',
    job='cost-exporter',
    task_id='wrkspc_test',
)


class _Writer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[monitoring_v3.TimeSeries]]] = []

    def create_time_series(self, *, name: str, time_series: Sequence[monitoring_v3.TimeSeries], timeout: float) -> None:
        assert timeout > 0
        self.calls.append((name, list(time_series)))


def test_one_series_per_agent_each_with_one_point_stamped_at_the_observation() -> None:
    series = gauge.time_series({'curator': 100, 'classifier': 1852}, _AT, _TARGET)

    assert [s.metric.labels[gauge.AGENT_LABEL] for s in series] == ['classifier', 'curator']
    for s in series:
        assert s.metric.type == gauge.METRIC_TYPE
        assert s.metric_kind == metric_pb2.MetricDescriptor.MetricKind.GAUGE
        assert s.value_type == metric_pb2.MetricDescriptor.ValueType.INT64
        assert s.resource.type == 'generic_task'
        assert dict(s.resource.labels) == {
            'project_id': 'themis-test',
            'location': 'australia-southeast1',
            'namespace': 'themis',
            'job': 'cost-exporter',
            'task_id': 'wrkspc_test',
        }
        assert len(s.points) == 1
        assert s.points[0].interval.end_time == _AT
    assert [s.points[0].value.int64_value for s in series] == [1852, 100]


def test_a_naive_observation_time_is_refused() -> None:
    with pytest.raises(ValueError, match='timezone-aware'):
        gauge.time_series({'a': 1}, _AT.replace(tzinfo=None), _TARGET)


def test_more_agents_than_one_write_accepts_is_refused_whole() -> None:
    totals = {f'agent-{i}': i for i in range(201)}
    with pytest.raises(ValueError, match='201 agents exceed'):
        gauge.time_series(totals, _AT, _TARGET)


def test_the_write_is_one_call_against_the_target_project() -> None:
    writer = _Writer()

    asyncio.run(gauge.CloudMonitoringGauge(writer, _TARGET).write({'classifier': 1852, 'curator': 100}, _AT))

    assert [(name, len(series)) for name, series in writer.calls] == [('projects/themis-test', 2)]


def test_no_agents_means_no_call() -> None:
    writer = _Writer()

    asyncio.run(gauge.CloudMonitoringGauge(writer, _TARGET).write({}, _AT))

    assert writer.calls == []
