"""Google Cloud's Telemetry API: the one endpoint the metrics and trace pipelines export to.

Both pipelines speak OTLP over gRPC to `telemetry.googleapis.com`, with Application Default Credentials
attached to every call. The API bills quota to the credentials' project and writes into the project the
OpenTelemetry resource names under `gcp.project_id`: points to Cloud Monitoring, spans to Cloud Trace.
"""

from __future__ import annotations

import google.auth
import google.auth.credentials
import google.auth.transport.grpc
import google.auth.transport.requests
import grpc
from opentelemetry.resourcedetector import gcp_resource_detector
from opentelemetry.sdk import resources

ENDPOINT = 'https://telemetry.googleapis.com:443'
# The resource attribute naming the project a point or span is written to.
GCP_PROJECT_ID = 'gcp.project_id'

_SCOPES = ('https://www.googleapis.com/auth/cloud-platform',)


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


def channel_credentials(credentials: google.auth.credentials.Credentials) -> grpc.ChannelCredentials:
    """TLS to the API, and `credentials` refreshed and attached to every call."""
    plugin = google.auth.transport.grpc.AuthMetadataPlugin(credentials, google.auth.transport.requests.Request())
    return grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), grpc.metadata_call_credentials(plugin))


def google_cloud_detectors() -> tuple[resources.ResourceDetector, ...]:
    """The detectors that read a Cloud Run service's region, instance and name off its environment."""
    return (gcp_resource_detector.GoogleCloudResourceDetector(),)
