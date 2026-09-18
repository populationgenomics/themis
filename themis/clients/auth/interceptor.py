"""The server interceptor that gates every data-plane call (rpc-authorization.md).

For each inbound call it verifies the calling service account from the ID token Cloud Run forwarded,
reads the caller's claim about what it is calling as, resolves the session that claim names, and builds
the call's :class:`~themis.clients.auth.context.AuthContext` from the three. It then derives the rule
the rpc's contract declares — the principals the rpc admits — and denies when none is the call. It is
server-wide by construction: a gRPC interceptor sees every method path before any handler runs, so an
rpc whose contract admits nobody is denied without anyone having remembered to gate it, and a path no
imported contract declares is refused before any rule is consulted — it is not an rpc, so not even the
developer identity, admitted on every rpc there is, reaches it. The health check is the one exemption,
probed by Cloud Run without credentials.

What is verified and what is trusted: the account is verified, by Google's signature on the token; the
claim is trusted, because the account is trusted to make it, and is checked only for being one the
account has a member for. Several outcomes are not denials and are surfaced as what they are: a caller
whose ID token does not verify is ``UNAUTHENTICATED``, since Cloud Run admits no call without a valid
token and one this service cannot re-verify is a deployment fault or a broken guarantee; a claim the
callee cannot honour — none from an account that never calls as itself, one that names a session and
carries no token, one that does not decode — is ``INTERNAL``, a caller-side fault. A session token that
does not resolve is no binding, so a member that needs one is not satisfied and the call is denied. A
session-resolver failure that is not "the token does not resolve" propagates, for any call presenting a
token: a context built with the token ignored would attribute the call wrongly.

Only unary rpcs are gated; no server this interceptor fronts declares a streaming one, and a streaming
handler is refused loudly rather than passed through ungated.

``gated_server`` is the one way an evidence image builds its server: a server built any other way has
no interceptor, and default-deny would depend on remembering to add one.
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import override

import grpc
import grpc.aio

from themis.clients.auth import caller as caller_mod
from themis.clients.auth import claim as claim_mod
from themis.clients.auth import context as context_mod
from themis.clients.auth import rules as rules_mod
from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, sandbox_options_pb2

_HEALTH_SERVICE = '/grpc.health.v1.Health/'

# A grpc.aio unary handler's behaviour: the stubs type handlers for the sync API, so the aio shape is
# named here and cast to.
type _UnaryUnary[TRequest, TResponse] = Callable[
    [TRequest, grpc.aio.ServicerContext[TRequest, TResponse]], Awaitable[TResponse]
]


@dataclasses.dataclass(frozen=True)
class Authorizer:
    """What the interceptor resolves a call through, and the deployment fact admission turns on.

    Attributes:
        session_resolver: Resolves a claim's session token to its binding.
        verify_caller: Verifies the forwarded ID token to the calling service account's email.
        project: The GCP project this service runs in — what completes a ``Caller`` member's account
            id into the email the verifier yields.
    """

    session_resolver: session_mod.SessionResolver
    verify_caller: caller_mod.CallerVerifier
    project: str


class AuthInterceptor(grpc.aio.ServerInterceptor):
    """Gate every call on the server: build its context, derive its rule, deny what the rule does not admit."""

    def __init__(self, authorizer: Authorizer) -> None:
        self._authorizer = authorizer

    @override
    async def intercept_service[TRequest, TResponse](
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[TRequest, TResponse]]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[TRequest, TResponse]:
        handler = await continuation(handler_call_details)
        if handler is None:  # pyright: ignore[reportUnnecessaryComparison]  — no such method: the server answers UNIMPLEMENTED
            return handler
        path = handler_call_details.method
        if path.startswith(_HEALTH_SERVICE):
            return handler
        rule = rules_mod.rule_for_path(path)
        if rule is None:
            return _refused(handler, f'{path} is declared by no contract this server holds')
        return _gated(handler, rule, self._authorizer)


def gated_server(authorizer: Authorizer, *, interceptors: Sequence[grpc.aio.ServerInterceptor] = ()) -> grpc.aio.Server:
    """A ``grpc.aio`` server with the auth interceptor installed ahead of any other."""
    return grpc.aio.server(interceptors=[AuthInterceptor(authorizer), *interceptors])


async def _build_context(context: grpc.aio.ServicerContext, authorizer: Authorizer) -> context_mod.AuthContext:
    caller = await authorizer.verify_caller(context)
    if caller is None:
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, 'the caller presented no ID token this service can verify')
    try:
        claim = claim_mod.read(context)
    except claim_mod.MalformedClaimError as e:
        await context.abort(grpc.StatusCode.INTERNAL, str(e))
    if claim is None:
        if rules_mod.is_named(caller, project=authorizer.project) and not rules_mod.calling_as_self(
            caller, project=authorizer.project
        ):
            await context.abort(grpc.StatusCode.INTERNAL, 'this account never calls as itself and presented no claim')
        return context_mod.AuthContext(caller=caller, calling_as=sandbox_options_pb2.CALLING_AS_SELF, session=None)
    if claim.calling_as in claim_mod.SESSION_SCOPED and not claim.session_token:
        await context.abort(grpc.StatusCode.INTERNAL, 'the claim names a session and carries no session token')
    session = None
    if claim.session_token:
        session = await _resolve_session(claim.session_token, authorizer.session_resolver)
    return context_mod.AuthContext(caller=caller, calling_as=claim.calling_as, session=session)


async def _resolve_session(token: str, resolver: session_mod.SessionResolver) -> auth_pb2.SessionContext | None:
    try:
        return await resolver(token)
    except session_mod.UnresolvedSessionError:
        return None


def _refused[TRequest, TResponse](
    handler: grpc.RpcMethodHandler[TRequest, TResponse], reason: str
) -> grpc.RpcMethodHandler[TRequest, TResponse]:
    """``handler`` replaced by one that denies every call: the path has no contract to be admitted to."""

    async def refuse(request: TRequest, context: grpc.aio.ServicerContext[TRequest, TResponse]) -> TResponse:
        del request
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, reason)
        raise AssertionError('unreachable: abort does not return')  # pragma: no cover

    return grpc.unary_unary_rpc_method_handler(refuse, handler.request_deserializer, handler.response_serializer)


def _gated[TRequest, TResponse](
    handler: grpc.RpcMethodHandler[TRequest, TResponse], rule: rules_mod.Rule, authorizer: Authorizer
) -> grpc.RpcMethodHandler[TRequest, TResponse]:
    """``handler`` behind the gate: the context is built and the rule evaluated before its behaviour runs.

    Raises:
        NotImplementedError: ``handler`` is not unary-unary; nothing this gate fronts streams.
    """
    if handler.unary_unary is None:
        raise NotImplementedError('the auth interceptor gates unary rpcs only; a streaming handler was registered')
    inner = typing.cast('_UnaryUnary[TRequest, TResponse]', handler.unary_unary)

    async def unary_unary(request: TRequest, context: grpc.aio.ServicerContext[TRequest, TResponse]) -> TResponse:
        auth = await _build_context(context, authorizer)
        if not rule.admits(auth, project=authorizer.project):
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, 'the caller is not admitted to this rpc')
        token = context_mod.bind(auth)
        try:
            return await inner(request, context)
        finally:
            context_mod.unbind(token)

    return grpc.unary_unary_rpc_method_handler(unary_unary, handler.request_deserializer, handler.response_serializer)
