"""Tests for the work-queue poll-response parsing and the offline fixture queue."""

from __future__ import annotations

import asyncio

import pytest

from themis.clients.work_queue import client as work_queue_mod
from themis.clients.work_queue.tests import fixture_work_queue


def test_parse_item_reads_work_and_session_ids() -> None:
    item = work_queue_mod._parse_item({'id': 'work-1', 'data': {'id': 'sess-1', 'type': 'session'}})
    assert item == work_queue_mod.WorkItem(work_id='work-1', session_id='sess-1', item_type='session')
    assert item is not None
    assert item.is_session


def test_parse_item_empty_queue_is_none() -> None:
    assert work_queue_mod._parse_item({}) is None


def test_parse_item_missing_data_raises() -> None:
    with pytest.raises(ValueError, match='data'):
        work_queue_mod._parse_item({'id': 'work-1'})  # a work id but no data object


def test_parse_item_missing_session_fields_raises() -> None:
    with pytest.raises(ValueError, match='missing string'):
        work_queue_mod._parse_item({'id': 'work-1', 'data': {'id': 'sess-1'}})  # no data.type


def test_fixture_queue_yields_items_then_none() -> None:
    queue = fixture_work_queue.FixtureWorkQueue(
        [work_queue_mod.WorkItem(work_id='work-a', session_id='sess-a', item_type='session')]
    )

    async def run() -> tuple[work_queue_mod.WorkItem | None, work_queue_mod.WorkItem | None]:
        first = await queue.poll(reclaim_older_than_ms=1)
        second = await queue.poll(reclaim_older_than_ms=1)
        return first, second

    first, second = asyncio.run(run())
    assert first == work_queue_mod.WorkItem(work_id='work-a', session_id='sess-a', item_type='session')
    assert second is None
    assert queue.polls == 2
