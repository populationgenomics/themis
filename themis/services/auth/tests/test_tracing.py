"""A resolve as a trace: the caller's span continued into the server's.

The server and its client carry the interceptors the deployed service and its callers carry; the spans
land in the in-memory recorder. The Cloud SQL round trips inside a resolve are `test_cloudsql.py`'s, which
has the Postgres they need.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from opentelemetry import trace
from opentelemetry.instrumentation import grpc as grpc_instrumentation
from opentelemetry.sdk import trace as sdk_trace
from opentelemetry.sdk.trace.export import in_memory_span_exporter

from themis.rpc import auth_pb2, auth_pb2_grpc
from themis.services.auth import backend as auth_backend
from themis.services.auth import servicer as servicer_mod
from themis.telemetry import tracing
from themis.testing import in_process_grpc

_TOKEN = 'tok-123'
_CONTEXT = auth_pb2.SessionContext(project_id='proj-1', analysis_id='ana-1')
_TRACER = trace.get_tracer(__name__)


def _named(spans: Sequence[sdk_trace.ReadableSpan], name: str) -> sdk_trace.ReadableSpan:
    (span,) = [s for s in spans if s.name == name]
    return span


def _parent_of(span: sdk_trace.ReadableSpan) -> int | None:
    return None if span.parent is None else span.parent.span_id


def _context(span: sdk_trace.ReadableSpan) -> trace.SpanContext:
    assert span.context is not None
    return span.context


def _resolve_through(backend: auth_backend.SessionBackend) -> None:
    """Resolve `_TOKEN` through a served auth servicer over `backend`, inside a `caller` span."""

    async def run() -> None:
        servicer = servicer_mod.Servicer(backend)
        async with in_process_grpc.serving(
            lambda server: auth_pb2_grpc.add_AuthServicer_to_server(servicer, server),
            interceptors=grpc_instrumentation.aio_client_interceptors(),
            server_interceptors=[tracing.server_interceptor()],
        ) as channel:
            with _TRACER.start_as_current_span('caller'):
                await auth_pb2_grpc.AuthStub(channel).ResolveSession(auth_pb2.ResolveTokenRequest(session_token=_TOKEN))

    asyncio.run(run())


def test_a_resolve_continues_the_callers_trace(recorded_spans: in_memory_span_exporter.InMemorySpanExporter) -> None:
    _resolve_through(auth_backend.FixtureBackend({auth_backend.hash_token(_TOKEN): _CONTEXT}))

    spans = recorded_spans.get_finished_spans()
    caller = _named(spans, 'caller')
    (client,) = [s for s in spans if s.kind is trace.SpanKind.CLIENT]
    (server,) = [s for s in spans if s.kind is trace.SpanKind.SERVER]
    assert server.name == client.name == '/themis.rpc.auth.Auth/ResolveSession'
    assert {_context(s).trace_id for s in spans} == {_context(caller).trace_id}
    assert _parent_of(client) == _context(caller).span_id
    assert _parent_of(server) == _context(client).span_id
    assert server.parent is not None
    assert server.parent.is_remote


def test_the_health_check_is_not_traced(recorded_spans: in_memory_span_exporter.InMemorySpanExporter) -> None:
    async def run() -> None:
        async def register(server: object) -> None:
            servicer = health.aio.HealthServicer()  # pyright: ignore[reportAttributeAccessIssue]
            await servicer.set('', health_pb2.HealthCheckResponse.SERVING)
            health_pb2_grpc.add_HealthServicer_to_server(servicer, server)

        async with in_process_grpc.serving(register, server_interceptors=[tracing.server_interceptor()]) as channel:
            await health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest())  # pyright: ignore[reportAttributeAccessIssue]

    asyncio.run(run())

    assert recorded_spans.get_finished_spans() == ()
