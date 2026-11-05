"""A deploy writes one point as it ends, the time it ended labelled with how, on a series of its own per run."""

from __future__ import annotations

from typing import override

import pytest
from opentelemetry.sdk.metrics import export as metrics_export

from themis.telemetry import deploy_outcome, operational_names
from themis.telemetry import metrics as telemetry_metrics


class _Exporter(metrics_export.MetricExporter):
    def __init__(self, result: metrics_export.MetricExportResult) -> None:
        super().__init__()
        self._result = result
        self.exported: list[metrics_export.MetricsData] = []

    @override
    def export(
        self, metrics_data: metrics_export.MetricsData, timeout_millis: float = 10_000, **kwargs: object
    ) -> metrics_export.MetricExportResult:
        self.exported.append(metrics_data)
        return self._result

    @override
    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    @override
    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        pass


@pytest.mark.parametrize('result', list(operational_names.DeployResult))
def test_the_point_is_the_end_time_labelled_with_the_result_on_the_runs_own_series(
    result: operational_names.DeployResult,
) -> None:
    exporter = _Exporter(metrics_export.MetricExportResult.SUCCESS)
    deploy_outcome.record(
        project='p',
        region='australia-southeast1',
        run='42-1',
        result=result,
        reader=telemetry_metrics.OneShotReader(exporter),
        clock=lambda: 1_800_000_000.5,
    )

    [data] = exporter.exported
    [resource_metrics] = data.resource_metrics
    attributes = resource_metrics.resource.attributes
    assert attributes[telemetry_metrics.SERVICE_INSTANCE_ID] == '42-1'
    assert attributes[telemetry_metrics.CLOUD_REGION] == 'australia-southeast1'
    assert attributes[telemetry_metrics.SERVICE_NAME] == deploy_outcome.SERVICE_NAME
    points: dict[str, list[tuple[dict[str, object], int | float]]] = {}
    for scope_metrics in resource_metrics.scope_metrics:
        for metric in scope_metrics.metrics:
            for point in metric.data.data_points:
                assert isinstance(point, metrics_export.NumberDataPoint)
                points.setdefault(metric.name, []).append((dict(point.attributes or {}), point.value))
    assert points == {
        operational_names.DEPLOY_FINISHED: [({operational_names.RESULT_LABEL: result.value}, 1_800_000_000.5)]
    }


def test_the_command_line_takes_only_a_known_result() -> None:
    with pytest.raises(SystemExit):
        deploy_outcome.main(['--region', 'australia-southeast1', '--run', '42-1', '--result', 'skipped'])


def test_a_point_the_api_did_not_accept_fails_the_step() -> None:
    reader = telemetry_metrics.OneShotReader(_Exporter(metrics_export.MetricExportResult.FAILURE))
    with pytest.raises(telemetry_metrics.ExportError):
        deploy_outcome.record(
            project='p',
            region='australia-southeast1',
            run='42-1',
            result=operational_names.DeployResult.FAILED,
            reader=reader,
        )
