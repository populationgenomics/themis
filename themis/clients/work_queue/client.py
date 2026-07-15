"""Client for the Anthropic Managed-Agents work queue (self-hosted-sandbox.md §2, §5).

On a ``session.status_run_started`` webhook the dispatcher drains the queue: the webhook is only the
trigger and carries no work, so items are claimed by polling. It polls (draining, without acking) and
force-stops non-session items; the sandbox proxy acks its own item once restore is proven. Every call
authenticates with the environment key as a Bearer on ``ANTHROPIC_BASE_URL``.

``WorkQueue`` is the port; ``AnthropicWorkQueue`` is the HTTP adapter. The in-memory double the
dispatcher's orchestration tests drive lives in the test scaffolding.
"""

from __future__ import annotations

import abc
import dataclasses

import aiohttp

_ANTHROPIC_VERSION = '2023-06-01'  # anthropic-version header value
_ANTHROPIC_BETA = 'managed-agents-2026-04-01'  # anthropic-beta opt-in for the work-queue API
_SESSION_ITEM_TYPE = 'session'


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
    """The work queue over the Anthropic API (environment-key Bearer auth)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        environment_id: str,
        environment_key: str,
    ) -> None:
        self._session = session
        self._work_base = f'{base_url.rstrip("/")}/v1/environments/{environment_id}/work'
        self._headers = {
            'Authorization': f'Bearer {environment_key}',
            'anthropic-version': _ANTHROPIC_VERSION,
            'anthropic-beta': _ANTHROPIC_BETA,
        }

    async def poll(self, *, reclaim_older_than_ms: int) -> WorkItem | None:
        params = {
            'beta': 'true',
            'reclaim_older_than_ms': str(reclaim_older_than_ms),
            'block_ms': '0',  # non-blocking: a run_started item is already enqueued; a race reclaims
        }
        async with self._session.get(f'{self._work_base}/poll', headers=self._headers, params=params) as response:
            response.raise_for_status()
            if response.status == 204:
                return None
            body = await response.json()
        return _parse_item(body)

    async def ack(self, work_id: str) -> None:
        await self._post(f'{self._work_base}/{work_id}/ack')

    async def stop(self, work_id: str) -> None:
        await self._post(f'{self._work_base}/{work_id}/stop')

    async def _post(self, url: str) -> None:
        async with self._session.post(url, headers=self._headers, params={'beta': 'true'}) as response:
            response.raise_for_status()


def _parse_item(body: object) -> WorkItem | None:
    if not isinstance(body, dict):
        raise ValueError(f'unexpected work-poll response: {type(body).__name__}')
    # The work item wraps the session: top-level `id` is the work id, `data.id` the session id.
    work_id = body.get('id')
    if not work_id:
        return None  # empty queue — nothing claimed
    data = body.get('data')
    if not isinstance(data, dict):
        raise ValueError(f'work item missing data object: {body!r}')
    session_id = data.get('id')
    item_type = data.get('type')
    if not (isinstance(work_id, str) and isinstance(session_id, str) and isinstance(item_type, str)):
        raise ValueError(f'work item missing string id/data.id/data.type: {body!r}')
    return WorkItem(work_id=work_id, session_id=session_id, item_type=item_type)
