"""Each call the server handles is recorded once, with the status it ended in, however it ended."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import grpc
import grpc.aio
import pytest
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from opentelemetry.sdk import metrics as sdk_metrics
from opentelemetry.sdk.metrics import export as metrics_export

from themis.clients.auth import claim as claim_mod
from themis.clients.auth import interceptor as interceptor_mod
from themis.rpc import literature_pb2
from themis.telemetry import operational_names, rpc_metrics
from themis.testing import auth as auth_fixture
from themis.testing import in_process_grpc

_SERVICE = 'themis.test.Probe'
# A contract rpc that streams out and admits the agent: what the gate lets through, or denies, for real.
_READ_MARKDOWN = literature_pb2.DESCRIPTOR.services_by_name['Literature'].methods_by_name['ReadMarkdown']
_STREAM_PATH = f'/{_READ_MARKDOWN.full_name.rsplit(".", 1)[0]}/{_READ_MARKDOWN.name}'
_SETTLE_SECONDS = 2.0


async def _ok(request: bytes, _context: grpc.aio.ServicerContext[bytes, bytes]) -> bytes:
    return request


async def _set_internal(_request: bytes, context: grpc.aio.ServicerContext[bytes, bytes]) -> bytes:
    context.set_code(grpc.StatusCode.INTERNAL)
    return b''


async def _abort_not_found(_request: bytes, context: grpc.aio.ServicerContext[bytes, bytes]) -> bytes:
    await context.abort(grpc.StatusCode.NOT_FOUND, 'no such thing')


async def _raise(_request: bytes, _context: grpc.aio.ServicerContext[bytes, bytes]) -> bytes:
    raise RuntimeError('a bug the handler did not turn into a status')


async def _hang(_request: bytes, _context: grpc.aio.ServicerContext[bytes, bytes]) -> bytes:
    await asyncio.Event().wait()
    return b''


async def _stream(_request: bytes, _context: grpc.aio.ServicerContext[bytes, bytes]) -> AsyncIterator[bytes]:
    yield b'a'
    yield b'b'


_HANDLERS = {
    'Ok': grpc.unary_unary_rpc_method_handler(_ok),
    'SetInternal': grpc.unary_unary_rpc_method_handler(_set_internal),
    'AbortNotFound': grpc.unary_unary_rpc_method_handler(_abort_not_found),
    'Raise': grpc.unary_unary_rpc_method_handler(_raise),
    'Hang': grpc.unary_unary_rpc_method_handler(_hang),
    'Stream': grpc.unary_stream_rpc_method_handler(_stream),
}


def _calls(reader: metrics_export.InMemoryMetricReader) -> dict[tuple[str, str], int]:
    """Calls recorded so far, by method and status."""
    data = reader.get_metrics_data()
    calls: dict[tuple[str, str], int] = {}
    if data is None:
        return calls
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != operational_names.RPC_SERVER_CALL_DURATION:
                    continue
                assert metric.unit == 's'
                for point in metric.data.data_points:
                    assert isinstance(point, metrics_export.HistogramDataPoint)
                    attributes = dict(point.attributes or {})
                    assert attributes[operational_names.RPC_SYSTEM_NAME_LABEL] == operational_names.RPC_SYSTEM_GRPC
                    key = (
                        str(attributes[operational_names.RPC_METHOD_LABEL]),
                        str(attributes[operational_names.RPC_RESPONSE_STATUS_CODE_LABEL]),
                    )
                    calls[key] = point.count
    return calls


async def _recorded(reader: metrics_export.InMemoryMetricReader, key: tuple[str, str]) -> dict[tuple[str, str], int]:
    # The server records as the call's task finishes, which can trail the client seeing its status.
    async with asyncio.timeout(_SETTLE_SECONDS):
        while key not in _calls(reader):
            await asyncio.sleep(0.01)
    return _calls(reader)


async def _register(server: grpc.aio.Server) -> None:
    server.add_generic_rpc_handlers([grpc.method_handlers_generic_handler(_SERVICE, _HANDLERS)])
    # grpc_health ships no py.typed; `health.aio` is a runtime re-export pyright can't see.
    health_servicer = health.aio.HealthServicer()  # pyright: ignore[reportAttributeAccessIssue]
    await health_servicer.set('', health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)


def _interceptor() -> tuple[rpc_metrics.RpcMetrics, metrics_export.InMemoryMetricReader]:
    reader = metrics_export.InMemoryMetricReader()
    meter = sdk_metrics.MeterProvider(metric_readers=[reader]).get_meter('themis-test')
    return rpc_metrics.RpcMetrics(meter), reader


def test_a_unary_call_that_succeeds_is_recorded_ok() -> None:
    interceptor, reader = _interceptor()

    async def run() -> dict[tuple[str, str], int]:
        async with in_process_grpc.serving(_register, server_interceptors=[interceptor]) as channel:
            await channel.unary_unary(f'/{_SERVICE}/Ok')(b'x')
            return await _recorded(reader, (f'{_SERVICE}/Ok', 'OK'))

    assert asyncio.run(run()) == {(f'{_SERVICE}/Ok', 'OK'): 1}


@pytest.mark.parametrize(
    ('method', 'status'),
    [
        pytest.param('SetInternal', grpc.StatusCode.INTERNAL, id='set-code'),
        pytest.param('AbortNotFound', grpc.StatusCode.NOT_FOUND, id='abort'),
        pytest.param('Raise', grpc.StatusCode.UNKNOWN, id='uncaught-exception'),
    ],
)
def test_a_unary_call_that_fails_is_recorded_with_the_status_the_client_saw(
    method: str, status: grpc.StatusCode
) -> None:
    interceptor, reader = _interceptor()

    async def run() -> dict[tuple[str, str], int]:
        async with in_process_grpc.serving(_register, server_interceptors=[interceptor]) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await channel.unary_unary(f'/{_SERVICE}/{method}')(b'x')
            assert raised.value.code() == status
            return await _recorded(reader, (f'{_SERVICE}/{method}', status.name))

    assert asyncio.run(run()) == {(f'{_SERVICE}/{method}', status.name): 1}


def test_a_stream_is_recorded_once_when_it_ends() -> None:
    interceptor, reader = _interceptor()

    async def run() -> dict[tuple[str, str], int]:
        async with in_process_grpc.serving(_register, server_interceptors=[interceptor]) as channel:
            responses = [response async for response in channel.unary_stream(f'/{_SERVICE}/Stream')(b'')]
            assert responses == [b'a', b'b']
            return await _recorded(reader, (f'{_SERVICE}/Stream', 'OK'))

    assert asyncio.run(run()) == {(f'{_SERVICE}/Stream', 'OK'): 1}


def test_a_call_the_client_cancels_is_recorded_as_cancelled() -> None:
    interceptor, reader = _interceptor()

    async def run() -> dict[tuple[str, str], int]:
        async with in_process_grpc.serving(_register, server_interceptors=[interceptor]) as channel:
            call = channel.unary_unary(f'/{_SERVICE}/Hang')(b'')
            await asyncio.sleep(0.1)  # let the call reach the handler
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            return await _recorded(reader, (f'{_SERVICE}/Hang', 'CANCELLED'))

    assert asyncio.run(run()) == {(f'{_SERVICE}/Hang', 'CANCELLED'): 1}


def test_the_health_check_is_not_recorded() -> None:
    interceptor, reader = _interceptor()

    async def run() -> dict[tuple[str, str], int]:
        async with in_process_grpc.serving(_register, server_interceptors=[interceptor]) as channel:
            # grpc_health ships no py.typed, so its stub's methods are invisible to pyright.
            await health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest())  # pyright: ignore[reportAttributeAccessIssue]
            await channel.unary_unary(f'/{_SERVICE}/Ok')(b'')
            return await _recorded(reader, (f'{_SERVICE}/Ok', 'OK'))

    assert asyncio.run(run()) == {(f'{_SERVICE}/Ok', 'OK'): 1}


def test_a_call_past_its_deadline_is_recorded_as_deadline_exceeded() -> None:
    interceptor, reader = _interceptor()

    async def run() -> dict[tuple[str, str], int]:
        async with in_process_grpc.serving(_register, server_interceptors=[interceptor]) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await channel.unary_unary(f'/{_SERVICE}/Hang')(b'', timeout=0.2)
            assert raised.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
            return await _recorded(reader, (f'{_SERVICE}/Hang', 'DEADLINE_EXCEEDED'))

    assert asyncio.run(run()) == {(f'{_SERVICE}/Hang', 'DEADLINE_EXCEEDED'): 1}


async def _abort_mid_stream(_request: bytes, context: grpc.aio.ServicerContext[bytes, bytes]) -> AsyncIterator[bytes]:
    yield b'a'
    await context.abort(grpc.StatusCode.FAILED_PRECONDITION, 'the rendering went away')


async def _stream_then_hang(_request: bytes, _context: grpc.aio.ServicerContext[bytes, bytes]) -> AsyncIterator[bytes]:
    yield b'a'
    await asyncio.Event().wait()


@pytest.mark.parametrize(
    ('caller', 'behavior', 'cancel_after_first', 'status'),
    [
        # The gate's own denial is recorded with the status it ended in — the caller's, not a server fault.
        pytest.param(auth_fixture.WEB, _stream_then_hang, False, grpc.StatusCode.PERMISSION_DENIED, id='gate-denial'),
        pytest.param(auth_fixture.AGENT, _abort_mid_stream, False, grpc.StatusCode.FAILED_PRECONDITION, id='abort'),
        pytest.param(auth_fixture.AGENT, _stream_then_hang, True, grpc.StatusCode.CANCELLED, id='client-cancel'),
    ],
)
def test_a_gated_stream_is_recorded_with_the_status_it_ended_in(
    caller: auth_fixture.Metadata,
    behavior: Callable[[bytes, grpc.aio.ServicerContext[bytes, bytes]], AsyncIterator[bytes]],
    cancel_after_first: bool,
    status: grpc.StatusCode,
) -> None:
    interceptor, reader = _interceptor()
    method = _STREAM_PATH.removeprefix('/')
    service, name = method.split('/')
    gate = interceptor_mod.AuthInterceptor(auth_fixture.authorizer())

    def register(server: grpc.aio.Server) -> None:
        handler = grpc.unary_stream_rpc_method_handler(behavior)
        server.add_generic_rpc_handlers([grpc.method_handlers_generic_handler(service, {name: handler})])

    async def run() -> dict[tuple[str, str], int]:
        # The observer ahead of the gate, as `interceptor.gated_server` builds a server.
        async with in_process_grpc.serving(register, server_interceptors=[interceptor, gate]) as channel:
            call = channel.unary_stream(_STREAM_PATH)(b'', metadata=claim_mod.pairs(caller))
            if cancel_after_first:
                assert await call.read() == b'a'
                call.cancel()
            else:
                with pytest.raises(grpc.aio.AioRpcError) as raised:
                    async for _ in call:
                        pass
                assert raised.value.code() == status
            return await _recorded(reader, (method, status.name))

    assert asyncio.run(run()) == {(method, status.name): 1}
