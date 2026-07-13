"""Behaviour tests for the hello servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import grpc
import grpc.aio
import pytest

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, hello_pb2, hello_pb2_grpc
from themis.services.hello import servicer as servicer_mod

_GOOD_TOKEN = (('x-themis-session-token', 'good'),)


async def _session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='proj', analysis_id='ana')
    raise session_mod.UnresolvedSessionError


@contextlib.asynccontextmanager
async def _serving() -> AsyncIterator[hello_pb2_grpc.HelloStub]:
    server = grpc.aio.server()
    hello_pb2_grpc.add_HelloServicer_to_server(servicer_mod.Servicer(_session_resolver), server)
    port = server.add_insecure_port('127.0.0.1:0')
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f'127.0.0.1:{port}') as channel:
            yield hello_pb2_grpc.HelloStub(channel)
    finally:
        await server.stop(None)


def test_say_hello_echoes_the_resolved_binding() -> None:
    async def run() -> hello_pb2.SayHelloResponse:
        async with _serving() as stub:
            return await stub.SayHello(hello_pb2.SayHelloRequest(note='hi'), metadata=_GOOD_TOKEN)

    reply = asyncio.run(run())
    assert reply.analysis_id == 'ana'
    assert reply.project_id == 'proj'
    assert reply.greeting == 'hello from analysis ana: hi'


def test_missing_session_token_is_unauthenticated() -> None:
    async def run() -> hello_pb2.SayHelloResponse:
        async with _serving() as stub:
            return await stub.SayHello(hello_pb2.SayHelloRequest(note='hi'))

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code() is grpc.StatusCode.UNAUTHENTICATED


def test_unresolvable_token_is_permission_denied() -> None:
    async def run() -> hello_pb2.SayHelloResponse:
        async with _serving() as stub:
            return await stub.SayHello(
                hello_pb2.SayHelloRequest(note='hi'), metadata=(('x-themis-session-token', 'bad'),)
            )

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code() is grpc.StatusCode.PERMISSION_DENIED
