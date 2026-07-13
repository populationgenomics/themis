"""Behaviour tests for the auth servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import grpc
import grpc.aio
import pytest

from themis.rpc import auth_pb2, auth_pb2_grpc
from themis.services.auth import backend as backend_mod
from themis.services.auth import servicer as servicer_mod


def _resolve(bindings: Mapping[str, auth_pb2.SessionContext], token: str) -> auth_pb2.SessionContext:
    """Drive one Resolve call through a real in-process server + stub."""

    async def run() -> auth_pb2.SessionContext:
        server = grpc.aio.server()
        auth_pb2_grpc.add_AuthServicer_to_server(servicer_mod.Servicer(backend_mod.FixtureBackend(bindings)), server)
        port = server.add_insecure_port('127.0.0.1:0')
        await server.start()
        try:
            async with grpc.aio.insecure_channel(f'127.0.0.1:{port}') as channel:
                stub = auth_pb2_grpc.AuthStub(channel)
                return await stub.ResolveSession(auth_pb2.ResolveTokenRequest(session_token=token))
        finally:
            await server.stop(None)

    return asyncio.run(run())


def test_resolve_returns_the_binding() -> None:
    context = auth_pb2.SessionContext(project_id='proj-1', analysis_id='ana-1')
    result = _resolve({backend_mod.hash_token('tok-123'): context}, 'tok-123')
    assert result.project_id == 'proj-1'
    assert result.analysis_id == 'ana-1'


def test_unknown_token_is_permission_denied() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        _resolve({}, 'nope')
    assert exc_info.value.code() is grpc.StatusCode.PERMISSION_DENIED
