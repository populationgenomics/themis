"""The workspace's sessions as the spend signal: each session's cumulative usage, by agent.

The sessions list is the one place a dollar figure is readable with a workspace credential
(`docs/design/cost-monitoring.md`, Background): every session carries its cumulative `usage` — list
cost, tokens by component, active seconds, web searches — so one paginated listing, archived sessions
included, which the API leaves out unless asked, is a complete snapshot of the workspace. A session
the API returns without a field the totals need is a precondition failure, never a zero: a silent
zero would read as spend shrinking.
"""

from __future__ import annotations

import abc
import dataclasses
import re
from collections.abc import AsyncIterator, Mapping
from typing import override

import anthropic
from anthropic.types import beta as anthropic_beta

from themis.telemetry import names

# Sessions per page; the SDK follows the cursor to the next page as the listing is iterated.
_PAGE_SIZE = 100

# `list_cost.amount` is the cent total as a decimal string; anything else the total cannot use.
_CENTS = re.compile(r'[0-9]+')

_CURRENCY = 'USD'


class SessionRecordError(ValueError):
    """A session the API returned lacks a field the totals need, or carries one in an unexpected form."""


@dataclasses.dataclass(frozen=True)
class SessionUsage:
    """One session's cumulative usage.

    Attributes:
        agent: The name of the agent the session ran — the attribution dimension.
        cents: The cumulative list cost in USD cents, as the API reports it (rounded to the cent).
        tokens: Cumulative tokens by component; `CACHE_CREATION` is both cache lifetimes together.
        active_seconds: The seconds the session had a thread running — what its runtime is priced on.
        web_search_requests: Server-side web searches the session ran.
    """

    agent: str
    cents: int
    tokens: Mapping[names.TokenType, int]
    active_seconds: float
    web_search_requests: int


@dataclasses.dataclass(frozen=True)
class AgentTotals:
    """Cumulative usage across every session of one agent; `NO_SESSIONS` is the total of none.

    Attributes:
        cents: Summed list cost in USD cents.
        tokens: Summed tokens by component, every component present.
        active_seconds: Summed active seconds.
        web_search_requests: Summed web searches.
    """

    cents: int
    tokens: Mapping[names.TokenType, int]
    active_seconds: float
    web_search_requests: int

    def plus(self, usage: SessionUsage) -> AgentTotals:
        """These totals with one more session's usage added."""
        return AgentTotals(
            cents=self.cents + usage.cents,
            tokens={token_type: count + usage.tokens[token_type] for token_type, count in self.tokens.items()},
            active_seconds=self.active_seconds + usage.active_seconds,
            web_search_requests=self.web_search_requests + usage.web_search_requests,
        )


NO_SESSIONS = AgentTotals(cents=0, tokens=dict.fromkeys(names.TokenType, 0), active_seconds=0.0, web_search_requests=0)


class SessionUsageSource(abc.ABC):
    """Every session in the workspace, archived included, each with its cumulative usage."""

    @abc.abstractmethod
    def sessions(self) -> AsyncIterator[SessionUsage]:
        """Yield every session; raise rather than skip one the totals cannot use."""


class AnthropicSessionUsage(SessionUsageSource):
    """The live source: the Managed Agents sessions list, read through the SDK's cursor pagination."""

    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        self._client = client

    @override
    async def sessions(self) -> AsyncIterator[SessionUsage]:
        async for session in self._client.beta.sessions.list(include_archived=True, limit=_PAGE_SIZE):
            yield session_usage(session)


def _required[T](session_id: str, value: T | None, field: str) -> T:
    if value is None:
        raise SessionRecordError(f'precondition failed: session {session_id} carries no usage.{field}')
    return value


def _cents(session_id: str, cost: anthropic.types.BetaMonetaryAmount) -> int:
    if cost.currency != _CURRENCY:
        raise SessionRecordError(
            f'precondition failed: session {session_id} lists its cost in {cost.currency!r}, not {_CURRENCY}'
        )
    if not _CENTS.fullmatch(cost.amount):
        raise SessionRecordError(
            f'precondition failed: session {session_id} list cost amount {cost.amount!r} is not a whole number of cents'
        )
    return int(cost.amount)


def session_usage(session: anthropic_beta.BetaManagedAgentsSession) -> SessionUsage:
    """Read one session's attribution and cumulative usage.

    Raises:
        SessionRecordError: The session lacks any usage field the totals need, lists its cost in a
            currency other than USD or as anything but a whole number of cents, or names no agent.
    """
    usage = session.usage
    cache_creation = _required(session.id, usage.cache_creation, 'cache_creation')
    server_tools = _required(session.id, usage.server_tool_use, 'server_tool_use')
    if not session.agent.name:
        raise SessionRecordError(f'precondition failed: session {session.id} names no agent')
    return SessionUsage(
        agent=session.agent.name,
        cents=_cents(session.id, _required(session.id, usage.list_cost, 'list_cost')),
        tokens={
            names.TokenType.INPUT: _required(session.id, usage.input_tokens, 'input_tokens'),
            names.TokenType.OUTPUT: _required(session.id, usage.output_tokens, 'output_tokens'),
            names.TokenType.CACHE_READ: _required(session.id, usage.cache_read_input_tokens, 'cache_read_input_tokens'),
            names.TokenType.CACHE_CREATION: (
                _required(
                    session.id, cache_creation.ephemeral_5m_input_tokens, 'cache_creation.ephemeral_5m_input_tokens'
                )
                + _required(
                    session.id, cache_creation.ephemeral_1h_input_tokens, 'cache_creation.ephemeral_1h_input_tokens'
                )
            ),
        },
        active_seconds=_required(session.id, usage.active_seconds, 'active_seconds'),
        web_search_requests=_required(
            session.id, server_tools.web_search_requests, 'server_tool_use.web_search_requests'
        ),
    )
