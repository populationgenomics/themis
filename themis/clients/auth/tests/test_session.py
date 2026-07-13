"""Behaviour tests for the session resolver + servicer guard.

The resolver and the ``require_session`` guard are exercised over real in-process ``grpc.aio``
servers (a real ``ServicerContext``, real metadata, real status codes) — the credential-bearing
``session_resolver``/``id_token`` construction only works on GCE and is validated at deploy.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import grpc
import grpc.aio
import pytest

from themis.clients.auth import session
from themis.rpc import auth_pb2, auth_pb2_grpc


class _StubAuth(auth_pb2_grpc.AuthServicer):
    """A minimal auth service: resolves ``good`` to a binding, aborts every other token.

    ``abort_code`` selects the failure: ``PERMISSION_DENIED`` models an unresolvable token,
    any other code (e.g. ``UNAVAILABLE``) models a transport/backend failure.
    """

    def __init__(self, abort_code: grpc.StatusCode = grpc.StatusCode.PERMISSION_DENIED) -> None:
        self._abort_code = abort_code

    async def ResolveSession(
        self, request: auth_pb2.ResolveTokenRequest, context: grpc.aio.ServicerContext
    ) -> auth_pb2.SessionContext:
        if request.session_token != 'good':
            await context.abort(self._abort_code, 'unknown token')
        return auth_pb2.SessionContext(project_id='p1', analysis_id='a1')


class _GuardServicer(auth_pb2_grpc.AuthServicer):
    """Hosts ``require_session`` so it runs under a real context; returns the resolved binding."""

    def __init__(self, session_resolver: session.SessionResolver) -> None:
        self._session_resolver = session_resolver

    async def ResolveSession(
        self, request: auth_pb2.ResolveTokenRequest, context: grpc.aio.ServicerContext
    ) -> auth_pb2.SessionContext:
        return await session.require_session(context, self._session_resolver)


@contextlib.asynccontextmanager
async def _serving(servicer: auth_pb2_grpc.AuthServicer) -> AsyncIterator[auth_pb2_grpc.AuthStub]:
    server = grpc.aio.server()
    auth_pb2_grpc.add_AuthServicer_to_server(servicer, server)
    port = server.add_insecure_port('127.0.0.1:0')
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f'127.0.0.1:{port}') as channel:
            yield auth_pb2_grpc.AuthStub(channel)
    finally:
        await server.stop(None)


async def _fixture_session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='p1', analysis_id='a1')
    raise session.UnresolvedSessionError


def test_session_resolver_returns_the_binding() -> None:
    async def run() -> auth_pb2.SessionContext:
        async with _serving(_StubAuth()) as stub:
            return await session._session_resolver_over_stub(stub)('good')

    result = asyncio.run(run())
    assert result.project_id == 'p1'
    assert result.analysis_id == 'a1'


def test_session_resolver_maps_permission_denied_to_unresolved() -> None:
    async def run() -> auth_pb2.SessionContext:
        async with _serving(_StubAuth()) as stub:
            return await session._session_resolver_over_stub(stub)('nope')

    with pytest.raises(session.UnresolvedSessionError):
        asyncio.run(run())


def test_session_resolver_propagates_non_permission_denied() -> None:
    # An auth outage (UNAVAILABLE) must surface loudly, not masquerade as a bad token.
    async def run() -> auth_pb2.SessionContext:
        async with _serving(_StubAuth(grpc.StatusCode.UNAVAILABLE)) as stub:
            return await session._session_resolver_over_stub(stub)('nope')

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code() is grpc.StatusCode.UNAVAILABLE


def test_require_session_returns_the_binding() -> None:
    async def run() -> auth_pb2.SessionContext:
        async with _serving(_GuardServicer(_fixture_session_resolver)) as stub:
            return await stub.ResolveSession(
                auth_pb2.ResolveTokenRequest(),
                metadata=(('x-themis-session-token', 'good'),),
            )

    result = asyncio.run(run())
    assert result.project_id == 'p1'


def test_require_session_missing_token_is_unauthenticated() -> None:
    async def run() -> None:
        async with _serving(_GuardServicer(_fixture_session_resolver)) as stub:
            await stub.ResolveSession(auth_pb2.ResolveTokenRequest())

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code() is grpc.StatusCode.UNAUTHENTICATED


def test_require_session_unresolvable_token_is_permission_denied() -> None:
    async def run() -> None:
        async with _serving(_GuardServicer(_fixture_session_resolver)) as stub:
            await stub.ResolveSession(
                auth_pb2.ResolveTokenRequest(),
                metadata=(('x-themis-session-token', 'bad'),),
            )

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code() is grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.parametrize(
    ('auth_url', 'expected'),
    [
        ('https://auth-abc-uc.a.run.app', 'auth-abc-uc.a.run.app:443'),
        ('https://auth-abc-uc.a.run.app/', 'auth-abc-uc.a.run.app:443'),
        ('http://localhost:50051', 'localhost:50051'),
    ],
)
def test_target_yields_host_port(auth_url: str, expected: str) -> None:
    assert session._target(auth_url) == expected


def test_session_resolver_from_env_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_AUTH_URL', raising=False)
    with pytest.raises(SystemExit):
        session.session_resolver_from_env()
