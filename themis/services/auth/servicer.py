"""The auth gRPC servicer: implements the ``Auth`` service from the proto contract.

Subclasses the generated ``themis.rpc.auth_pb2_grpc.AuthServicer`` (the forced interface). Both rpcs
answer from the holder's current copy of the ACL. ``GetAccess`` sits behind the auth interceptor
and raises its statuses for the gate to send; ``ResolveSession`` is the rpc the gate itself resolves
sessions through, so the interceptor passes it through ungated and it aborts the call itself.
"""

from __future__ import annotations

from typing import override

import grpc
import grpc.aio

from themis.clients.auth import interceptor as interceptor_mod
from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, auth_pb2_grpc
from themis.services.auth import acl_holder
from themis.services.auth import backend as auth_backend


class ProjectGoneError(auth_backend.UnresolvedError):
    """The token resolves, but its Project is no longer in the ACL."""


class Servicer(auth_pb2_grpc.AuthServicer):
    def __init__(self, backend: auth_backend.AuthBackend, acl: acl_holder.AclHolder, *, operator: str) -> None:
        """Answer from ``backend``'s token store and ``acl``'s current copy of the ACL.

        Args:
            backend: The token store ``ResolveSession`` looks tokens up in.
            acl: The holder of the ACL both rpcs answer from.
            operator: The email of the deployment's operator account, which ``GetAccess`` admits to every
                Project in the ACL.
        """
        self._backend = backend
        self._acl = acl
        self._operator = operator

    async def session_context(self, session_token: str) -> auth_pb2.SessionContext:
        """The Analysis a token is bound to, and the grant for its Project as the ACL states it now.

        Raises:
            acl_holder.StaleAclError: The ACL's last good copy is past the staleness limit.
            backend.UnresolvedError: The token grants nothing, or (as ``ProjectGoneError``) its Project is
                no longer in the ACL.
        """
        snapshot = self._acl.current()
        stored = await self._backend.resolve(session_token)
        grant = snapshot.grant(stored.project_id)
        if grant is None:
            raise ProjectGoneError
        return auth_pb2.SessionContext(analysis_id=stored.analysis_id, project=grant)

    @override
    async def ResolveSession(
        self, request: auth_pb2.ResolveTokenRequest, context: grpc.aio.ServicerContext
    ) -> auth_pb2.SessionContext:
        try:
            return await self.session_context(request.session_token)
        except acl_holder.StaleAclError as e:
            await context.abort(grpc.StatusCode.UNAVAILABLE, str(e))
        except ProjectGoneError:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "the session's Project is not in the ACL")
        except auth_backend.UnresolvedError:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, 'session token could not be resolved')

    @override
    async def GetAccess(
        self, request: auth_pb2.GetAccessRequest, context: grpc.aio.ServicerContext
    ) -> auth_pb2.GetAccessResponse:
        del context
        if not request.email:
            raise interceptor_mod.StatusError(grpc.StatusCode.INVALID_ARGUMENT, 'GetAccess names no email')
        try:
            snapshot = self._acl.current()
        except acl_holder.StaleAclError as e:
            raise interceptor_mod.StatusError(grpc.StatusCode.UNAVAILABLE, str(e)) from e
        return snapshot.access(request.email, operator=self._operator)


def in_process_session_resolver(servicer: Servicer) -> session_mod.SessionResolver:
    """The session resolver the auth service's own gate uses: ``servicer``'s resolution, with no network hop.

    A token that does not resolve is ``UnresolvedSessionError``, as from the auth service over the wire;
    a stale ACL is the ``UNAVAILABLE`` status the gate ends the call with.
    """

    async def session_resolver(session_token: str) -> auth_pb2.SessionContext:
        try:
            return await servicer.session_context(session_token)
        except acl_holder.StaleAclError as e:
            raise interceptor_mod.StatusError(grpc.StatusCode.UNAVAILABLE, str(e)) from e
        except auth_backend.UnresolvedError as e:
            raise session_mod.UnresolvedSessionError from e

    return session_resolver
