"""Spawn one sandbox Job execution per work item via the Cloud Run Admin API (self-hosted-sandbox.md §5, §7).

``gcloud run jobs execute`` cannot set per-container env, so the dispatcher POSTs the REST ``:run``
endpoint with ``containerOverrides`` (spike-validated). The Job is a single trusted worker container:
the claimed session's ids, the environment key, and the per-session token are all injected into it — it
holds the credentials and runs untrusted code only inside the postern sandbox, so there is no
co-resident untrusted container to keep them from (postern-sandbox-swap.md §4). The container is
targeted by ``name``, which must match the Job manifest's container name.

``JobRunner`` is the port; ``CloudRunJobRunner`` the adapter (dispatcher-SA access token). The
spawn-recording double the orchestration tests drive lives in the test scaffolding.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses

import aiohttp
import google.auth.credentials
import google.auth.transport.requests

_WORKER_CONTAINER = 'worker'


@dataclasses.dataclass(frozen=True)
class SpawnRequest:
    """The per-execution values the dispatcher injects into one sandbox spawn."""

    session_id: str
    work_id: str
    environment_id: str
    # environment_key and session_token are secrets — keep them out of repr (logs, tracebacks).
    environment_key: str = dataclasses.field(repr=False)
    session_token: str = dataclasses.field(repr=False)


class JobRunner(abc.ABC):
    """Spawn one sandbox Job execution with the per-execution env injected."""

    @abc.abstractmethod
    async def spawn(self, request: SpawnRequest) -> None: ...


class CloudRunJobRunner(JobRunner):
    """Runs the sandbox Job via the Cloud Run Admin API ``:run`` endpoint (dispatcher-SA bearer)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        project: str,
        region: str,
        job: str,
        credentials: google.auth.credentials.Credentials,
    ) -> None:
        self._session = session
        self._url = f'https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{job}:run'
        self._credentials = credentials

    async def spawn(self, request: SpawnRequest) -> None:
        token = await asyncio.to_thread(self._access_token)
        async with self._session.post(
            self._url, headers={'Authorization': f'Bearer {token}'}, json=_overrides_body(request)
        ) as response:
            response.raise_for_status()

    def _access_token(self) -> str:
        if not self._credentials.valid:
            self._credentials.refresh(google.auth.transport.requests.Request())
        token = self._credentials.token
        if not token:
            raise RuntimeError('failed to mint a Cloud Run Admin API access token')
        return token


@dataclasses.dataclass(frozen=True)
class _ContainerOverride:
    name: str
    env: dict[str, str]


def _container_overrides(request: SpawnRequest) -> list[_ContainerOverride]:
    """The single worker container's per-execution env: session ids, the environment key, session token."""
    env = {
        'ANTHROPIC_SESSION_ID': request.session_id,
        'ANTHROPIC_WORK_ID': request.work_id,
        'ANTHROPIC_ENVIRONMENT_ID': request.environment_id,
        'ANTHROPIC_ENVIRONMENT_KEY': request.environment_key,
        'THEMIS_SESSION_TOKEN': request.session_token,
    }
    return [_ContainerOverride(_WORKER_CONTAINER, env)]


def _overrides_body(request: SpawnRequest) -> dict[str, object]:
    """Serialize the container overrides into the Cloud Run ``:run`` request body."""
    return {
        'overrides': {
            'containerOverrides': [
                {'name': c.name, 'env': [{'name': k, 'value': v} for k, v in c.env.items()]}
                for c in _container_overrides(request)
            ],
            'taskCount': 1,
        }
    }
