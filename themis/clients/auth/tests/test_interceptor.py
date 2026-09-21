"""The interceptor as served: every path gated, the caller verified, its claim read, the rule derived and applied.

Driven over a real in-process ``grpc.aio`` server with generic handlers registered at real contract
paths — the literature rpcs and a store rpc, whose options the contracts declare — plus a path no
contract declares, and the health check.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import override

import grpc
import grpc.aio
import pytest
from google.protobuf import empty_pb2, message
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from themis.clients.auth import claim as claim_mod
from themis.clients.auth import context as auth_context
from themis.clients.auth import interceptor as interceptor_mod
from themis.rpc import literature_pb2, sandbox_options_pb2, store_pb2
from themis.testing import auth as fixture
from themis.testing import in_process_grpc

_LITERATURE = 'themis.rpc.literature.Literature'
_STORE = store_pb2.DESCRIPTOR.services_by_name['Store'].full_name  # imported here: the rule is read off the pool
_NOWHERE = 'themis.rpc.nowhere.Nothing'

Metadata = fixture.Metadata
_Codec = tuple[Callable[[bytes], message.Message], Callable[[message.Message], bytes]]

_SELF = sandbox_options_pb2.CALLING_AS_SELF
_AGENT_SESSION = sandbox_options_pb2.CALLING_AS_AGENT_SESSION
_WORKER_SESSION = sandbox_options_pb2.CALLING_AS_WORKER_SESSION


def _bytes(b: bytes) -> bytes:
    return b


def _serialize(m: message.Message) -> bytes:
    return m.SerializeToString()


class _Seen:
    """What the handler observed of the call it served."""

    def __init__(self) -> None:
        self.auth: auth_context.AuthContext | None = None


def _register(seen: _Seen) -> Callable[[grpc.aio.Server], None]:
    async def observe(request: message.Message, context: grpc.aio.ServicerContext) -> message.Message:
        del context
        seen.auth = auth_context.current()
        return request

    def passthrough(service: str, methods: dict[str, _Codec]) -> grpc.GenericRpcHandler:
        return grpc.method_handlers_generic_handler(
            service,
            {
                name: grpc.unary_unary_rpc_method_handler(observe, deserializer, serializer)
                for name, (deserializer, serializer) in methods.items()
            },
        )

    def register(server: grpc.aio.Server) -> None:
        server.add_generic_rpc_handlers(
            (
                passthrough(
                    _LITERATURE,
                    {
                        'GetMarkdown': (literature_pb2.GetMarkdownRequest.FromString, _serialize),
                        'DescribePaper': (literature_pb2.DescribePaperRequest.FromString, _serialize),
                        'MaybeIngestPapers': (literature_pb2.MaybeIngestPapersRequest.FromString, _serialize),
                    },
                ),
                passthrough(_STORE, {'GetWorkingDocument': (empty_pb2.Empty.FromString, _serialize)}),
                passthrough(_NOWHERE, {'Call': (empty_pb2.Empty.FromString, _serialize)}),
            )
        )
        health_servicer = health.aio.HealthServicer()  # pyright: ignore[reportAttributeAccessIssue]
        health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

    return register


def _call(
    path: str, metadata: Metadata, request: bytes = b''
) -> tuple[grpc.StatusCode, auth_context.AuthContext | None]:
    """Drive one unary call through the gated server; the status it ended with, and what the handler saw."""
    seen = _Seen()

    async def run() -> grpc.StatusCode:
        async with in_process_grpc.serving(
            _register(seen), server_interceptors=(interceptor_mod.AuthInterceptor(fixture.authorizer()),)
        ) as channel:
            method = channel.unary_unary(path, request_serializer=_bytes, response_deserializer=_bytes)
            try:
                await method(request, metadata=claim_mod.pairs(metadata))
            except grpc.aio.AioRpcError as e:
                return e.code()
            return grpc.StatusCode.OK

    return asyncio.run(run()), seen.auth


def test_the_agent_reaches_an_rpc_naming_it_with_its_context_hydrated_from_the_claim() -> None:
    code, auth = _call(f'/{_LITERATURE}/GetMarkdown', fixture.AGENT)
    assert code is grpc.StatusCode.OK
    assert auth == auth_context.AuthContext(
        caller=fixture.SANDBOX_JOB_EMAIL, calling_as=_AGENT_SESSION, session=fixture.SESSION
    )


def test_a_self_acting_caller_reaches_its_rpc_with_no_claim_at_all() -> None:
    code, auth = _call(f'/{_LITERATURE}/DescribePaper', fixture.WEB)
    assert code is grpc.StatusCode.OK
    assert auth == auth_context.AuthContext(caller=fixture.WEB_EMAIL, calling_as=_SELF, session=None)


def test_the_worker_s_claim_reaches_the_host_only_rpc_and_not_the_agent_s() -> None:
    # One account, two principals: the claim decides which member the call is.
    code, auth = _call(f'/{_STORE}/GetWorkingDocument', fixture.WORKER)
    assert code is grpc.StatusCode.OK
    assert auth == auth_context.AuthContext(
        caller=fixture.SANDBOX_JOB_EMAIL, calling_as=_WORKER_SESSION, session=fixture.SESSION
    )
    code, auth = _call(f'/{_STORE}/GetWorkingDocument', fixture.AGENT)
    assert code is grpc.StatusCode.PERMISSION_DENIED
    assert auth is None
    code, auth = _call(f'/{_LITERATURE}/GetMarkdown', fixture.WORKER)
    assert code is grpc.StatusCode.PERMISSION_DENIED
    assert auth is None


def test_a_verified_caller_the_contract_does_not_name_is_denied() -> None:
    # web is named on the paper-content reads and nowhere else; a claimed session does not widen that.
    for metadata in (fixture.WEB, (*fixture.WEB, *fixture.claim(_SELF, 'good'))):
        code, auth = _call(f'/{_LITERATURE}/GetMarkdown', metadata)
        assert code is grpc.StatusCode.PERMISSION_DENIED, metadata
        assert auth is None


def test_a_self_acting_caller_s_session_is_resolved_for_attribution_without_admitting() -> None:
    code, auth = _call(f'/{_LITERATURE}/DescribePaper', fixture.CLU_WITH_SESSION)
    assert code is grpc.StatusCode.OK
    assert auth == auth_context.AuthContext(caller=fixture.CLU_EMAIL, calling_as=_SELF, session=fixture.SESSION)


def test_a_self_acting_caller_whose_named_session_does_not_resolve_is_denied() -> None:
    # Admitted without a session, but a claim the callee cannot honour is not one to serve under: the
    # handler would read "named none" from a call that named one.
    code, auth = _call(f'/{_LITERATURE}/DescribePaper', (*fixture.CLU, *fixture.claim(_SELF, 'bad')))
    assert code is grpc.StatusCode.PERMISSION_DENIED
    assert auth is None


def test_the_agent_with_a_session_that_does_not_resolve_is_denied_not_faulted() -> None:
    # Expired or revoked is routine and the caller's to remedy: the member needs a session, and there is none.
    code, auth = _call(f'/{_LITERATURE}/GetMarkdown', fixture.AGENT_BAD_SESSION)
    assert code is grpc.StatusCode.PERMISSION_DENIED
    assert auth is None


def test_a_session_resolver_outage_propagates_rather_than_denying() -> None:
    code, _ = _call(f'/{_LITERATURE}/GetMarkdown', fixture.AGENT_OUTAGE)
    assert code is grpc.StatusCode.UNKNOWN  # the raise surfaced; it did not read as "no session"


@pytest.mark.parametrize('metadata', [(('authorization', 'Bearer nobody'),), fixture.claim(_AGENT_SESSION, 'good'), ()])
def test_an_unverifiable_id_token_is_denied_before_any_context_exists(metadata: Metadata) -> None:
    code, auth = _call(f'/{_LITERATURE}/GetMarkdown', metadata)
    assert code is grpc.StatusCode.UNAUTHENTICATED
    assert auth is None


def test_an_account_that_never_calling_as_itself_must_claim() -> None:
    # The sandbox job's account has no self-acting member, so a call from it with no claim is a worker
    # defect — the hatch and the worker's own clients always claim — surfaced as INTERNAL, not a denial.
    code, _ = _call(f'/{_LITERATURE}/GetMarkdown', fixture.SANDBOX_JOB)
    assert code is grpc.StatusCode.INTERNAL


@pytest.mark.parametrize(
    'claim',
    [
        fixture.claim(sandbox_options_pb2.CALLING_AS_UNSPECIFIED, 'good'),  # names nothing
        fixture.claim(_SELF, 'good'),  # names a member the sandbox job's account does not have
    ],
)
def test_a_claim_the_account_has_no_member_for_is_a_fault_not_a_denial(claim: Metadata) -> None:
    code, _ = _call(f'/{_LITERATURE}/GetMarkdown', (*fixture.SANDBOX_JOB, *claim))
    assert code is grpc.StatusCode.INTERNAL


def test_a_session_claim_with_no_token_is_a_fault() -> None:
    code, _ = _call(f'/{_LITERATURE}/GetMarkdown', (*fixture.SANDBOX_JOB, *fixture.claim(_AGENT_SESSION, '')))
    assert code is grpc.StatusCode.INTERNAL


def test_a_claim_that_does_not_decode_is_a_fault() -> None:
    code, _ = _call(f'/{_LITERATURE}/GetMarkdown', (*fixture.SANDBOX_JOB, (claim_mod.CLAIM_METADATA, b'\xff\xff\xff')))
    assert code is grpc.StatusCode.INTERNAL


@pytest.mark.parametrize('metadata', [fixture.AGENT, fixture.CLU])
def test_a_path_no_contract_declares_is_refused_even_to_the_developer(metadata: Metadata) -> None:
    code, auth = _call(f'/{_NOWHERE}/Call', metadata)
    assert code is grpc.StatusCode.PERMISSION_DENIED
    assert auth is None


def test_the_developer_identity_reaches_an_rpc_that_names_only_the_worker() -> None:
    code, auth = _call(f'/{_STORE}/GetWorkingDocument', fixture.CLU)
    assert code is grpc.StatusCode.OK
    assert auth == auth_context.AuthContext(caller=fixture.CLU_EMAIL, calling_as=_SELF, session=None)


def test_the_health_check_is_exempt() -> None:
    async def run() -> health_pb2.HealthCheckResponse:
        async with in_process_grpc.serving(
            _register(_Seen()), server_interceptors=(interceptor_mod.AuthInterceptor(fixture.authorizer()),)
        ) as channel:
            stub = health_pb2_grpc.HealthStub(channel)
            return await stub.Check(health_pb2.HealthCheckRequest())  # pyright: ignore[reportAttributeAccessIssue]

    response = asyncio.run(run())
    assert response.status == health_pb2.HealthCheckResponse.SERVING


def test_gated_server_gates_every_path_and_keeps_the_other_interceptors() -> None:
    seen: list[str] = []

    class Marker(grpc.aio.ServerInterceptor):
        @override
        async def intercept_service[TRequest, TResponse](
            self,
            continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[TRequest, TResponse]]],
            handler_call_details: grpc.HandlerCallDetails,
        ) -> grpc.RpcMethodHandler[TRequest, TResponse]:
            seen.append(handler_call_details.method)
            return await continuation(handler_call_details)

    async def run() -> grpc.StatusCode:
        server = interceptor_mod.gated_server(fixture.authorizer(), interceptors=(Marker(),))
        _register(_Seen())(server)
        port = server.add_insecure_port('127.0.0.1:0')
        await server.start()
        try:
            async with grpc.aio.insecure_channel(f'127.0.0.1:{port}') as channel:
                method = channel.unary_unary(
                    f'/{_LITERATURE}/GetMarkdown', request_serializer=_bytes, response_deserializer=_bytes
                )
                try:
                    await method(b'')
                except grpc.aio.AioRpcError as e:
                    return e.code()
                return grpc.StatusCode.OK
        finally:
            await server.stop(None)

    # No credential: the auth interceptor denies; the marker still saw the path, so both are installed.
    assert asyncio.run(run()) is grpc.StatusCode.UNAUTHENTICATED
    assert seen == [f'/{_LITERATURE}/GetMarkdown']
