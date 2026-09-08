"""The resource a workload's series belong to, and the one-shot reader's fail-loud export.

The detectors are fakes standing in for what the GCP detector yields on a Cloud Run service; the
exporter is a fake recording what it was handed. Nothing here reaches a network.
"""

from __future__ import annotations

import datetime
from typing import override

import grpc
import pytest
from google.auth import credentials as google_credentials
from opentelemetry.sdk import metrics as sdk_metrics
from opentelemetry.sdk import resources
from opentelemetry.sdk.metrics import export as metrics_export

from themis.telemetry import metrics

_TIMEOUT = datetime.timedelta(seconds=5)


class _Detector(resources.ResourceDetector):
    """What the GCP detector yields on a Cloud Run service, or nothing, as on a Job."""

    def __init__(self, attributes: dict[str, str]) -> None:
        super().__init__()
        self._attributes = attributes

    @override
    def detect(self) -> resources.Resource:
        return resources.Resource(self._attributes) if self._attributes else resources.Resource.get_empty()


_CLOUD_RUN_SERVICE = {
    'cloud.platform': 'gcp_cloud_run',
    metrics.CLOUD_REGION: 'australia-southeast1',
    metrics.FAAS_NAME: 'themis-convert-worker',
    metrics.FAAS_INSTANCE: '00bf4bf02d6c',
}


def test_a_job_places_itself_with_explicit_region_and_a_stable_instance() -> None:
    resource = metrics.workload_resource(
        project='themis-test',
        service_name='themis-cost-exporter',
        attributes={
            metrics.CLOUD_REGION: 'australia-southeast1',
            metrics.SERVICE_INSTANCE_ID: 'w',
        },
        detectors=[_Detector({})],
    )

    assert resource.attributes[metrics.GCP_PROJECT_ID] == 'themis-test'
    assert resource.attributes[metrics.SERVICE_NAME] == 'themis-cost-exporter'
    assert resource.attributes[metrics.CLOUD_REGION] == 'australia-southeast1'
    assert resource.attributes[metrics.SERVICE_INSTANCE_ID] == 'w'


def test_a_service_is_placed_by_what_the_detector_finds() -> None:
    resource = metrics.workload_resource(
        project='themis-test', service_name='themis-convert-worker', detectors=[_Detector(_CLOUD_RUN_SERVICE)]
    )

    assert resource.attributes[metrics.CLOUD_REGION] == 'australia-southeast1'
    assert resource.attributes[metrics.FAAS_INSTANCE] == '00bf4bf02d6c'
    assert resource.attributes[metrics.SERVICE_NAME] == 'themis-convert-worker'


def test_an_explicit_attribute_overrides_a_detected_one() -> None:
    resource = metrics.workload_resource(
        project='themis-test',
        service_name='s',
        attributes={metrics.CLOUD_REGION: 'europe-west1'},
        detectors=[_Detector(_CLOUD_RUN_SERVICE)],
    )

    assert resource.attributes[metrics.CLOUD_REGION] == 'europe-west1'


@pytest.mark.parametrize(
    ('attributes', 'missing'),
    [
        pytest.param({metrics.SERVICE_INSTANCE_ID: 'w'}, 'location', id='no-location'),
        pytest.param({metrics.CLOUD_REGION: 'r'}, 'instance', id='no-instance'),
        pytest.param({}, 'location', id='neither'),
    ],
)
def test_a_resource_the_api_would_drop_points_for_is_refused(attributes: dict[str, str], missing: str) -> None:
    with pytest.raises(metrics.ResourceError, match=missing):
        metrics.workload_resource(project='p', service_name='s', attributes=attributes, detectors=[_Detector({})])


class _Exporter(metrics_export.MetricExporter):
    def __init__(
        self,
        result: metrics_export.MetricExportResult,
        *,
        temporality: dict[type, metrics_export.AggregationTemporality] | None = None,
    ) -> None:
        super().__init__(preferred_temporality=temporality)
        self._result = result
        self.exported: list[metrics_export.MetricsData] = []
        self.shutdowns = 0

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
        self.shutdowns += 1


def _provider_over(exporter: _Exporter) -> tuple[sdk_metrics.MeterProvider, metrics.OneShotReader]:
    reader = metrics.OneShotReader(exporter)
    return sdk_metrics.MeterProvider(metric_readers=[reader], shutdown_on_exit=False), reader


def _metric_names(data: metrics_export.MetricsData) -> list[str]:
    return [m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics]


def test_a_flush_exports_what_was_set_once() -> None:
    exporter = _Exporter(metrics_export.MetricExportResult.SUCCESS)
    provider, reader = _provider_over(exporter)
    provider.get_meter('t').create_gauge('g').set(3)

    reader.flush(timeout=_TIMEOUT)

    assert [_metric_names(data) for data in exporter.exported] == [['g']]


def test_an_export_the_api_did_not_accept_raises() -> None:
    exporter = _Exporter(metrics_export.MetricExportResult.FAILURE)
    provider, reader = _provider_over(exporter)
    provider.get_meter('t').create_gauge('g').set(3)

    with pytest.raises(metrics.ExportError, match='FAILURE'):
        reader.flush(timeout=_TIMEOUT)


def test_a_flush_with_nothing_recorded_raises_rather_than_exit_clean() -> None:
    # A run that set no instrument has nothing the freshness alert could see; the process has to say so.
    exporter = _Exporter(metrics_export.MetricExportResult.SUCCESS)
    _, reader = _provider_over(exporter)

    with pytest.raises(metrics.ExportError, match='nothing to export'):
        reader.flush(timeout=_TIMEOUT)

    assert exporter.exported == []


def test_the_reader_collects_in_the_exporters_temporality() -> None:
    # The environment's temporality preference reaches the exporter alone; what the reader collects has
    # to follow it, or the API receives cumulative points labelled as deltas.
    exporter = _Exporter(
        metrics_export.MetricExportResult.SUCCESS,
        temporality={sdk_metrics.Counter: metrics_export.AggregationTemporality.DELTA},
    )
    provider, reader = _provider_over(exporter)
    provider.get_meter('t').create_counter('c').add(1)

    reader.flush(timeout=_TIMEOUT)

    (data,) = exporter.exported
    (metric,) = [m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics]
    assert isinstance(metric.data, metrics_export.Sum)
    assert metric.data.aggregation_temporality is metrics_export.AggregationTemporality.DELTA


def test_shutdown_exports_nothing() -> None:
    exporter = _Exporter(metrics_export.MetricExportResult.SUCCESS)
    provider, _ = _provider_over(exporter)
    provider.get_meter('t').create_gauge('g').set(3)

    provider.shutdown()

    assert exporter.exported == []
    assert exporter.shutdowns == 1


def test_the_exporter_is_built_for_the_telemetry_api_over_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, object] = {}

    class _Built:
        def __init__(self, **kwargs: object) -> None:
            built.update(kwargs)

    monkeypatch.setattr(metrics.otlp_grpc, 'OTLPMetricExporter', _Built)

    metrics.telemetry_api_exporter(google_credentials.AnonymousCredentials(), timeout=_TIMEOUT)

    # An https endpoint is what makes the SDK open a secure channel; the credentials carry the token.
    assert built['endpoint'] == 'https://telemetry.googleapis.com:443'
    assert isinstance(built['credentials'], grpc.ChannelCredentials)
    assert built['timeout'] == _TIMEOUT.total_seconds()
