"""Tests for ``AnthropicWorkQueue`` and the offline fixture queue."""

from __future__ import annotations

import asyncio
import types
from unittest import mock

import anthropic
import httpx2

from themis.clients.work_queue import client as work_queue_mod
from themis.clients.work_queue.tests import fixture_work_queue


def test_anthropic_work_queue_poll_maps_session_work_item_and_passes_reclaim_window() -> None:
    seen_urls: list[httpx2.URL] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_urls.append(request.url)
        return httpx2.Response(
            200,
            json={
                'id': 'work-1',
                'created_at': '2026-01-01T00:00:00Z',
                'data': {'id': 'sess-1', 'type': 'session'},
                'environment_id': 'env-1',
                'metadata': {},
                'state': 'queued',
                'type': 'work',
            },
        )

    async def run() -> work_queue_mod.WorkItem | None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
            sdk = anthropic.AsyncAnthropic(api_key='test-key', http_client=http_client)
            queue = work_queue_mod.AnthropicWorkQueue(sdk, environment_id='env-1')
            return await queue.poll(reclaim_older_than_ms=5000)

    item = asyncio.run(run())

    assert item is not None
    assert item == work_queue_mod.WorkItem(work_id='work-1', session_id='sess-1', item_type='session')
    assert item.is_session
    (url,) = seen_urls
    assert url.path == '/v1/environments/env-1/work/poll'
    assert url.params['reclaim_older_than_ms'] == '5000'
    assert 'block_ms' not in url.params


def test_anthropic_work_queue_poll_returns_none_when_queue_is_empty() -> None:
    transport = httpx2.MockTransport(
        lambda _req: httpx2.Response(200, content=b'null', headers={'content-type': 'application/json'})
    )

    async def run() -> work_queue_mod.WorkItem | None:
        async with httpx2.AsyncClient(transport=transport) as http_client:
            sdk = anthropic.AsyncAnthropic(api_key='test-key', http_client=http_client)
            queue = work_queue_mod.AnthropicWorkQueue(sdk, environment_id='env-1')
            return await queue.poll(reclaim_older_than_ms=5000)

    assert asyncio.run(run()) is None


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
