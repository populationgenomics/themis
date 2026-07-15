"""Tests for the run_started drain orchestration (no-ack, force-stop non-session, spawn per session)."""

from __future__ import annotations

import asyncio

from themis.clients.auth.tests import fixture_deriver
from themis.clients.work_queue import client as work_queue_mod
from themis.clients.work_queue.tests import fixture_work_queue
from themis.services.dispatcher import dispatch as dispatch_mod
from themis.services.dispatcher import job_runner as job_runner_mod
from themis.services.dispatcher.tests import fixture_job_runner


def _drain(work_queue: work_queue_mod.WorkQueue, job_runner: job_runner_mod.JobRunner) -> None:
    asyncio.run(
        dispatch_mod.dispatch_run_started(
            work_queue=work_queue,
            deriver=fixture_deriver.fixture_deriver(b'k'),
            job_runner=job_runner,
            environment_id='env-1',
            environment_key='env-key',
            reclaim_older_than_ms=180_000,
        )
    )


def test_drains_and_spawns_each_session_item_without_acking() -> None:
    work_queue = fixture_work_queue.FixtureWorkQueue(
        [
            work_queue_mod.WorkItem(work_id='work-a', session_id='sess-a', item_type='session'),
            work_queue_mod.WorkItem(work_id='work-b', session_id='sess-b', item_type='session'),
        ]
    )
    job_runner = fixture_job_runner.FixtureJobRunner()
    _drain(work_queue, job_runner)

    assert [s.session_id for s in job_runner.spawns] == ['sess-a', 'sess-b']
    assert work_queue.acked == []  # the ack is deferred into the sandbox (§5)
    assert work_queue.stopped == []


def test_forwards_ids_and_the_derived_token_to_the_spawn() -> None:
    work_queue = fixture_work_queue.FixtureWorkQueue(
        [work_queue_mod.WorkItem(work_id='work-1', session_id='sess-1', item_type='session')]
    )
    job_runner = fixture_job_runner.FixtureJobRunner()
    _drain(work_queue, job_runner)

    spawn = job_runner.spawns[0]
    assert spawn.session_id == 'sess-1'
    assert spawn.work_id == 'work-1'
    assert spawn.environment_id == 'env-1'
    assert spawn.environment_key == 'env-key'

    async def token() -> str:
        return await fixture_deriver.fixture_deriver(b'k')('sess-1')  # derived from the session id

    assert spawn.session_token == asyncio.run(token())


def test_force_stops_a_non_session_item_and_does_not_spawn() -> None:
    work_queue = fixture_work_queue.FixtureWorkQueue(
        [work_queue_mod.WorkItem(work_id='work-x', session_id='sess-x', item_type='batch')]
    )
    job_runner = fixture_job_runner.FixtureJobRunner()
    _drain(work_queue, job_runner)

    assert work_queue.stopped == ['work-x']  # stop keys on the work id
    assert job_runner.spawns == []


def test_spawn_failure_leaves_item_unacked_and_keeps_draining() -> None:
    class _FailBoom(fixture_job_runner.FixtureJobRunner):
        async def spawn(self, request: job_runner_mod.SpawnRequest) -> None:
            if request.session_id == 'boom':
                raise RuntimeError('spawn failed')
            await super().spawn(request)

    work_queue = fixture_work_queue.FixtureWorkQueue(
        [
            work_queue_mod.WorkItem(work_id='work-boom', session_id='boom', item_type='session'),
            work_queue_mod.WorkItem(work_id='work-ok', session_id='ok', item_type='session'),
        ]
    )
    job_runner = _FailBoom()
    _drain(work_queue, job_runner)

    assert [s.session_id for s in job_runner.spawns] == ['ok']  # the good item still spawns
    assert work_queue.acked == []  # neither acked
    assert work_queue.stopped == []  # the failed one is left for reclaim, not stopped
