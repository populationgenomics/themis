"""Drain the work queue on a run_started webhook and spawn a sandbox per session item (self-hosted-sandbox.md §5).

The orchestration, over the work-queue / deriver / job-runner ports so it is testable offline. It
polls **without acking** (the ack is deferred into the sandbox, once restore is proven), force-stops
a non-session item (whose lease would otherwise sit until TTL under no-auto-stop), and for each
session item derives the per-session token and triggers one Job execution. A derive or spawn failure
leaves the item unacked — reclaim re-surfaces it on a later drain (§5) — and does not stop the drain.
"""

from __future__ import annotations

import logging

from themis.clients.auth import derive as derive_mod
from themis.clients.work_queue import client as work_queue_mod
from themis.services.dispatcher import job_runner as job_runner_mod

_logger = logging.getLogger(__name__)


# One webhook drains at most this many items inline; a larger backlog reclaims on the next delivery, so
# a recovery surge cannot stall the response past Anthropic's webhook timeout (self-hosted-sandbox.md §5).
_MAX_ITEMS_PER_DELIVERY = 20


async def dispatch_run_started(
    *,
    work_queue: work_queue_mod.WorkQueue,
    deriver: derive_mod.SessionTokenDeriver,
    job_runner: job_runner_mod.JobRunner,
    environment_id: str,
    environment_key: str,
    reclaim_older_than_ms: int,
) -> None:
    """Drain the queue: force-stop non-session items, derive + spawn each session item, never ack."""
    for _ in range(_MAX_ITEMS_PER_DELIVERY):
        item = await work_queue.poll(reclaim_older_than_ms=reclaim_older_than_ms)
        match item:
            case None:
                return
            case work_queue_mod.WorkItem(is_session=False):
                # A non-session item is unexpected (the endpoint subscribes to run_started only): surface it,
                # and don't let a transient stop failure abort the drain of the remaining session items.
                _logger.warning(
                    'unexpected non-session work item %s (type=%s); force-stopping', item.work_id, item.item_type
                )
                try:
                    await work_queue.stop(item.work_id)
                except Exception:
                    _logger.exception('failed to force-stop work item %s; continuing drain', item.work_id)
            case _:
                await _spawn_for(item, deriver, job_runner, environment_id, environment_key)
    _logger.warning(
        'reached the per-delivery cap of %d items; any remaining work reclaims on the next webhook',
        _MAX_ITEMS_PER_DELIVERY,
    )


async def _spawn_for(
    item: work_queue_mod.WorkItem,
    deriver: derive_mod.SessionTokenDeriver,
    job_runner: job_runner_mod.JobRunner,
    environment_id: str,
    environment_key: str,
) -> None:
    try:
        session_token = await deriver(item.session_id)
        await job_runner.spawn(
            job_runner_mod.SpawnRequest(
                session_id=item.session_id,
                work_id=item.work_id,
                environment_id=environment_id,
                environment_key=environment_key,
                session_token=session_token,
            )
        )
    except Exception:
        # Resilience boundary: any failure leaves the item unacked, so reclaim re-surfaces it (§5).
        _logger.exception('spawn failed for work item %s; left unacked for reclaim', item.work_id)
