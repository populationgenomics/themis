"""Client for the Anthropic Managed-Agents work queue (self-hosted-sandbox.md §2, §5).

On a ``session.status_run_started`` webhook the dispatcher drains the queue: the webhook is only the
trigger and carries no work, so items are claimed by polling. It polls (draining, without acking) and
force-stops non-session items; the sandbox proxy acks its own item once restore is proven.

The queue is driven through the SDK's low-level ``work.poll`` / ``work.ack`` / ``work.stop``, not the
high-level ``work.poller``: the poller acks each item on yield, which would break the restore-gated
deferred ack — the dispatcher polls without acking, and the proxy acks only after restore proves.

``WorkQueue`` is the port; ``AnthropicWorkQueue`` is the SDK adapter. The in-memory double the
dispatcher's orchestration tests drive lives in the test scaffolding.
"""

from __future__ import annotations

import abc
import dataclasses

import anthropic
from anthropic.types.beta.environments import beta_self_hosted_work

_SESSION_ITEM_TYPE = 'session'
# Ack/stop are quick request/response calls, but the proxy's shared client carries a long stream-read
# timeout tuned for its minutes-long SSE stream. Bound them per-call so a stalled call fails loud
# instead of hanging the proxy's startup (the ack gates the agent) forever.
_ACK_TIMEOUT_S = 30.0


@dataclasses.dataclass(frozen=True)
class WorkItem:
    """A claimed work item: ``work_id`` (ack/stop key), ``session_id`` it runs, ``item_type`` discriminant."""

    work_id: str
    session_id: str
    item_type: str

    @property
    def is_session(self) -> bool:
        return self.item_type == _SESSION_ITEM_TYPE


class WorkQueue(abc.ABC):
    """The work-queue operations the dispatcher and proxy need — poll (no ack), ack, force-stop."""

    @abc.abstractmethod
    async def poll(self, *, reclaim_older_than_ms: int) -> WorkItem | None: ...

    @abc.abstractmethod
    async def ack(self, work_id: str) -> None: ...

    @abc.abstractmethod
    async def stop(self, work_id: str) -> None: ...


class AnthropicWorkQueue(WorkQueue):
    """The work queue over the Anthropic SDK's low-level ``work`` methods (the client carries auth)."""

    def __init__(self, client: anthropic.AsyncAnthropic, *, environment_id: str) -> None:
        self._client = client
        self._environment_id = environment_id

    async def poll(self, *, reclaim_older_than_ms: int) -> WorkItem | None:
        # Low-level poll, never work.poller: the poller acks on yield, but our ack is deferred until the
        # proxy proves restore. Omitting block_ms is the non-blocking default — the run_started webhook
        # already enqueued the item, so there is nothing to long-poll for.
        work = await self._client.beta.environments.work.poll(
            self._environment_id, reclaim_older_than_ms=reclaim_older_than_ms
        )
        if work is None:
            return None
        return _to_work_item(work)

    async def ack(self, work_id: str) -> None:
        await self._client.beta.environments.work.ack(
            work_id, environment_id=self._environment_id, timeout=_ACK_TIMEOUT_S
        )

    async def stop(self, work_id: str) -> None:
        await self._client.beta.environments.work.stop(
            work_id, environment_id=self._environment_id, timeout=_ACK_TIMEOUT_S
        )


def _to_work_item(work: beta_self_hosted_work.BetaSelfHostedWork) -> WorkItem:
    # The work item wraps the session: top-level `id` is the work id, `data.id` the session id.
    return WorkItem(work_id=work.id, session_id=work.data.id, item_type=work.data.type)
