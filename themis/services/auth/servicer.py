"""The auth gRPC servicer: implements the ``Auth`` service from the proto contract.

Subclasses the generated ``themis.rpc.auth_pb2_grpc.AuthServicer`` (the forced
interface), resolving each request's session token through the injected backend. An
unresolvable token is a ``PERMISSION_DENIED`` the servicer adds — a transport-level
rejection, not a modelled body this slice.
"""

from __future__ import annotations

import grpc

from themis.rpc import auth_pb2, auth_pb2_grpc
from themis.services.auth import backend as auth_backend


class Servicer(auth_pb2_grpc.AuthServicer):
    def __init__(self, backend: auth_backend.SessionBackend) -> None:
        self._backend = backend

    async def ResolveSession(
        self, request: auth_pb2.ResolveTokenRequest, context: grpc.aio.ServicerContext
    ) -> auth_pb2.SessionContext:
        try:
            return await self._backend.resolve(request.session_token)
        except auth_backend.UnresolvedError:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, 'session token could not be resolved')
