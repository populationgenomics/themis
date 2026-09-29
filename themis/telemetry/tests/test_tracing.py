"""The trace pipeline's configuration, its sampling decision, and the exporter it is built on.

Nothing here reaches a network: the exporter is built, never used, and the providers under test carry no
exporter at all.
"""

from __future__ import annotations

import datetime
from typing import override

import grpc
import pytest
from google.auth import credentials as google_credentials
from opentelemetry import context, trace
from opentelemetry.sdk import resources
from opentelemetry.sdk.trace.export import in_memory_span_exporter

from themis.telemetry import telemetry_api, tracing

_TIMEOUT = datetime.timedelta(seconds=5)


@pytest.mark.parametrize(('raw', 'ratio'), [('0', 0.0), ('1', 1.0), ('1.0', 1.0), ('0.25', 0.25)])
def test_a_ratio_from_0_to_1_is_read(monkeypatch: pytest.MonkeyPatch, raw: str, ratio: float) -> None:
    monkeypatch.setenv(tracing.SAMPLE_RATIO_VAR, raw)
    assert tracing.sample_ratio_from_env() == ratio


@pytest.mark.parametrize('raw', ['', 'one', '-0.1', '1.5', 'nan', 'inf'])
def test_a_ratio_that_is_not_a_fraction_exits(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(tracing.SAMPLE_RATIO_VAR, raw)
    with pytest.raises(SystemExit, match=tracing.SAMPLE_RATIO_VAR):
        tracing.sample_ratio_from_env()


def test_an_unset_ratio_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tracing.SAMPLE_RATIO_VAR, raising=False)
    with pytest.raises(SystemExit, match='required'):
        tracing.install_from_env('themis-test')


def _no_credentials() -> tuple[google_credentials.Credentials, str]:
    raise AssertionError('Application Default Credentials were read')


def test_a_ratio_of_0_installs_nothing_and_reads_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(tracing.SAMPLE_RATIO_VAR, '0')
    monkeypatch.setattr(telemetry_api, 'application_default_credentials', _no_credentials)
    before = trace.get_tracer_provider()

    tracing.install_from_env('themis-test')

    assert trace.get_tracer_provider() is before


def test_a_second_provider_is_refused(
    monkeypatch: pytest.MonkeyPatch, recorded_spans: in_memory_span_exporter.InMemorySpanExporter
) -> None:
    # The recorder is the process's provider; installing another would be silently ignored by the SDK.
    del recorded_spans
    monkeypatch.setenv(tracing.SAMPLE_RATIO_VAR, '1')
    monkeypatch.setattr(telemetry_api, 'application_default_credentials', _no_credentials)
    with pytest.raises(RuntimeError, match='already installed'):
        tracing.install_from_env('themis-test')


def _records(ratio: float, trace_id: str, *, parent_sampled: bool) -> bool:
    """Whether a provider at `ratio` records a span continuing a remote parent in trace `trace_id`."""
    provider = tracing.tracer_provider(resource=resources.Resource.get_empty(), sample_ratio=ratio)
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
    assert _records(ratio, trace_id, parent_sampled=True) is recorded
    assert _records(ratio, trace_id, parent_sampled=False) is recorded


def test_the_resource_names_the_project_and_the_service() -> None:
    class _Detected(resources.ResourceDetector):
        @override
        def detect(self) -> resources.Resource:
            return resources.Resource({'cloud.region': 'australia-southeast1', resources.SERVICE_NAME: 'detected'})

    resource = tracing.workload_resource(project='themis-test', service_name='themis-sheaf', detectors=[_Detected()])

    assert resource.attributes[telemetry_api.GCP_PROJECT_ID] == 'themis-test'
    assert resource.attributes[resources.SERVICE_NAME] == 'themis-sheaf'
    assert resource.attributes['cloud.region'] == 'australia-southeast1'


def test_the_exporter_is_built_for_the_telemetry_api_over_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, object] = {}

    class _Built:
        def __init__(self, **kwargs: object) -> None:
            built.update(kwargs)

    monkeypatch.setattr(tracing.otlp_grpc, 'OTLPSpanExporter', _Built)

    tracing.telemetry_api_exporter(google_credentials.AnonymousCredentials(), timeout=_TIMEOUT)

    assert built['endpoint'] == 'https://telemetry.googleapis.com:443'
    assert isinstance(built['credentials'], grpc.ChannelCredentials)
    assert built['timeout'] == _TIMEOUT.total_seconds()
