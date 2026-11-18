"""Tests for ``AnthropicWorkQueue`` and the offline fixture queue."""

from __future__ import annotations

import asyncio

import anthropic
import httpx2
import pytest

from themis.clients.work_queue import client as work_queue_mod
from themis.clients.work_queue.tests import fixture_work_queue


def _work_json(work_id: str, *, state: str = 'queued') -> dict[str, object]:
    return {
        'id': work_id,
        'created_at': '2026-01-01T00:00:00Z',
        'data': {'id': 'sess-1', 'type': 'session'},
        'environment_id': 'env-1',
        'metadata': {},
        'state': state,
        'type': 'work',
    }


def test_anthropic_work_queue_poll_maps_session_work_item_and_passes_reclaim_window() -> None:
    seen_urls: list[httpx2.URL] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_urls.append(request.url)
        return httpx2.Response(200, json=_work_json('work-1'))

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
    # Ack and stop target the work id (not the session id) and override the client's minutes-long SSE
    # default timeout with a short request/response bound.
    seen: list[tuple[str, str, dict[str, float | None]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        timeout_ext = request.extensions.get('timeout')
        assert isinstance(timeout_ext, dict)
        seen.append((request.method, request.url.path, timeout_ext))
        work_id = request.url.path.split('/')[-2]
        return httpx2.Response(200, json=_work_json(work_id, state='starting'))

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
            sdk = anthropic.AsyncAnthropic(api_key='test-key', timeout=600.0, http_client=http_client)
            queue = work_queue_mod.AnthropicWorkQueue(sdk, environment_id='env-1')
            await queue.ack('work-1')
            await queue.stop('work-2')

    asyncio.run(run())

    assert [(method, path) for method, path, _ in seen] == [
        ('POST', '/v1/environments/env-1/work/work-1/ack'),
        ('POST', '/v1/environments/env-1/work/work-2/stop'),
    ]
    for _, _, timeout_ext in seen:
        read_timeout = timeout_ext.get('read')
        assert isinstance(read_timeout, int | float)
        assert 0 < read_timeout < 60.0


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


def test_fixture_queue_records_ack_and_stop_and_refuses_ack_after_stop() -> None:
    queue = fixture_work_queue.FixtureWorkQueue([])

    async def run() -> None:
        await queue.ack('work-1')
        await queue.stop('work-1')
        with pytest.raises(RuntimeError, match='already-stopped'):
            await queue.ack('work-1')

    asyncio.run(run())
    assert queue.acked == ['work-1']
    assert queue.stopped == ['work-1']
