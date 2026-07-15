"""The webhook delivery decision, over the ports so it is testable without an HTTP server.

``process_delivery`` verifies the HMAC, ignores anything but ``run_started``, and drains the queue —
returning the HTTP status the entrypoint sends. Bundling the ports and per-environment config in one
``Dispatcher`` keeps the entrypoint's env wiring apart from the delivery logic tested here.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Mapping

from themis.clients.auth import derive as derive_mod
from themis.clients.work_queue import client as work_queue_mod
from themis.services.dispatcher import dispatch as dispatch_mod
from themis.services.dispatcher import job_runner as job_runner_mod
from themis.services.dispatcher import webhook as webhook_mod

_RUN_STARTED_EVENT = 'session.status_run_started'

_UNAUTHORIZED = 401
_BAD_REQUEST = 400
_NO_CONTENT = 204

_logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Dispatcher:
    """The dispatcher's ports plus the per-environment config a delivery needs."""

    work_queue: work_queue_mod.WorkQueue
    deriver: derive_mod.SessionTokenDeriver
    job_runner: job_runner_mod.JobRunner
    signing_key: str
    environment_id: str
    environment_key: str
    reclaim_older_than_ms: int


async def process_delivery(dispatcher: Dispatcher, headers: Mapping[str, str], body: bytes) -> int:
    """Authenticate the delivery and, if it is ``run_started``, drain the queue.

    Returns:
        The HTTP status: 401 on a bad signature, 400 on unparseable JSON, 204 once handled (an event
        that is not ``run_started`` is acknowledged and ignored — the endpoint subscribes only to it).
    """
    try:
        event = webhook_mod.verify(headers, body, dispatcher.signing_key)
    except webhook_mod.SignatureError:
        return _UNAUTHORIZED
    except (json.JSONDecodeError, UnicodeDecodeError):
        # A signed-but-non-JSON body, or one that is not even UTF-8, is malformed — 400, never a spawn.
        return _BAD_REQUEST
    # The event type is on `data.type`; the top-level `type` is always "event".
    data = event.get('data') if isinstance(event, dict) else None
    if not isinstance(data, dict) or data.get('type') != _RUN_STARTED_EVENT:
        observed = data.get('type') if isinstance(data, dict) else None
        _logger.warning('ignoring delivery that is not %s (data.type=%r)', _RUN_STARTED_EVENT, observed)
        return _NO_CONTENT
    await dispatch_mod.dispatch_run_started(
        work_queue=dispatcher.work_queue,
        deriver=dispatcher.deriver,
        job_runner=dispatcher.job_runner,
        environment_id=dispatcher.environment_id,
        environment_key=dispatcher.environment_key,
        reclaim_older_than_ms=dispatcher.reclaim_older_than_ms,
    )
    return _NO_CONTENT
