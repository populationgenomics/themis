"""Behaviour tests for the hello servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import grpc
import grpc.aio
import pytest

from themis.clients.auth.tests import fixture_session
from themis.rpc import hello_pb2, hello_pb2_grpc
from themis.services.hello import servicer as servicer_mod
from themis.testing import in_process_grpc


@contextlib.asynccontextmanager
async def _serving() -> AsyncIterator[hello_pb2_grpc.HelloStub]:
    servicer = servicer_mod.Servicer(fixture_session.resolve_fixture_session)
    async with in_process_grpc.serving(
        lambda server: hello_pb2_grpc.add_HelloServicer_to_server(servicer, server)
    ) as channel:
        yield hello_pb2_grpc.HelloStub(channel)


def test_say_hello_echoes_the_resolved_binding() -> None:
    async def run() -> hello_pb2.SayHelloResponse:
        async with _serving() as stub:
            return await stub.SayHello(hello_pb2.SayHelloRequest(note='hi'), metadata=fixture_session.GOOD_METADATA)

    reply = asyncio.run(run())
    assert reply.analysis_id == fixture_session.ANALYSIS_ID
    assert reply.project_id == fixture_session.PROJECT_ID
    assert reply.greeting == f'hello from analysis {fixture_session.ANALYSIS_ID}: hi'


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
                hello_pb2.SayHelloRequest(note='hi'), metadata=fixture_session.session_metadata('bad')
            )

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code() is grpc.StatusCode.PERMISSION_DENIED
