"""The meter pipeline Themis workloads emit through: OTLP over gRPC to Google Cloud's Telemetry API.

A point lands in Cloud Monitoring as a `prometheus.googleapis.com/<name>/<kind>` series on a
`prometheus_target` resource whose labels the API derives from the OpenTelemetry resource — `location`
from `cloud.region` (or `location`), `instance` from `service.instance.id` (or `faas.instance`), `job`
from `service.name` (or `faas.name`) — and whose project is `gcp.project_id`. A point without a
location or an instance is dropped server-side, so `workload_resource` refuses to build a resource
lacking either. A Cloud Run service gets region, instance and name from the GCP resource detector
(the metadata server plus `K_SERVICE`); a Cloud Run Job sets none of the markers the detector reads,
so a Job passes them explicitly. Application Default Credentials authenticate the channel, and the
API bills quota to their project.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping
from typing import override

import google.auth
import google.auth.credentials
import google.auth.transport.grpc
import google.auth.transport.requests
import grpc
from opentelemetry.exporter.otlp.proto.grpc import metric_exporter as otlp_grpc
from opentelemetry.resourcedetector import gcp_resource_detector
from opentelemetry.sdk import resources
from opentelemetry.sdk.metrics import export as metrics_export

# Resource attribute keys: OpenTelemetry's semantic conventions, plus `gcp.project_id`, the project a point
# is written to.
CLOUD_REGION = 'cloud.region'
CLOUD_AVAILABILITY_ZONE = 'cloud.availability_zone'
SERVICE_NAME = 'service.name'
SERVICE_INSTANCE_ID = 'service.instance.id'
FAAS_NAME = 'faas.name'
FAAS_INSTANCE = 'faas.instance'
K8S_POD_NAME = 'k8s.pod.name'
HOST_ID = 'host.id'
GCP_PROJECT_ID = 'gcp.project_id'

_ENDPOINT = 'https://telemetry.googleapis.com:443'
_SCOPES = ('https://www.googleapis.com/auth/cloud-platform',)

# The API's fallback chains for the two labels it drops a point without, in its order.
_LOCATION_ATTRIBUTES = ('location', CLOUD_AVAILABILITY_ZONE, CLOUD_REGION)
_INSTANCE_ATTRIBUTES = ('instance', SERVICE_INSTANCE_ID, FAAS_INSTANCE, K8S_POD_NAME, HOST_ID)


class ResourceError(ValueError):
    """The resource lacks an attribute the Telemetry API drops every point without."""


class ExportError(RuntimeError):
    """The Telemetry API did not accept an export."""


def application_default_credentials() -> tuple[google.auth.credentials.Credentials, str]:
    """Application Default Credentials, and the project they resolve.

    Raises:
        google.auth.exceptions.DefaultCredentialsError: No credentials are configured.
        ValueError: The credentials name no project.
    """
    credentials, project = google.auth.default(scopes=_SCOPES)
    if not project:
        raise ValueError('precondition failed: Application Default Credentials name no project')
    return credentials, project


def google_cloud_detectors() -> tuple[resources.ResourceDetector, ...]:
    """The detectors that read a Cloud Run service's region, instance and name off its environment."""
    return (gcp_resource_detector.GoogleCloudResourceDetector(),)


def workload_resource(
    *,
    project: str,
    service_name: str,
    attributes: Mapping[str, str] | None = None,
    detectors: Iterable[resources.ResourceDetector] = (),
) -> resources.Resource:
    """The resource a workload's series belong to: detected attributes, overridden by explicit ones.

    Args:
        project: The GCP project the points are written to.
        service_name: The workload's name — the series' `job` label.
        attributes: Explicit attributes, taking precedence over what `detectors` find. A Cloud Run Job
            passes its region and a stable `service.instance.id` here: a new process per execution
            would otherwise start a new series every run.
        detectors: Resource detectors to run, `google_cloud_detectors()` on a Cloud Run service.

    Raises:
        ResourceError: Nothing in the result yields a location or an instance.
    """
    detected = resources.Resource.get_empty()
    for detector in detectors:
        detected = detected.merge(detector.detect())
    explicit = {
        GCP_PROJECT_ID: project,
        SERVICE_NAME: service_name,
        **(attributes or {}),
    }
    resource = detected.merge(resources.Resource.create(explicit))
    for label, candidates in (('location', _LOCATION_ATTRIBUTES), ('instance', _INSTANCE_ATTRIBUTES)):
        if not any(resource.attributes.get(candidate) for candidate in candidates):
            raise ResourceError(
                f'precondition failed: no resource attribute yields the {label} label; the Telemetry API '
                f'drops every point without one (any of {", ".join(candidates)})'
            )
    return resource


def telemetry_api_exporter(
    credentials: google.auth.credentials.Credentials, *, timeout: datetime.timedelta
) -> otlp_grpc.OTLPMetricExporter:
    """An OTLP/gRPC exporter to the Telemetry API, `credentials` attached to every call.

    Args:
        credentials: Google credentials, normally `application_default_credentials()`.
        timeout: How long one export may take, retries of transient failures included.
    """
    plugin = google.auth.transport.grpc.AuthMetadataPlugin(credentials, google.auth.transport.requests.Request())
    channel_credentials = grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(), grpc.metadata_call_credentials(plugin)
    )
    return otlp_grpc.OTLPMetricExporter(
        endpoint=_ENDPOINT, credentials=channel_credentials, timeout=timeout.total_seconds()
    )


class OneShotReader(metrics_export.MetricReader):
    """Exports on demand, for a process that sets its instruments once and exits.

    `flush` collects every instrument and exports once, raising when there is nothing to export or the
    API did not accept it, so the process exits non-zero rather than logging and exiting clean.
    Collection consumes a gauge's last value, so a second flush in one process raises "nothing to
    export". Shutdown exports nothing: a run that failed before its flush leaves no point behind.
    """

    def __init__(self, exporter: metrics_export.MetricExporter) -> None:
        # The exporter's preferences, as the SDK's own periodic reader takes them: the environment's
        # temporality preference reaches the exporter alone, and the two must not disagree.
        super().__init__(
            preferred_temporality=exporter._preferred_temporality,  # noqa: SLF001
            preferred_aggregation=exporter._preferred_aggregation,  # noqa: SLF001
        )
        self._exporter = exporter
        self._exported = False

    def flush(self, *, timeout: datetime.timedelta) -> None:
        """Collect every instrument once and export.

        `timeout` bounds the SDK's collection; the export itself is bounded by the exporter's own
        timeout, fixed at its construction.

        Raises:
            ExportError: No instrument recorded a value, so there was nothing to export, or the API
                did not accept the export.
        """
        self._exported = False
        self.collect(timeout_millis=timeout.total_seconds() * 1000)
        if not self._exported:
            raise ExportError('nothing to export: no instrument recorded a value')

    @override
    def _receive_metrics(
        self, metrics_data: metrics_export.MetricsData, timeout_millis: float = 10_000, **kwargs: object
    ) -> None:
        result = self._exporter.export(metrics_data, timeout_millis=timeout_millis)
        self._exported = True
        if result is not metrics_export.MetricExportResult.SUCCESS:
            raise ExportError(f'the Telemetry API did not accept the export: {result.name}')

    @override
    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        self._exporter.shutdown(timeout_millis=timeout_millis)
