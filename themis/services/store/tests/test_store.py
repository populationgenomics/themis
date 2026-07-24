"""Behaviour tests for the store servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterable

import grpc
import grpc.aio
import pytest
from google.protobuf import empty_pb2

from themis.clients.auth.tests import fixture_session
from themis.rpc import store_pb2, store_pb2_grpc
from themis.services.store import servicer as servicer_mod
from themis.services.store import storage as storage_mod
from themis.testing import in_process_grpc


@contextlib.asynccontextmanager
async def _serving(storage: storage_mod.Storage) -> AsyncIterator[store_pb2_grpc.StoreStub]:
    servicer = servicer_mod.Servicer(storage, fixture_session.resolve_fixture_session)
    async with in_process_grpc.serving(
        lambda server: store_pb2_grpc.add_StoreServicer_to_server(servicer, server)
    ) as channel:
        yield store_pb2_grpc.StoreStub(channel)


def test_put_then_get_working_document() -> None:
    async def run() -> store_pb2.WorkingDocumentSnapshot:
        async with _serving(storage_mod.FixtureStorage()) as stub:
            first = await stub.PutWorkingDocument(
                store_pb2.PutWorkingDocumentRequest(markdown='v1'), metadata=fixture_session.GOOD_METADATA
            )
            assert first.version == 1
            await stub.PutWorkingDocument(
                store_pb2.PutWorkingDocumentRequest(markdown='v2'), metadata=fixture_session.GOOD_METADATA
            )
            return await stub.GetWorkingDocument(empty_pb2.Empty(), metadata=fixture_session.GOOD_METADATA)

    snapshot = asyncio.run(run())
    assert snapshot.version == 2
    assert snapshot.markdown == 'v2'


def test_get_working_document_absent_is_not_found() -> None:
    async def run() -> store_pb2.WorkingDocumentSnapshot:
        async with _serving(storage_mod.FixtureStorage()) as stub:
            return await stub.GetWorkingDocument(empty_pb2.Empty(), metadata=fixture_session.GOOD_METADATA)

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code() is grpc.StatusCode.NOT_FOUND


def test_put_then_get_workspace_round_trips_the_archive() -> None:
    async def run() -> bytes:
        async with _serving(storage_mod.FixtureStorage()) as stub:
            await stub.PutWorkspace(_chunks([b'hello ', b'world']), metadata=fixture_session.GOOD_METADATA)
            call = stub.GetWorkspace(empty_pb2.Empty(), metadata=fixture_session.GOOD_METADATA)
            return b''.join([chunk.content async for chunk in call])

    assert asyncio.run(run()) == b'hello world'


def test_get_workspace_reassembles_multiple_output_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(servicer_mod, '_CHUNK_SIZE', 4)

    async def run() -> list[bytes]:
        async with _serving(storage_mod.FixtureStorage()) as stub:
            await stub.PutWorkspace(_chunks([b'abcdefghij']), metadata=fixture_session.GOOD_METADATA)
            call = stub.GetWorkspace(empty_pb2.Empty(), metadata=fixture_session.GOOD_METADATA)
            return [chunk.content async for chunk in call]

    chunks = asyncio.run(run())
    assert chunks == [b'abcd', b'efgh', b'ij']


def test_put_workspace_over_cap_is_resource_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(servicer_mod, '_MAX_WORKSPACE_ARCHIVE_BYTES', 4)

    async def run() -> None:
        async with _serving(storage_mod.FixtureStorage()) as stub:
            await stub.PutWorkspace(_chunks([b'abc', b'def']), metadata=fixture_session.GOOD_METADATA)

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code() is grpc.StatusCode.RESOURCE_EXHAUSTED


def test_get_workspace_absent_is_not_found() -> None:
    async def run() -> None:
        async with _serving(storage_mod.FixtureStorage()) as stub:
            call = stub.GetWorkspace(empty_pb2.Empty(), metadata=fixture_session.GOOD_METADATA)
            async for _ in call:
                pass

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code() is grpc.StatusCode.NOT_FOUND


def test_missing_session_token_is_unauthenticated() -> None:
    async def run() -> store_pb2.PutWorkingDocumentResponse:
        async with _serving(storage_mod.FixtureStorage()) as stub:
            return await stub.PutWorkingDocument(store_pb2.PutWorkingDocumentRequest(markdown='v1'))

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code() is grpc.StatusCode.UNAUTHENTICATED


def test_unresolvable_token_is_permission_denied() -> None:
    async def run() -> store_pb2.PutWorkingDocumentResponse:
        async with _serving(storage_mod.FixtureStorage()) as stub:
            return await stub.PutWorkingDocument(
                store_pb2.PutWorkingDocumentRequest(markdown='v1'),
                metadata=fixture_session.session_metadata('bad'),
            )

    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code() is grpc.StatusCode.PERMISSION_DENIED


async def _chunks(payload: Iterable[bytes]) -> AsyncIterator[store_pb2.WorkspaceChunk]:
    for content in payload:
        yield store_pb2.WorkspaceChunk(content=content)
