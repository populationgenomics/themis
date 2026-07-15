"""In-memory work-queue test double for the dispatcher's orchestration tests.

Kept out of ``client`` so the production module ships no test-only code; the work-queue and
dispatcher tests import it here.
"""

from __future__ import annotations

from themis.clients.work_queue import client


class FixtureWorkQueue(client.WorkQueue):
    """In-memory work queue for the dispatcher's orchestration tests.

    Seeded with the items successive polls return (one per ``poll``, then ``None``); records the ids
    acked and stopped so a test can assert the dispatcher neither acks on claim nor double-services.
    """

    def __init__(self, items: list[client.WorkItem]) -> None:
        self._items = list(items)
        self.polls = 0
        self.acked: list[str] = []
        self.stopped: list[str] = []

    async def poll(self, *, reclaim_older_than_ms: int) -> client.WorkItem | None:  # noqa: ARG002 — port signature
        self.polls += 1
        return self._items.pop(0) if self._items else None

    async def ack(self, work_id: str) -> None:
        self.acked.append(work_id)

    async def stop(self, work_id: str) -> None:
        self.stopped.append(work_id)
