"""Tests for GrpcStore's fail-closed contract on the working-document read.

A positive NOT_FOUND is the one store error that maps to ``None`` (first spawn / no document yet); every other
gRPC error re-raises, so the caller (``WorkspaceSync.restore``) fails the spawn rather than booting onto a blank doc.
"""

from __future__ import annotations

import asyncio
from typing import cast

import grpc
import grpc.aio
import pytest

from themis.rpc import store_pb2, store_pb2_grpc
from themis.services.sandbox_worker import store_client


def _rpc_error(code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, grpc.aio.Metadata(), grpc.aio.Metadata())


class _Stub:
    """Stands in for the StoreStub: raises the given error, else returns a snapshot."""

    def __init__(self, *, error: grpc.aio.AioRpcError | None = None, markdown: str = '') -> None:
        self._error = error
        self._markdown = markdown

    async def GetWorkingDocument(  # noqa: N802 — mirrors the generated stub method name
        self, request: object, *, metadata: object = None
    ) -> store_pb2.WorkingDocumentSnapshot:
        del request, metadata
        if self._error is not None:
            raise self._error
        return store_pb2.WorkingDocumentSnapshot(version=1, markdown=self._markdown)


def _grpc_store(stub: _Stub) -> store_client.GrpcStore:
    # Bypass __init__ so no real channel is dialled; inject the fake stub in place of the real one.
    store = store_client.GrpcStore.__new__(store_client.GrpcStore)
    store._stub = cast('store_pb2_grpc.StoreStub', stub)
    store._metadata = ((store_client._SESSION_TOKEN_METADATA, 'TOK'),)
    return store


def test_get_working_document_returns_none_on_not_found() -> None:
    store = _grpc_store(_Stub(error=_rpc_error(grpc.StatusCode.NOT_FOUND)))
    assert asyncio.run(store.get_working_document()) is None


def test_get_working_document_reraises_a_non_not_found_error() -> None:
    store = _grpc_store(_Stub(error=_rpc_error(grpc.StatusCode.UNAVAILABLE)))
    with pytest.raises(grpc.aio.AioRpcError):
        asyncio.run(store.get_working_document())


def test_get_working_document_returns_the_markdown_when_present() -> None:
    store = _grpc_store(_Stub(markdown='hello doc'))
    assert asyncio.run(store.get_working_document()) == 'hello doc'
