"""Tests for the work-item mapping, the ack/stop SDK calls, and the offline fixture queue."""

from __future__ import annotations

import asyncio
import types
from unittest import mock

from anthropic.types.beta.environments import beta_self_hosted_work, beta_session_work_data

from themis.clients.work_queue import client as work_queue_mod
from themis.clients.work_queue.tests import fixture_work_queue


def test_to_work_item_maps_session_work() -> None:
    work = beta_self_hosted_work.BetaSelfHostedWork(
        id='work-1',
        created_at='2026-01-01T00:00:00Z',
        data=beta_session_work_data.BetaSessionWorkData(id='sess-1', type='session'),
        environment_id='env-1',
        metadata={},
        state='queued',
        type='work',
    )
    item = work_queue_mod._to_work_item(work)
    assert item == work_queue_mod.WorkItem(work_id='work-1', session_id='sess-1', item_type='session')
    assert item.is_session


def test_anthropic_work_queue_acks_and_stops_by_work_id_with_environment_and_timeout() -> None:
    # The adapter must pass the work id positionally and bind environment_id + the per-call timeout; the
    # worker's reclaim fix depends on ack targeting the *work* id (not the session id).
    work = mock.AsyncMock()
    client = types.SimpleNamespace(beta=types.SimpleNamespace(environments=types.SimpleNamespace(work=work)))
    queue = work_queue_mod.AnthropicWorkQueue(client, environment_id='env-1')  # type: ignore[arg-type]

    asyncio.run(queue.ack('work-1'))
    asyncio.run(queue.stop('work-2'))

    work.ack.assert_awaited_once_with('work-1', environment_id='env-1', timeout=work_queue_mod._ACK_TIMEOUT_S)
    work.stop.assert_awaited_once_with('work-2', environment_id='env-1', timeout=work_queue_mod._ACK_TIMEOUT_S)


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
