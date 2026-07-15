"""Tests for the webhook delivery decision (verify -> event-type -> drain)."""

from __future__ import annotations

import asyncio
import base64
import datetime
import json

import standardwebhooks

from themis.clients.auth.tests import fixture_deriver
from themis.clients.work_queue import client as work_queue_mod
from themis.clients.work_queue.tests import fixture_work_queue
from themis.services.dispatcher import handler as handler_mod
from themis.services.dispatcher.tests import fixture_job_runner

_SIGNING_KEY = f'whsec_{base64.b64encode(b"a-32-byte-webhook-signing-secret!").decode()}'


def _dispatcher(work_queue: work_queue_mod.WorkQueue) -> handler_mod.Dispatcher:
    return handler_mod.Dispatcher(
        work_queue=work_queue,
        deriver=fixture_deriver.fixture_deriver(b'k'),
        job_runner=fixture_job_runner.FixtureJobRunner(),
        signing_key=_SIGNING_KEY,
        environment_id='env-1',
        environment_key='env-key',
        reclaim_older_than_ms=180_000,
    )


def _signed(body: bytes) -> dict[str, str]:
    timestamp = datetime.datetime.now(datetime.UTC)
    return {
        'webhook-id': 'msg_1',
        'webhook-timestamp': str(int(timestamp.timestamp())),
        'webhook-signature': standardwebhooks.Webhook(_SIGNING_KEY).sign('msg_1', timestamp, body.decode()),
    }


def _event(data_type: str) -> bytes:
    return json.dumps({'type': 'event', 'id': 'event_1', 'data': {'type': data_type, 'id': 'sesn_1'}}).encode()


def _spawns(dispatcher: handler_mod.Dispatcher) -> list[str]:
    assert isinstance(dispatcher.job_runner, fixture_job_runner.FixtureJobRunner)
    return [s.session_id for s in dispatcher.job_runner.spawns]


def test_forged_signature_is_unauthorized() -> None:
    dispatcher = _dispatcher(fixture_work_queue.FixtureWorkQueue([]))
    status = asyncio.run(handler_mod.process_delivery(dispatcher, {}, b'{}'))
    assert status == 401


def test_run_started_drains_the_queue() -> None:
    work_queue = fixture_work_queue.FixtureWorkQueue(
        [work_queue_mod.WorkItem(work_id='work-1', session_id='sess', item_type='session')]
    )
    dispatcher = _dispatcher(work_queue)
    body = _event('session.status_run_started')
    status = asyncio.run(handler_mod.process_delivery(dispatcher, _signed(body), body))
    assert status == 204
    assert _spawns(dispatcher) == ['sess']


def test_other_event_is_acknowledged_without_draining() -> None:
    work_queue = fixture_work_queue.FixtureWorkQueue(
        [work_queue_mod.WorkItem(work_id='work-1', session_id='sess', item_type='session')]
    )
    dispatcher = _dispatcher(work_queue)
    body = _event('session.status_idled')
    status = asyncio.run(handler_mod.process_delivery(dispatcher, _signed(body), body))
    assert status == 204
    assert work_queue.polls == 0  # a non-run_started event never drains
    assert _spawns(dispatcher) == []


def test_valid_signature_but_unparseable_body_is_bad_request() -> None:
    dispatcher = _dispatcher(fixture_work_queue.FixtureWorkQueue([]))
    status = asyncio.run(handler_mod.process_delivery(dispatcher, _signed(b'not json'), b'not json'))
    assert status == 400


def test_non_utf8_body_is_bad_request() -> None:
    # standardwebhooks decodes the body to verify the signature, so a non-UTF-8 body raises
    # UnicodeDecodeError inside verify; the handler maps that to 400 rather than letting it 500.
    dispatcher = _dispatcher(fixture_work_queue.FixtureWorkQueue([]))
    timestamp = str(int(datetime.datetime.now(datetime.UTC).timestamp()))
    headers = {'webhook-id': 'msg_1', 'webhook-timestamp': timestamp, 'webhook-signature': 'v1,invalid'}
    status = asyncio.run(handler_mod.process_delivery(dispatcher, headers, b'\xff\xfe\xff'))
    assert status == 400
