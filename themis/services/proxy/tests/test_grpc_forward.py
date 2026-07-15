"""Tests for the generic gRPC forward proxy (passthrough, metadata injection, method allowlist)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import grpc
import grpc.aio
import pytest

from themis.services.proxy import grpc_forward

_ALLOWED = '/echo.Echo/Say'


async def _echo(request_iterator: AsyncIterator[bytes], context: grpc.aio.ServicerContext) -> AsyncIterator[bytes]:
    # grpc's stubs type metadata keys as bytes; at runtime text keys/values are str.
    received = dict(context.invocation_metadata() or ())  # pyright: ignore[reportArgumentType, reportCallIssue]
    token = received.get('x-themis-session-token', '<none>')  # pyright: ignore[reportArgumentType, reportCallIssue]
    async for message in request_iterator:
        yield f'tok={token};'.encode() + message


class _EchoAny(grpc.GenericRpcHandler):
    def service(self, handler_call_details: grpc.HandlerCallDetails) -> grpc.RpcMethodHandler:
        del handler_call_details
        return grpc.stream_stream_rpc_method_handler(_echo, None, None)


@contextlib.asynccontextmanager
async def _proxied() -> AsyncIterator[grpc.aio.Channel]:
    upstream = grpc.aio.server()
    upstream.add_generic_rpc_handlers((_EchoAny(),))
    upstream_port = upstream.add_insecure_port('127.0.0.1:0')
    await upstream.start()
    upstream_channel = grpc.aio.insecure_channel(f'127.0.0.1:{upstream_port}')

    proxy = grpc.aio.server()
    proxy.add_generic_rpc_handlers(
        (grpc_forward.ForwardProxy(upstream_channel, allowed_methods=[_ALLOWED], session_token='ST'),)
    )
    proxy_port = proxy.add_insecure_port('127.0.0.1:0')
    await proxy.start()
    try:
        async with grpc.aio.insecure_channel(f'127.0.0.1:{proxy_port}') as client:
            yield client
    finally:
        await proxy.stop(None)
        await upstream_channel.close()
        await upstream.stop(None)


async def _call(client: grpc.aio.Channel, method: str, payload: bytes) -> list[bytes]:
    multicallable = client.stream_stream(method, request_serializer=None, response_deserializer=None)

    async def one() -> AsyncIterator[bytes]:
        yield payload

    return [response async for response in multicallable(one())]


def test_forwards_and_injects_the_session_token() -> None:
    async def run() -> list[bytes]:
        async with _proxied() as client:
            return await _call(client, _ALLOWED, b'hello')

    assert asyncio.run(run()) == [b'tok=ST;hello']


def test_rejects_an_off_allowlist_method() -> None:
    async def run() -> list[bytes]:
        async with _proxied() as client:
            return await _call(client, '/secret.Admin/Drop', b'x')

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code() is grpc.StatusCode.PERMISSION_DENIED
