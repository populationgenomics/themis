"""The rpc metric a gRPC server records: how long each call took and how it ended.

One histogram, OpenTelemetry's `rpc.server.call.duration` in seconds, labelled with each call's method and final
status code, so the calls that ended in a server fault are a count over its series. The interceptor is an observer
(`interceptor.gated_server`), so it times the gate as well and records the gate's denials with the status they ended in.
The health check is left out, as from tracing: Cloud Run probes it. A method the server does not implement never
reaches a handler, so it is never recorded and `rpc.method` stays within the methods the server serves.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import typing
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import override

import grpc
import grpc.aio
from opentelemetry import metrics

from themis.telemetry import operational_names as names

_HEALTH_SERVICE = '/grpc.health.v1.Health/'
# The semantic conventions' advisory boundaries, extended to a minute: evidence waits on upstreams for tens of seconds.
_BOUNDARIES = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0, 30.0, 60.0)

_Behavior = Callable[[object, grpc.aio.ServicerContext[object, object]], Awaitable[object]]
_StreamBehavior = Callable[[object, grpc.aio.ServicerContext[object, object]], AsyncIterator[object]]


class RpcMetrics(grpc.aio.ServerInterceptor):
    """Records `rpc.server.call.duration` for every call the server handles but the health check."""

    def __init__(self, meter: metrics.Meter) -> None:
        self._duration = meter.create_histogram(
            names.RPC_SERVER_CALL_DURATION,
            unit='s',
            description='How long each call took, by method and final status.',
            explicit_bucket_boundaries_advisory=_BOUNDARIES,
        )

    @override
    async def intercept_service[TRequest, TResponse](
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[TRequest, TResponse] | None]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[TRequest, TResponse] | None:
        handler = await continuation(handler_call_details)
        path = handler_call_details.method
        if handler is None or path.startswith(_HEALTH_SERVICE):
            return handler
        observed = self._observed(typing.cast('grpc.RpcMethodHandler[object, object]', handler), path.removeprefix('/'))
        return typing.cast('grpc.RpcMethodHandler[TRequest, TResponse]', observed)

    def _record(self, method: str, started: float, status: grpc.StatusCode) -> None:
        self._duration.record(
            time.monotonic() - started,
            {
                names.RPC_SYSTEM_NAME_LABEL: names.RPC_SYSTEM_GRPC,
                names.RPC_METHOD_LABEL: method,
                names.RPC_RESPONSE_STATUS_CODE_LABEL: status.name,
            },
        )

    def _observed(
        self, handler: grpc.RpcMethodHandler[object, object], method: str
    ) -> grpc.RpcMethodHandler[object, object]:
        deserializer, serializer = handler.request_deserializer, handler.response_serializer
        if handler.unary_unary is not None:
            return grpc.unary_unary_rpc_method_handler(
                self._timed(typing.cast('_Behavior', handler.unary_unary), method), deserializer, serializer
            )
        if handler.stream_unary is not None:
            return grpc.stream_unary_rpc_method_handler(
                self._timed(typing.cast('_Behavior', handler.stream_unary), method), deserializer, serializer
            )
        if handler.unary_stream is not None:
            return grpc.unary_stream_rpc_method_handler(
                self._timed_stream(handler.unary_stream, method), deserializer, serializer
            )
        if handler.stream_stream is not None:
            return grpc.stream_stream_rpc_method_handler(
                self._timed_stream(handler.stream_stream, method), deserializer, serializer
            )
        raise ValueError(f'{method}: a method handler with no behaviour')

    def _timed(self, behavior: _Behavior, method: str) -> _Behavior:
        async def timed(request: object, context: grpc.aio.ServicerContext[object, object]) -> object:
            started = time.monotonic()
            error: BaseException | None = None
            try:
                return await behavior(request, context)
            except BaseException as e:
                error = e
                raise
            finally:
                self._record(method, started, _final_status(context, error))

        return timed

    def _timed_stream(self, behavior: Callable[..., object], method: str) -> Callable[..., object]:
        # A stream-out handler is an async generator or a coroutine that writes through `context.write()`; gRPC tells
        # the two apart by the behaviour's own shape, so the wrapper keeps it.
        if not inspect.isasyncgenfunction(behavior):
            return self._timed(typing.cast('_Behavior', behavior), method)
        streaming = typing.cast('_StreamBehavior', behavior)

        async def timed(request: object, context: grpc.aio.ServicerContext[object, object]) -> AsyncIterator[object]:
            started = time.monotonic()
            error: BaseException | None = None
            try:
                async for response in streaming(request, context):
                    yield response
            except BaseException as e:
                error = e
                raise
            finally:
                self._record(method, started, _final_status(context, error))

        return timed


def _final_status(context: grpc.aio.ServicerContext[object, object], error: BaseException | None) -> grpc.StatusCode:
    """The status the call ended with: what the handler set or aborted with, else what its exception becomes.

    A call cut off mid-way is `DEADLINE_EXCEEDED` when its deadline had passed and `CANCELLED` otherwise — the client
    went away; an exception the handler did not turn into a status is `UNKNOWN`, as gRPC reports it to the client.
    """
    if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
        remaining = context.time_remaining()
        if remaining is not None and remaining <= 0:
            return grpc.StatusCode.DEADLINE_EXCEEDED
        return grpc.StatusCode.CANCELLED
    code = context.code()
    if code is not None:
        return code
    return grpc.StatusCode.OK if error is None else grpc.StatusCode.UNKNOWN
