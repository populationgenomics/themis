"""The trace pipeline a Themis service exports through: OTLP over gRPC to the Telemetry API, read in Cloud Trace.

A request's trace is one tree across the services it passes through. Every hop carries the W3C
`traceparent` header, in gRPC metadata between services, so each service continues its caller's trace
rather than starting one. Cloud Run populates `traceparent` on every request it forwards and records the
same trace id on its request log, so each hop's request log opens onto the trace too.

A service installs its tracer provider once at startup (`install_from_env`), and every span the process
starts reaches it through OpenTelemetry's global provider: the server interceptor's span around each
incoming rpc, the client interceptor's around each outgoing one (`themis/clients/auth/session.py`), and the
hand spans a backend puts around its own round trips.

A trace is recorded whole or not at all. Every service decides from the trace id alone, and never from the
sampled flag the caller sent: Cloud Run puts a span of its own between every two hops and samples those
spans at no more than 0.1 requests per second per instance, so the flag a service receives need not be its
caller's decision. The decision is the SDK's `TraceIdRatioBased`, whose rule the web server's sampler
reproduces (`apps/web/src/server/tracing/sampler.ts`), so a service at the same ratio agrees with its
callers about every trace.
"""

from __future__ import annotations

import datetime
import math
import os
from collections.abc import Iterable

import google.auth.credentials
import grpc.aio
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc import trace_exporter as otlp_grpc
from opentelemetry.instrumentation import grpc as grpc_instrumentation
from opentelemetry.instrumentation.grpc import filters as grpc_filters
from opentelemetry.sdk import resources
from opentelemetry.sdk import trace as sdk_trace
from opentelemetry.sdk.trace import export as trace_export
from opentelemetry.sdk.trace import sampling

from themis.telemetry import telemetry_api

SAMPLE_RATIO_VAR = 'THEMIS_TRACE_SAMPLE_RATIO'

_EXPORT_TIMEOUT = datetime.timedelta(seconds=10)


def sample_ratio_from_env() -> float:
    """The fraction of traces this service records, from `THEMIS_TRACE_SAMPLE_RATIO`.

    Raises:
        SystemExit: The variable is unset, empty, not a number, or outside 0 to 1.
    """
    raw = os.environ.get(SAMPLE_RATIO_VAR)
    if not raw:
        raise SystemExit(f'{SAMPLE_RATIO_VAR} is required: the fraction of traces this service records, 0 to 1')
    try:
        ratio = float(raw)
    except ValueError as e:
        raise SystemExit(f'{SAMPLE_RATIO_VAR} must be a number from 0 to 1, got {raw!r}') from e
    if not (math.isfinite(ratio) and 0 <= ratio <= 1):
        raise SystemExit(f'{SAMPLE_RATIO_VAR} must be a number from 0 to 1, got {raw!r}')
    return ratio


def workload_resource(
    *, project: str, service_name: str, detectors: Iterable[resources.ResourceDetector] = ()
) -> resources.Resource:
    """The resource a service's spans belong to: detected attributes, then its project and name.

    Args:
        project: The GCP project the spans are written to.
        service_name: The service's name, the one Cloud Trace lists its spans under.
        detectors: Resource detectors to run, `telemetry_api.google_cloud_detectors()` on Cloud Run.
    """
    detected = resources.Resource.get_empty()
    for detector in detectors:
        detected = detected.merge(detector.detect())
    return detected.merge(
        resources.Resource.create({telemetry_api.GCP_PROJECT_ID: project, resources.SERVICE_NAME: service_name})
    )


def tracer_provider(*, resource: resources.Resource, sample_ratio: float) -> sdk_trace.TracerProvider:
    """A tracer provider recording `sample_ratio` of traces by trace id, with no span processor yet.

    Raises:
        ValueError: `sample_ratio` is outside 0 to 1.
    """
    return sdk_trace.TracerProvider(resource=resource, sampler=sampling.TraceIdRatioBased(sample_ratio))


def telemetry_api_exporter(
    credentials: google.auth.credentials.Credentials, *, timeout: datetime.timedelta
) -> otlp_grpc.OTLPSpanExporter:
    """An OTLP/gRPC span exporter to the Telemetry API, `credentials` attached to every call.

    Args:
        credentials: Google credentials, normally `telemetry_api.application_default_credentials()`.
        timeout: How long one export may take, retries of transient failures included.
    """
    return otlp_grpc.OTLPSpanExporter(
        endpoint=telemetry_api.ENDPOINT,
        credentials=telemetry_api.channel_credentials(credentials),
        timeout=timeout.total_seconds(),
    )


def install_from_env(service_name: str) -> None:
    """Install the process's tracer provider at the ratio `THEMIS_TRACE_SAMPLE_RATIO` names.

    At a ratio of 0 nothing is installed and nothing reaches Google Cloud: every span is a no-op. Above 0 the
    provider exports in batches off the request path, so a failed export is logged by the SDK and never fails
    an rpc.

    Args:
        service_name: The service's name, the one Cloud Trace lists its spans under.

    Raises:
        SystemExit: The ratio is unset or malformed.
        RuntimeError: A tracer provider is already installed in this process.
        google.auth.exceptions.DefaultCredentialsError: No Application Default Credentials are configured.
        ValueError: The credentials name no project.
    """
    ratio = sample_ratio_from_env()
    if ratio == 0:
        return
    if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        raise RuntimeError('a tracer provider is already installed in this process')
    credentials, project = telemetry_api.application_default_credentials()
    provider = tracer_provider(
        resource=workload_resource(
            project=project, service_name=service_name, detectors=telemetry_api.google_cloud_detectors()
        ),
        sample_ratio=ratio,
    )
    provider.add_span_processor(
        trace_export.BatchSpanProcessor(telemetry_api_exporter(credentials, timeout=_EXPORT_TIMEOUT))
    )
    trace.set_tracer_provider(provider)


def server_interceptor() -> grpc.aio.ServerInterceptor:
    """The interceptor that opens a server span for each incoming rpc, continuing the caller's trace.

    The health check is left out: Cloud Run probes it, and each probe would otherwise be a trace of its own.
    """
    return grpc_instrumentation.aio_server_interceptor(filter_=grpc_filters.negate(grpc_filters.health_check()))
