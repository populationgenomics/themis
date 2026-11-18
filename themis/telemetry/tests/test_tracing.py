"""The trace pipeline's configuration, its sampling decision, and the exporter it is built on.

Sampling and provider tests shut down before exporting; the Telemetry API exporter test exports a span
over TLS to a loopback OTLP server.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import threading
import time
from collections.abc import Iterator, Sequence
from typing import override

import grpc
import pytest
from google.auth import credentials as google_credentials
from opentelemetry import context, trace
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc
from opentelemetry.sdk import resources
from opentelemetry.sdk import trace as sdk_trace
from opentelemetry.sdk.trace import export as trace_export
from opentelemetry.sdk.trace.export import in_memory_span_exporter

from themis.telemetry import telemetry_api, tracing
from themis.telemetry.tests import otlp_loopback

_TIMEOUT = dt.timedelta(seconds=5)


@pytest.mark.parametrize('raw', ['', 'one', '-0.1', '1.5', 'nan', 'inf'])
def test_a_ratio_that_is_not_a_fraction_exits(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(tracing.SAMPLE_RATIO_VAR, raw)
    with pytest.raises(SystemExit, match=tracing.SAMPLE_RATIO_VAR):
        tracing.install_from_env('themis-test')


def test_an_unset_ratio_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tracing.SAMPLE_RATIO_VAR, raising=False)
    with pytest.raises(SystemExit, match='required'):
        tracing.install_from_env('themis-test')


def _no_credentials() -> tuple[google_credentials.Credentials, str]:
    raise AssertionError('Application Default Credentials were read')


@pytest.mark.parametrize('raw', ['0', '0.0'])
def test_a_ratio_of_0_installs_nothing_and_reads_no_credentials(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(tracing.SAMPLE_RATIO_VAR, raw)
    monkeypatch.setattr(telemetry_api, 'application_default_credentials', _no_credentials)
    before = trace.get_tracer_provider()

    tracing.install_from_env('themis-test')

    assert trace.get_tracer_provider() is before


@contextlib.contextmanager
def _isolated_provider_slot(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[sdk_trace.TracerProvider]]:
    installed: list[sdk_trace.TracerProvider] = []

    def get_provider() -> trace.TracerProvider:
        return installed[-1] if installed else trace.ProxyTracerProvider()

    def set_provider(provider: trace.TracerProvider) -> None:
        assert isinstance(provider, sdk_trace.TracerProvider)
        installed.append(provider)

    monkeypatch.setattr(trace, 'get_tracer_provider', get_provider)
    monkeypatch.setattr(trace, 'set_tracer_provider', set_provider)
    monkeypatch.setattr(
        telemetry_api,
        'application_default_credentials',
        lambda: (google_credentials.AnonymousCredentials(), 'themis-test'),
    )
    try:
        yield installed
    finally:
        for provider in installed:
            provider.shutdown()


def _records_on(provider: sdk_trace.TracerProvider, trace_id: str, *, parent_sampled: bool = True) -> bool:
    parent = trace.NonRecordingSpan(
        trace.SpanContext(
            trace_id=int(trace_id, 16),
            span_id=0x00F067AA0BA902B7,
            is_remote=True,
            trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED if parent_sampled else trace.TraceFlags.DEFAULT),
        )
    )
    span = provider.get_tracer('t').start_span('s', context=trace.set_span_in_context(parent, context.Context()))
    return span.is_recording()


def test_install_from_env_places_spans_on_the_detected_workload_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Detected(resources.ResourceDetector):
        @override
        def detect(self) -> resources.Resource:
            return resources.Resource({'cloud.region': 'australia-southeast1', resources.SERVICE_NAME: 'detected'})

    monkeypatch.setenv(tracing.SAMPLE_RATIO_VAR, '1')
    with _isolated_provider_slot(monkeypatch) as installed:
        tracing.install_from_env('themis-sheaf', detectors=[_Detected()])
        (provider,) = installed
        assert provider.resource.attributes[telemetry_api.GCP_PROJECT_ID] == 'themis-test'
        assert provider.resource.attributes[resources.SERVICE_NAME] == 'themis-sheaf'
        assert provider.resource.attributes['cloud.region'] == 'australia-southeast1'


@pytest.mark.parametrize(
    ('raw', 'trace_id', 'recorded'),
    [
        ('1', 'ffffffffffffffffffffffffffffffff', True),
        ('1.0', 'ffffffffffffffffffffffffffffffff', True),
        ('0.25', '4bf92f3577b34da63fffffffffffffff', True),
        ('0.25', '4bf92f3577b34da64000000000000000', False),
    ],
)
def test_install_from_env_samples_at_the_configured_ratio(
    monkeypatch: pytest.MonkeyPatch, raw: str, trace_id: str, recorded: bool
) -> None:
    monkeypatch.setenv(tracing.SAMPLE_RATIO_VAR, raw)
    with _isolated_provider_slot(monkeypatch) as installed:
        tracing.install_from_env('themis-sheaf', detectors=())
        (provider,) = installed
        assert _records_on(provider, trace_id) is recorded


def test_a_second_provider_is_refused(
    monkeypatch: pytest.MonkeyPatch, recorded_spans: in_memory_span_exporter.InMemorySpanExporter
) -> None:
    # The recorder is the process's provider; installing another would be silently ignored by the SDK.
    del recorded_spans
    monkeypatch.setenv(tracing.SAMPLE_RATIO_VAR, '1')
    monkeypatch.setattr(telemetry_api, 'application_default_credentials', _no_credentials)
    with pytest.raises(RuntimeError, match='already installed'):
        tracing.install_from_env('themis-test')


# The boundary cases `apps/web/src/server/tracing/sampler.test.ts` holds the web server's sampler to: a trace
# is recorded when the trace id's low 64 bits fall under the ratio times 2^64, whatever the high 64 bits.
_DECISIONS = [
    (0.5, 'ffffffffffffffff7fffffffffffffff', True),
    (0.5, '00000000000000008000000000000000', False),
    (0.25, '4bf92f3577b34da63fffffffffffffff', True),
    (0.25, '4bf92f3577b34da64000000000000000', False),
    (1.0, 'ffffffffffffffffffffffffffffffff', True),
    (0.0, '00000000000000000000000000000001', False),
]


@pytest.mark.parametrize(('ratio', 'trace_id', 'recorded'), _DECISIONS)
def test_the_decision_is_the_trace_ids_alone(ratio: float, trace_id: str, recorded: bool) -> None:
    # The parent's flag is Cloud Run's decision, not the caller's; whichever way it points, the id decides.
    provider = tracing.tracer_provider(resource=resources.Resource.get_empty(), sample_ratio=ratio)
    assert _records_on(provider, trace_id, parent_sampled=True) is recorded
    assert _records_on(provider, trace_id, parent_sampled=False) is recorded


def test_the_exporter_is_built_for_the_telemetry_api_over_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[tuple[trace_service_pb2.ExportTraceServiceRequest, dict[str, str | bytes], float]] = []

    class _Collector(trace_service_pb2_grpc.TraceServiceServicer):
        @override
        def Export(
            self,
            request: trace_service_pb2.ExportTraceServiceRequest,
            context: grpc.ServicerContext,
        ) -> trace_service_pb2.ExportTraceServiceResponse:
            received.append((request, dict(context.invocation_metadata()), context.time_remaining()))
            return trace_service_pb2.ExportTraceServiceResponse()

    with otlp_loopback.serving_tls(
        monkeypatch, lambda s: trace_service_pb2_grpc.add_TraceServiceServicer_to_server(_Collector(), s)
    ):
        exporter = tracing.telemetry_api_exporter(otlp_loopback.FixedTokenCredentials('trace-token'), timeout=_TIMEOUT)
        provider = sdk_trace.TracerProvider()
        provider.add_span_processor(trace_export.SimpleSpanProcessor(exporter))
        try:
            provider.get_tracer('t').start_span('rpc_call').end()
        finally:
            provider.shutdown()

    [(req, metadata, remaining)] = received
    assert metadata['authorization'] == 'Bearer trace-token'
    assert 0 < remaining <= _TIMEOUT.total_seconds()
    exported_names = [s.name for rs in req.resource_spans for ss in rs.scope_spans for s in ss.spans]
    assert exported_names == ['rpc_call']


def test_a_flush_returns_at_its_timeout_while_the_export_is_still_running(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stopping instance has ten seconds in all; a flush that waited the exporter out would spend them.
    exporting, released = threading.Event(), threading.Event()

    class _Stuck(trace_export.SpanExporter):
        @override
        def export(self, spans: Sequence[sdk_trace.ReadableSpan]) -> trace_export.SpanExportResult:
            exporting.set()
            released.wait(timeout=30)
            return trace_export.SpanExportResult.SUCCESS

        @override
        def shutdown(self) -> None:
            pass

    provider = sdk_trace.TracerProvider()
    provider.add_span_processor(trace_export.BatchSpanProcessor(_Stuck()))
    monkeypatch.setattr(trace, 'get_tracer_provider', lambda: provider)
    provider.get_tracer(__name__).start_span('still held').end()
    started = time.monotonic()
    try:
        tracing.flush(dt.timedelta(seconds=0.2))
        waited = time.monotonic() - started
        assert exporting.is_set()
        assert waited < 5
    finally:
        released.set()
        provider.shutdown()
