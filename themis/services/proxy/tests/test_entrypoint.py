"""Tests for the proxy entrypoint's restore/ack sequencing (fail-fast on a store restore error)."""

from __future__ import annotations

import asyncio
import pathlib

import grpc
import grpc.aio

from themis.clients.work_queue.tests import fixture_work_queue
from themis.services.proxy import __main__ as entrypoint
from themis.services.proxy import store_client, sync


def _sync(store: store_client.Store, root: pathlib.Path) -> sync.WorkspaceSync:
    return sync.WorkspaceSync(store, root=root, document_path=root / 'document.md')


def test_restore_success_acks_and_proceeds(tmp_path: pathlib.Path) -> None:
    queue = fixture_work_queue.FixtureWorkQueue([])
    proceed = asyncio.run(
        entrypoint._restore_or_fail_item(_sync(store_client.FixtureStore(document='doc'), tmp_path), queue, 'w1')
    )
    assert proceed is True
    assert queue.acked == ['w1']
    assert queue.stopped == []


def test_restore_error_acks_and_stops_the_item(tmp_path: pathlib.Path) -> None:
    class _Failing(store_client.FixtureStore):
        async def get_working_document(self) -> str | None:
            raise grpc.aio.AioRpcError(
                grpc.StatusCode.INTERNAL, grpc.aio.Metadata(), grpc.aio.Metadata(), details='store down'
            )

    queue = fixture_work_queue.FixtureWorkQueue([])
    proceed = asyncio.run(entrypoint._restore_or_fail_item(_sync(_Failing(), tmp_path), queue, 'w1'))
    assert proceed is False
    assert queue.acked == ['w1']  # ack stops reclaim
    assert queue.stopped == ['w1']  # stop terminates the item
