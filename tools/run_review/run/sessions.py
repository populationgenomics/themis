"""The Anthropic reads the run half makes: the session, its raw event log, its threads, and the agent's records.

Everything comes back as the API's JSON objects, as the SDK dumps them, so what the harness decides on
is exactly what collection writes to disk. The client is the operator's, for the dev deployment's
workspace: `themis-clu` has no Anthropic identity.
"""

from __future__ import annotations

import datetime
from typing import Protocol, override

import anthropic

from tools.run_review import rounds

# The errors a read or an interrupt may raise that are worth trying again at the next poll: the
# connection, the rate limit and the server side. The SDK has already retried each a couple of times.
TRANSIENT_ANTHROPIC_ERRORS: tuple[type[Exception], ...] = (
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
)


class Sessions(Protocol):
    def session(self, session_id: str) -> rounds.Json: ...
    def events(self, session_id: str, *, since: datetime.datetime | None) -> list[rounds.Json]:
        """The session's events processed at or after `since`, or every event where it is None."""
        ...

    def threads(self, session_id: str) -> list[rounds.Json]: ...
    def thread_events(self, session_id: str, thread_id: str) -> list[rounds.Json]:
        """One thread's events, in the order the API lists them, which it does not state."""
        ...

    def agent_metadata(self, agent_id: str, version: int) -> dict[str, str]: ...


class AnthropicSessions(Sessions):
    """`Sessions` over the Managed Agents API. Every list is read whole, page by page."""

    def __init__(self, client: anthropic.Anthropic) -> None:
        self._client = client

    @override
    def session(self, session_id: str) -> rounds.Json:
        return self._client.beta.sessions.retrieve(session_id).model_dump(mode='json')

    @override
    def events(self, session_id: str, *, since: datetime.datetime | None) -> list[rounds.Json]:
        # The time filters compare against `processed_at`, which is also what `order` sorts by.
        listed = self._client.beta.sessions.events.list(
            session_id, order='asc', created_at_gte=anthropic.omit if since is None else since
        )
        return [event.model_dump(mode='json') for event in listed]

    @override
    def threads(self, session_id: str) -> list[rounds.Json]:
        return [thread.model_dump(mode='json') for thread in self._client.beta.sessions.threads.list(session_id)]

    @override
    def thread_events(self, session_id: str, thread_id: str) -> list[rounds.Json]:
        return [
            event.model_dump(mode='json')
            for event in self._client.beta.sessions.threads.events.list(thread_id, session_id=session_id)
        ]

    @override
    def agent_metadata(self, agent_id: str, version: int) -> dict[str, str]:
        return dict(self._client.beta.agents.retrieve(agent_id, version=version).metadata)
