"""The store gRPC servicer: implements the ``Store`` service from the proto contract.

Subclasses the generated ``themis.rpc.store_pb2_grpc.StoreServicer`` (the forced interface).
Every method authorizes the request first — ``require_session`` resolves the
``x-themis-session-token`` metadata to its Analysis through the injected session resolver — and derives
every blob key from that Analysis, so a request can only ever touch its own session's state. The
workspace RPCs stream ``WorkspaceChunk``s; the servicer marshals them to and from the whole
``bytes`` the storage port speaks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import grpc
from google.protobuf import empty_pb2

from themis.clients.auth import session as session_mod
from themis.rpc import store_pb2, store_pb2_grpc
from themis.services.store import storage as storage_mod

# Under gRPC's 4 MiB default per-message limit, with margin, so a large restore streams.
_CHUNK_SIZE = 1 << 20

# The scratch archive is buffered whole in memory before upload (§9); cap it so a runaway workspace
# can't exhaust the store. Aligned with the proxy's decompressed-workspace cap (proxy/workspace.py) —
# the archive is an uncompressed tar, so its size tracks the workspace it holds.
_MAX_WORKSPACE_ARCHIVE_BYTES = 512 * 1024 * 1024


class Servicer(store_pb2_grpc.StoreServicer):
    def __init__(self, storage: storage_mod.Storage, session_resolver: session_mod.SessionResolver) -> None:
        self._storage = storage
        self._session_resolver = session_resolver

    async def PutWorkingDocument(
        self, request: store_pb2.PutWorkingDocumentRequest, context: grpc.aio.ServicerContext
    ) -> store_pb2.PutWorkingDocumentResponse:
        session = await session_mod.require_session(context, self._session_resolver)
        version = await self._storage.put_working_document(session.analysis_id, request.markdown)
        return store_pb2.PutWorkingDocumentResponse(version=version)

    async def GetWorkingDocument(
        self, request: empty_pb2.Empty, context: grpc.aio.ServicerContext
    ) -> store_pb2.WorkingDocumentSnapshot:
        session = await session_mod.require_session(context, self._session_resolver)
        snapshot = await self._storage.get_working_document(session.analysis_id)
        if snapshot is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, 'no working document for this analysis')
        return store_pb2.WorkingDocumentSnapshot(version=snapshot.version, markdown=snapshot.markdown)

    async def PutWorkspace(
        self, request_iterator: AsyncIterator[store_pb2.WorkspaceChunk], context: grpc.aio.ServicerContext
    ) -> store_pb2.PutWorkspaceResponse:
        session = await session_mod.require_session(context, self._session_resolver)
        chunks: list[bytes] = []
        total = 0
        async for chunk in request_iterator:
            total += len(chunk.content)
            if total > _MAX_WORKSPACE_ARCHIVE_BYTES:
                await context.abort(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    f'workspace archive exceeds the {_MAX_WORKSPACE_ARCHIVE_BYTES}-byte cap',
                )
            chunks.append(chunk.content)
        await self._storage.put_workspace(session.analysis_id, b''.join(chunks))
        return store_pb2.PutWorkspaceResponse()

    async def GetWorkspace(
        self, request: empty_pb2.Empty, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[store_pb2.WorkspaceChunk]:
        session = await session_mod.require_session(context, self._session_resolver)
        archive = await self._storage.get_workspace(session.analysis_id)
        if archive is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, 'no workspace for this analysis')
        for start in range(0, len(archive), _CHUNK_SIZE):
            yield store_pb2.WorkspaceChunk(content=archive[start : start + _CHUNK_SIZE])
