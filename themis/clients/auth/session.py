"""Resolve a request's session token to its Project + Analysis binding via the auth service.

Every data-plane servicer authorizes a request by resolving the ``x-themis-session-token``
metadata to a ``SessionContext`` through auth. ``session_resolver`` builds the resolver over the
generated auth stub (presenting the SA ID token via ``themis.clients.id_token``); ``require_session``
is the servicer guard that reads the metadata, resolves it, and aborts the RPC on a missing or
unresolvable token — it never returns ``None``, so a servicer cannot proceed without a binding.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable

import grpc
import grpc.aio
from google.protobuf import json_format

from themis.clients import id_token
from themis.rpc import auth_pb2, auth_pb2_grpc

_SESSION_TOKEN_METADATA = 'x-themis-session-token'  # noqa: S105 — a metadata key name, not a secret

SessionResolver = Callable[[str], Awaitable[auth_pb2.SessionContext]]


class UnresolvedSessionError(Exception):
    """The session token did not resolve to a binding (unknown, expired, or revoked)."""


def session_resolver(auth_url: str) -> SessionResolver:
    """Build a ``SessionResolver`` calling the auth service at ``auth_url``.

    The channel presents the runtime SA's ID token (audience = ``auth_url``). A
    ``PERMISSION_DENIED`` (the auth service's verdict on an unresolvable token) becomes
    ``UnresolvedSessionError``; any other gRPC failure — an outage, timeout, or IAM
    misconfiguration — propagates so it surfaces loudly rather than as a bad token.
    """
    channel = grpc.aio.secure_channel(_target(auth_url), id_token.channel_credentials(auth_url))
    return _session_resolver_over_stub(auth_pb2_grpc.AuthStub(channel))


def session_resolver_from_env() -> SessionResolver:
    """Build a ``SessionResolver`` from ``THEMIS_AUTH_URL`` (fail-loud if unset)."""
    auth_url = os.environ.get('THEMIS_AUTH_URL')
    if not auth_url:
        raise SystemExit('THEMIS_AUTH_URL is required to reach the auth service')
    return session_resolver(auth_url)


def fixture_session_resolver_from_json(raw: str | None, *, var_name: str) -> SessionResolver:
    """Build an offline ``SessionResolver`` from a JSON bearer→binding map.

    A service's offline/first-deploy authorizer: instead of reaching the auth service, resolve each
    token against an explicitly-seeded map. Shared by every data-plane service's ``__main__`` fixture
    branch so the JSON parsing and fail-loud validation live in one place.

    Args:
        raw: The JSON string — an object mapping each plaintext bearer to its binding, e.g.
            ``{"tok": {"project_id": "p1", "analysis_id": "a1"}}``. ``None`` (an unset env var) is an
            operator error; pass ``"{}"`` for an explicit empty set.
        var_name: The source env var, named in the fail-loud error messages.

    Returns:
        A ``SessionResolver`` returning the seeded ``SessionContext`` for a known bearer and raising
        ``UnresolvedSessionError`` on one it does not hold — matching the http resolver's contract.
    """
    if raw is None:
        raise SystemExit(
            f'{var_name} is required for the fixture authorizer: a JSON object of bearer -> binding, '
            'or "{}" for an explicit empty set'
        )
    try:
        seeds = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f'{var_name} is not valid JSON: {e}') from e
    if not isinstance(seeds, dict):
        raise SystemExit(f'{var_name} must be a JSON object of bearer -> binding, got {type(seeds).__name__}')
    contexts = {token: _parse_binding(binding, var_name=var_name) for token, binding in seeds.items()}

    async def session_resolver(session_token: str) -> auth_pb2.SessionContext:
        try:
            return contexts[session_token]
        except KeyError:
            raise UnresolvedSessionError from None

    return session_resolver


def _parse_binding(binding: object, *, var_name: str) -> auth_pb2.SessionContext:
    """Parse and validate one fixture binding into a ``SessionContext`` (fail-loud)."""
    if not isinstance(binding, dict):
        raise SystemExit(f'{var_name} binding must be a JSON object')
    try:
        context = json_format.ParseDict(binding, auth_pb2.SessionContext())
    except json_format.ParseError as e:
        raise SystemExit(f'{var_name} binding is malformed: {e}') from e
    if not context.project_id or not context.analysis_id:
        raise SystemExit(f'{var_name} binding must set project_id and analysis_id')
    return context


async def require_session(
    context: grpc.aio.ServicerContext, session_resolver: SessionResolver
) -> auth_pb2.SessionContext:
    """Resolve the request's session token or abort the RPC.

    Reads the ``x-themis-session-token`` metadata and resolves it through ``session_resolver``.
    Aborts ``UNAUTHENTICATED`` on a missing token, ``PERMISSION_DENIED`` on one that does not
    resolve. Never returns ``None``: a servicer cannot proceed without a binding.

    Args:
        context: The gRPC servicer context for the current call.
        session_resolver: Maps the session token to its binding.

    Returns:
        The resolved ``SessionContext``.
    """
    token = _session_token(context)
    if token is None:
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, 'missing session token')
    try:
        return await session_resolver(token)
    except UnresolvedSessionError:
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, 'session token could not be resolved')


def _session_resolver_over_stub(stub: auth_pb2_grpc.AuthStub) -> SessionResolver:
    async def session_resolver(session_token: str) -> auth_pb2.SessionContext:
        try:
            return await stub.ResolveSession(auth_pb2.ResolveTokenRequest(session_token=session_token))
        except grpc.aio.AioRpcError as e:
            # Only an unresolvable token is PERMISSION_DENIED; every other code (auth outage,
            # timeout, IAM misconfig) surfaces loudly rather than as a bogus bad-token.
            if e.code() is grpc.StatusCode.PERMISSION_DENIED:
                raise UnresolvedSessionError from e
            raise

    return session_resolver


def _session_token(context: grpc.aio.ServicerContext) -> str | None:
    metadata = context.invocation_metadata()
    if metadata is None:
        return None
    for key, value in metadata:
        if key == _SESSION_TOKEN_METADATA:
            return value
    return None


def _target(auth_url: str) -> str:
    """Strip the scheme from a Cloud Run URL, yielding the ``host:port`` gRPC target (default 443)."""
    host = auth_url.split('://', 1)[-1].rstrip('/')
    if ':' in host:
        return host
    return f'{host}:443'
