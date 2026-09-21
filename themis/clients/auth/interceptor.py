"""The server interceptor that gates every data-plane call (rpc-authorization.md).

For each inbound call it verifies the calling service account from the ID token Cloud Run forwarded,
reads the caller's claim about what it is calling as, resolves the session that claim names, and builds
the call's :class:`~themis.clients.auth.context.AuthContext` from the three. It then derives the rule
the rpc's contract declares — the principals the rpc admits — and denies when none is the call. It is
server-wide by construction: a gRPC interceptor sees every method path before any handler runs, so an
rpc whose contract admits nobody is denied without anyone having remembered to gate it, and a path no
imported contract declares is denied before any rule is consulted — it is not an rpc, so not even the
developer identity, admitted on every rpc there is, reaches it. The health check is the one exemption,
probed by Cloud Run without credentials.

What is verified and what is trusted: the account is verified, by Google's signature on the token; the
claim is trusted, because the account is trusted to make it, and is checked only for being one the
account has a member for. Several outcomes are not denials and are surfaced as what they are: a caller
whose ID token does not verify is ``UNAUTHENTICATED``, since Cloud Run admits no call without a valid
token and one this service cannot re-verify is a deployment fault or a broken guarantee; a claim the
callee cannot honour — one that does not decode, one naming nothing, one naming a session and carrying
no token, or a self claim (or no claim) from an account that has no self-acting member — is
``INTERNAL``, a caller-side fault. A session token that does not resolve is a denial whatever the claim:
the call would be attributed wrongly or not at all, so a handler that finds no session on its context
knows the claim named none. A session-resolver failure that is not "the token does not resolve"
propagates, for any call presenting a token.

The gate is also the one place a call's status is sent. A handler behind it raises :class:`StatusError`
— a gRPC status carried as an exception — and the gate aborts the context with it, so a servicer never
touches the context's status itself; a stream-in caller still writing when that happens receives the
status (rpc-authorization.md, "Default-deny is enforced at the interceptor").

``gated_server`` is the one way a data-plane image builds its server: a server built any other way has
no interceptor, and default-deny would depend on remembering to add one.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import os
import typing
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
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

# A grpc.aio handler's behaviour, per shape: the stubs type handlers for the sync API, so the aio
# shapes are named here and cast to.
type _UnaryUnary[TRequest, TResponse] = Callable[
    [TRequest, grpc.aio.ServicerContext[TRequest, TResponse]], Awaitable[TResponse]
]
type _UnaryStream[TRequest, TResponse] = Callable[
    [TRequest, grpc.aio.ServicerContext[TRequest, TResponse]], AsyncIterator[TResponse]
]
type _StreamUnary[TRequest, TResponse] = Callable[
    [AsyncIterator[TRequest], grpc.aio.ServicerContext[TRequest, TResponse]], Awaitable[TResponse]
]


class StatusError(Exception):
    """A gRPC status carried as an exception, for the gate to end the call with.

    Raised by the gate for what it decides — a denial, an unverifiable caller, a claim it cannot honour
    — and by a handler behind it for whatever status the handler's contract names; the gate is the one
    place the status is sent.
    """

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__(details)
        self.code = code
        self.details = details


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
            return _denied(handler, f'{path} is declared by no contract this server holds')
        return _gated(handler, rule, self._authorizer)


def gated_server(authorizer: Authorizer, *, interceptors: Sequence[grpc.aio.ServerInterceptor] = ()) -> grpc.aio.Server:
    """A ``grpc.aio`` server with the auth interceptor installed ahead of any other."""
    return grpc.aio.server(interceptors=[AuthInterceptor(authorizer), *interceptors])


AUTHORIZER_VAR = 'THEMIS_AUTHORIZER_BACKEND'
PROJECT_VAR = 'THEMIS_GCP_PROJECT'


def authorizer_from_env(*, fixture_contexts_var: str, fixture_callers_var: str) -> Authorizer:
    """The ``Authorizer`` a data-plane image builds from its environment, or ``SystemExit``.

    ``THEMIS_AUTHORIZER_BACKEND`` selects it: ``http`` resolves sessions through the auth service at
    ``THEMIS_AUTH_URL`` and verifies callers from the ID token Cloud Run forwards; ``fixture`` resolves
    against the JSON map in ``fixture_contexts_var`` (bearer -> binding) and treats the bearers in
    ``fixture_callers_var`` (bearer -> email) as verified. ``THEMIS_GCP_PROJECT`` completes each
    ``Caller``'s account id into the email the verifier yields. The two fixture variables are the image's
    own, so two images on one host can be seeded apart.

    Raises:
        SystemExit: A selector or seed is unset, unknown or malformed. Every input is read before the
            ``Authorizer`` is built, so a misconfigured deploy names the first missing variable; nothing
            here opens a connection.
    """
    backend = os.environ.get(AUTHORIZER_VAR)
    if backend is None:
        raise SystemExit(f'{AUTHORIZER_VAR} is required (expected "http" or "fixture")')
    if backend not in ('http', 'fixture'):
        raise SystemExit(f'unsupported {AUTHORIZER_VAR} {backend!r} (expected "http" or "fixture")')
    if backend == 'http':
        verify_caller = caller_mod.cloud_run_caller_verifier()
    else:
        verify_caller = caller_mod.fixture_caller_verifier_from_json(
            os.environ.get(fixture_callers_var), var_name=fixture_callers_var
        )
    project = os.environ.get(PROJECT_VAR)
    if not project:
        raise SystemExit(
            f'{PROJECT_VAR} is required: the GCP project this service runs in, completing each Caller into an email'
        )
    if backend == 'http':
        session_resolver = session_mod.session_resolver_from_env()
    else:
        session_resolver = session_mod.fixture_session_resolver_from_json(
            os.environ.get(fixture_contexts_var), var_name=fixture_contexts_var
        )
    return Authorizer(session_resolver=session_resolver, verify_caller=verify_caller, project=project)


async def _build_context(context: grpc.aio.ServicerContext, authorizer: Authorizer) -> context_mod.AuthContext:
    caller = await authorizer.verify_caller(context)
    if caller is None:
        raise StatusError(grpc.StatusCode.UNAUTHENTICATED, 'the caller presented no ID token this service can verify')
    try:
        claim = claim_mod.read(context)
    except claim_mod.MalformedClaimError as e:
        raise StatusError(grpc.StatusCode.INTERNAL, str(e)) from e
    calling_as = sandbox_options_pb2.CALLING_AS_SELF if claim is None else claim.calling_as
    if calling_as == sandbox_options_pb2.CALLING_AS_UNSPECIFIED:
        raise StatusError(grpc.StatusCode.INTERNAL, 'the claim names nothing the caller is calling as')
    if calling_as == sandbox_options_pb2.CALLING_AS_SELF and (
        rules_mod.is_named(caller, project=authorizer.project)
        and not rules_mod.calling_as_self(caller, project=authorizer.project)
    ):
        raise StatusError(grpc.StatusCode.INTERNAL, 'this account never calls as itself')
    if claim is None:
        return context_mod.AuthContext(caller=caller, calling_as=calling_as, session=None)
    if calling_as in claim_mod.SESSION_SCOPED and not claim.session_token:
        raise StatusError(grpc.StatusCode.INTERNAL, 'the claim names a session and carries no session token')
    session = None
    if claim.session_token:
        session = await _resolve_session(claim.session_token, authorizer.session_resolver)
    return context_mod.AuthContext(caller=caller, calling_as=calling_as, session=session)


async def _resolve_session(token: str, resolver: session_mod.SessionResolver) -> auth_pb2.SessionContext:
    try:
        return await resolver(token)
    except session_mod.UnresolvedSessionError as e:
        raise StatusError(grpc.StatusCode.PERMISSION_DENIED, 'the session the claim names does not resolve') from e


async def _admit(
    context: grpc.aio.ServicerContext, rule: rules_mod.Rule, authorizer: Authorizer
) -> context_mod.AuthContext:
    auth = await _build_context(context, authorizer)
    if not rule.admits(auth, project=authorizer.project):
        raise StatusError(grpc.StatusCode.PERMISSION_DENIED, 'the caller is not admitted to this rpc')
    return auth


@contextlib.asynccontextmanager
async def _admitted(
    context: grpc.aio.ServicerContext, rule: rules_mod.Rule, authorizer: Authorizer
) -> AsyncGenerator[None]:
    """Admit the call and bind its context for the body; unbound on exit, in the task that bound it."""
    token = context_mod.bind(await _admit(context, rule, authorizer))
    try:
        yield
    finally:
        context_mod.unbind(token)


async def _abort(context: grpc.aio.ServicerContext, status: StatusError) -> typing.NoReturn:
    await context.abort(status.code, status.details)


def _denied[TRequest, TResponse](
    handler: grpc.RpcMethodHandler[TRequest, TResponse], reason: str
) -> grpc.RpcMethodHandler[TRequest, TResponse]:
    """``handler`` replaced by one of its shape that denies every call: the path has no contract to be admitted to."""
    denial = StatusError(grpc.StatusCode.PERMISSION_DENIED, reason)
    deserializer, serializer = handler.request_deserializer, handler.response_serializer

    async def deny(request: object, context: grpc.aio.ServicerContext[TRequest, TResponse]) -> TResponse:
        del request
        await _abort(context, denial)

    if handler.response_streaming:
        # A coroutine, not a generator: grpc.aio runs either for a stream-out rpc, and this one never yields.
        return grpc.unary_stream_rpc_method_handler(deny, deserializer, serializer)
    if handler.request_streaming:
        return grpc.stream_unary_rpc_method_handler(deny, deserializer, serializer)
    return grpc.unary_unary_rpc_method_handler(deny, deserializer, serializer)


def _gated[TRequest, TResponse](
    handler: grpc.RpcMethodHandler[TRequest, TResponse], rule: rules_mod.Rule, authorizer: Authorizer
) -> grpc.RpcMethodHandler[TRequest, TResponse]:
    """``handler`` behind the gate: the context is built and the rule evaluated before its behaviour runs.

    Raises:
        NotImplementedError: ``handler`` is stream-stream, which no contract declares; or it streams out through
            ``context.write()`` rather than as an async generator, the one stream-out shape gated here.
    """
    deserializer, serializer = handler.request_deserializer, handler.response_serializer

    if handler.unary_unary is not None:
        inner = typing.cast('_UnaryUnary[TRequest, TResponse]', handler.unary_unary)

        async def unary_unary(request: TRequest, context: grpc.aio.ServicerContext[TRequest, TResponse]) -> TResponse:
            try:
                async with _admitted(context, rule, authorizer):
                    return await inner(request, context)
            except StatusError as status:
                await _abort(context, status)

        return grpc.unary_unary_rpc_method_handler(unary_unary, deserializer, serializer)

    if handler.unary_stream is not None:
        if not inspect.isasyncgenfunction(handler.unary_stream):
            raise NotImplementedError(
                'the auth interceptor gates a stream-out handler written as an async generator; one that writes '
                'through context.write() was registered'
            )
        streaming = typing.cast('_UnaryStream[TRequest, TResponse]', handler.unary_stream)

        async def unary_stream(
            request: TRequest, context: grpc.aio.ServicerContext[TRequest, TResponse]
        ) -> AsyncIterator[TResponse]:
            # Bound for the rpc's task and never reset: the binding has to hold while the response is
            # drawn, and a client that cancels mid-stream leaves this generator to be finalised in
            # another task, whose context the token does not belong to. The task's context dies with it.
            try:
                context_mod.bind(await _admit(context, rule, authorizer))
                async for response in streaming(request, context):
                    yield response
            except StatusError as status:
                await _abort(context, status)

        return grpc.unary_stream_rpc_method_handler(unary_stream, deserializer, serializer)

    if handler.stream_unary is not None:
        receiving = typing.cast('_StreamUnary[TRequest, TResponse]', handler.stream_unary)

        async def stream_unary(
            requests: AsyncIterator[TRequest], context: grpc.aio.ServicerContext[TRequest, TResponse]
        ) -> TResponse:
            try:
                async with _admitted(context, rule, authorizer):
                    return await receiving(requests, context)
            except StatusError as status:
                await _abort(context, status)

        return grpc.stream_unary_rpc_method_handler(stream_unary, deserializer, serializer)

    raise NotImplementedError('the auth interceptor gates no stream-stream rpc; no contract declares one')
