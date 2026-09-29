"""An observer outside the gate: its server span covers the gate's own work, and a denied call is traced too."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import grpc
import grpc.aio
from google.protobuf import message
from opentelemetry import trace
from opentelemetry.sdk.trace.export import in_memory_span_exporter

from themis.clients.auth import claim as claim_mod
from themis.clients.auth import interceptor as interceptor_mod
from themis.rpc import literature_pb2, sheaf_pb2
from themis.telemetry import tracing
from themis.testing import auth as fixture

_PATH = '/themis.rpc.literature.Literature/DescribePaper'
# Imported for the rule its contract declares, read off the descriptor pool.
_FETCH_PACK = f'/{sheaf_pb2.DESCRIPTOR.services_by_name["Sheaf"].full_name}/FetchPack'


def _call(metadata: fixture.Metadata) -> grpc.StatusCode:
    """Drive one DescribePaper call through a gated server whose observer is the tracing interceptor."""

    async def echo(request: message.Message, context: grpc.aio.ServicerContext) -> message.Message:
        del context
        return request

    async def run() -> grpc.StatusCode:
        server = interceptor_mod.gated_server(fixture.authorizer(), observers=[tracing.server_interceptor()])
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    'themis.rpc.literature.Literature',
                    {
                        'DescribePaper': grpc.unary_unary_rpc_method_handler(
                            echo, literature_pb2.DescribePaperRequest.FromString, lambda m: m.SerializeToString()
                        )
                    },
                ),
            )
        )
        port = server.add_insecure_port('127.0.0.1:0')
        await server.start()
        try:
            async with grpc.aio.insecure_channel(f'127.0.0.1:{port}') as channel:
                method = channel.unary_unary(_PATH, request_serializer=bytes, response_deserializer=bytes)
                try:
                    await method(b'', metadata=claim_mod.pairs(metadata))
                except grpc.aio.AioRpcError as e:
                    return e.code()
                return grpc.StatusCode.OK
        finally:
            await server.stop(None)

    return asyncio.run(run())


def _server_status_and_verification_parent(
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> tuple[object, bool]:
    """The status code the server span records, and whether the caller's verification is its child."""
    spans = recorded_spans.get_finished_spans()
    (server,) = [s for s in spans if s.kind is trace.SpanKind.SERVER]
    (verification,) = [s for s in spans if s.name == 'auth.verify_caller']
    assert server.name == _PATH
    assert server.attributes is not None
    assert server.context is not None
    inside = verification.parent is not None and verification.parent.span_id == server.context.span_id
    return server.attributes['rpc.grpc.status_code'], inside


def test_an_admitted_call_is_one_span_with_the_gate_inside(
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    assert _call(fixture.WEB) is grpc.StatusCode.OK

    assert _server_status_and_verification_parent(recorded_spans) == (grpc.StatusCode.OK.value[0], True)


def test_a_denied_call_is_traced_with_the_status_it_ended_with(
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    assert _call(()) is grpc.StatusCode.UNAUTHENTICATED

    assert _server_status_and_verification_parent(recorded_spans) == (grpc.StatusCode.UNAUTHENTICATED.value[0], True)


def _stream(path: str, metadata: fixture.Metadata) -> tuple[grpc.StatusCode, list[bytes]]:
    """Drive one stream-out call at `path` through the observed gate; its status and what it streamed."""

    async def two_chunks(request: bytes, context: grpc.aio.ServicerContext) -> AsyncIterator[bytes]:
        del request, context
        yield b'a'
        yield b'b'

    async def run() -> tuple[grpc.StatusCode, list[bytes]]:
        server = interceptor_mod.gated_server(fixture.authorizer(), observers=[tracing.server_interceptor()])
        service, method = path.lstrip('/').split('/')
        handler = grpc.unary_stream_rpc_method_handler(two_chunks, bytes, bytes)
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(service, {method: handler}),))
        port = server.add_insecure_port('127.0.0.1:0')
        await server.start()
        received: list[bytes] = []
        try:
            async with grpc.aio.insecure_channel(f'127.0.0.1:{port}') as channel:
                call = channel.unary_stream(path, request_serializer=bytes, response_deserializer=bytes)
                try:
                    async for chunk in call(b'', metadata=claim_mod.pairs(metadata)):
                        received.append(chunk)
                except grpc.aio.AioRpcError as e:
                    return e.code(), received
                return grpc.StatusCode.OK, received
        finally:
            await server.stop(None)

    return asyncio.run(run())


def test_an_observed_stream_out_call_is_admitted_and_streams(
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    del recorded_spans
    assert _stream(_FETCH_PACK, fixture.WORKER) == (grpc.StatusCode.OK, [b'a', b'b'])


def test_an_observed_stream_out_path_no_contract_declares_is_denied_as_itself(
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    # The observer iterates the handler it wraps; the gate's denial has to reach the client as the denial.
    del recorded_spans
    assert _stream('/themis.rpc.nowhere.Nothing/Stream', fixture.WORKER) == (grpc.StatusCode.PERMISSION_DENIED, [])
