"""The offline authorization fixture every gated-servicer test drives through (rpc-authorization.md).

One project, the ``Caller`` enum's accounts in it, a session resolver that resolves one token, a caller
verifier that verifies one bearer per account, and the claims each principal presents — held once so a
test of the interceptor and a test of a servicer behind it agree on who the callers are.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import override

import grpc.aio

from themis.clients.auth import caller as caller_mod
from themis.clients.auth import claim as claim_mod
from themis.clients.auth import interceptor as interceptor_mod
from themis.clients.auth import rules
from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, sandbox_options_pb2

Metadata = claim_mod.Metadata

PROJECT = 'x'
WEB_EMAIL = rules.account_email('themis-web', PROJECT)
CLU_EMAIL = rules.account_email('themis-clu', PROJECT)
SANDBOX_JOB_EMAIL = rules.account_email('themis-sandbox-job', PROJECT)

SESSION = auth_pb2.SessionContext(project_id='proj', analysis_id='ana')

_GOOD = 'good'
_BAD = 'bad'
_OUTAGE = 'outage'  # a token whose resolution fails for a reason other than "does not resolve"

WEB: Metadata = (('authorization', 'Bearer web'),)
CLU: Metadata = (('authorization', 'Bearer clu'),)
SANDBOX_JOB: Metadata = (('authorization', 'Bearer sandbox-job'),)


def claim(calling_as: sandbox_options_pb2.CallingAs, session_token: str) -> Metadata:
    """The claim metadata alone — ``calling_as`` with ``session_token`` — for a hand-built call."""
    encoded = auth_pb2.CallerClaim(calling_as=calling_as, session_token=session_token).SerializeToString()
    return ((claim_mod.CLAIM_METADATA, encoded),)


# The agent as the hatch presents it: the sandbox job's account, claiming the agent's session.
AGENT: Metadata = (*SANDBOX_JOB, *claim(sandbox_options_pb2.CALLING_AS_AGENT_SESSION, _GOOD))
AGENT_BAD_SESSION: Metadata = (*SANDBOX_JOB, *claim(sandbox_options_pb2.CALLING_AS_AGENT_SESSION, _BAD))
AGENT_OUTAGE: Metadata = (*SANDBOX_JOB, *claim(sandbox_options_pb2.CALLING_AS_AGENT_SESSION, _OUTAGE))
# The worker calling as itself within the session — its own checkpoints and mirror traffic.
WORKER: Metadata = (*SANDBOX_JOB, *claim(sandbox_options_pb2.CALLING_AS_WORKER_SESSION, _GOOD))
# A caller calling as itself that presents a session anyway: resolved for attribution, admitting nothing.
CLU_WITH_SESSION: Metadata = (*CLU, *claim(sandbox_options_pb2.CALLING_AS_SELF, _GOOD))

_CALLERS = {'web': WEB_EMAIL, 'clu': CLU_EMAIL, 'sandbox-job': SANDBOX_JOB_EMAIL}


async def session_resolver(session_token: str) -> auth_pb2.SessionContext:
    """Resolve the good token; fail outright on the outage token; ``UnresolvedSessionError`` for anything else."""
    if session_token == _GOOD:
        return SESSION
    if session_token == _OUTAGE:
        raise RuntimeError('auth service unreachable')
    raise session_mod.UnresolvedSessionError


def authorizer() -> interceptor_mod.Authorizer:
    """An ``Authorizer`` over the fixture resolvers and project ``x``."""
    return interceptor_mod.Authorizer(
        session_resolver=session_resolver,
        verify_caller=caller_mod.fixture_caller_verifier_from_json(json.dumps(_CALLERS), var_name='test'),
        project=PROJECT,
    )


def server_interceptors() -> tuple[grpc.aio.ServerInterceptor, ...]:
    """The auth interceptor over :func:`authorizer`, for an in-process server."""
    return (interceptor_mod.AuthInterceptor(authorizer()),)


class InjectMetadata(grpc.aio.UnaryUnaryClientInterceptor):
    """Prepend fixed metadata to every unary call — the credential a gated call would otherwise carry."""

    def __init__(self, metadata: Metadata) -> None:
        self._metadata = metadata

    @override
    async def intercept_unary_unary(
        self,
        continuation: Callable[[grpc.aio.ClientCallDetails, object], Awaitable[object]],
        client_call_details: grpc.aio.ClientCallDetails,
        request: object,
    ) -> object:
        # The stub types Metadata iteration as keys; at runtime it yields (key, value) pairs.
        existing = tuple(client_call_details.metadata or ())
        merged = grpc.aio.Metadata(*self._metadata, *existing)  # pyright: ignore[reportArgumentType]
        amended = client_call_details._replace(metadata=merged)  # type: ignore[attr-defined]
        return await continuation(amended, request)
