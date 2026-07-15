"""The store client: the proxy syncs ``/workspace`` to the store service (self-hosted-sandbox.md §9).

The proxy is the store's client, injecting the session token as ``x-themis-session-token`` metadata on
its own put/get calls; the channel carries the job SA's ID token. ``Store`` is the port so the sync
orchestration tests offline against a fixture. A ``NOT_FOUND`` (the genuine first spawn) maps to
``None``; every other gRPC failure propagates so restore can fail closed.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator

import grpc
import grpc.aio
from google.protobuf import empty_pb2

from themis.rpc import store_pb2, store_pb2_grpc

_SESSION_TOKEN_METADATA = 'x-themis-session-token'  # noqa: S105 — a metadata key name, not a secret
_CHUNK_SIZE = 1 << 20  # 1 MiB per workspace-archive chunk


class Store(abc.ABC):
    """The store operations the proxy's workspace sync needs."""

    @abc.abstractmethod
    async def get_working_document(self) -> str | None: ...

    @abc.abstractmethod
    async def put_working_document(self, markdown: str) -> int: ...

    @abc.abstractmethod
    async def get_workspace(self) -> bytes | None: ...

    @abc.abstractmethod
    async def put_workspace(self, archive: bytes) -> None: ...


class GrpcStore(Store):
    """The store over its gRPC contract (session-token metadata; the channel carries the SA ID token)."""

    def __init__(self, channel: grpc.aio.Channel, *, session_token: str) -> None:
        self._stub = store_pb2_grpc.StoreStub(channel)
        self._metadata = ((_SESSION_TOKEN_METADATA, session_token),)

    async def get_working_document(self) -> str | None:
        try:
            snapshot = await self._stub.GetWorkingDocument(empty_pb2.Empty(), metadata=self._metadata)
        except grpc.aio.AioRpcError as e:
            if e.code() is grpc.StatusCode.NOT_FOUND:
                return None
            raise
        return snapshot.markdown

    async def put_working_document(self, markdown: str) -> int:
        response = await self._stub.PutWorkingDocument(
            store_pb2.PutWorkingDocumentRequest(markdown=markdown), metadata=self._metadata
        )
        return response.version

    async def get_workspace(self) -> bytes | None:
        call = self._stub.GetWorkspace(empty_pb2.Empty(), metadata=self._metadata)
        chunks: list[bytes] = []
        try:
            async for chunk in call:
                chunks.append(chunk.content)
        except grpc.aio.AioRpcError as e:
            if e.code() is grpc.StatusCode.NOT_FOUND:
                return None
            raise
        return b''.join(chunks)

    async def put_workspace(self, archive: bytes) -> None:
        await self._stub.PutWorkspace(_chunks(archive), metadata=self._metadata)


class FixtureStore(Store):
    """In-memory store for the sync orchestration tests; records puts, returns seeded gets."""

    def __init__(self, *, document: str | None = None, workspace: bytes | None = None) -> None:
        self._document = document
        self._workspace = workspace
        self.put_documents: list[str] = []
        self.put_workspaces: list[bytes] = []

    async def get_working_document(self) -> str | None:
        return self._document

    async def put_working_document(self, markdown: str) -> int:
        self.put_documents.append(markdown)
        return len(self.put_documents)

    async def get_workspace(self) -> bytes | None:
        return self._workspace

    async def put_workspace(self, archive: bytes) -> None:
        self.put_workspaces.append(archive)


async def _chunks(archive: bytes) -> AsyncIterator[store_pb2.WorkspaceChunk]:
    for start in range(0, len(archive), _CHUNK_SIZE):
        yield store_pb2.WorkspaceChunk(content=archive[start : start + _CHUNK_SIZE])
