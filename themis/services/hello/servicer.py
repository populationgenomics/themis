"""The hello servicer: echo the caller's note against its resolved session binding."""

from __future__ import annotations

import grpc.aio

from themis.clients.auth import session as session_mod
from themis.rpc import hello_pb2, hello_pb2_grpc


class Servicer(hello_pb2_grpc.HelloServicer):
    """Resolves each request's session token and echoes the note against the bound Analysis.

    The resolved binding is the whole authorization: ``require_session`` rejects a missing or
    unresolvable token before any reply, so a caller that reaches this service without the proxy's
    injected token never gets a greeting. Holds no state beyond the session resolver.
    """

    def __init__(self, session_resolver: session_mod.SessionResolver) -> None:
        self._session_resolver = session_resolver

    async def SayHello(
        self, request: hello_pb2.SayHelloRequest, context: grpc.aio.ServicerContext
    ) -> hello_pb2.SayHelloResponse:
        session = await session_mod.require_session(context, self._session_resolver)
        return hello_pb2.SayHelloResponse(
            greeting=f'hello from analysis {session.analysis_id}: {request.note}',
            project_id=session.project_id,
            analysis_id=session.analysis_id,
        )
