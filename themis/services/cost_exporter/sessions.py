"""The workspace's sessions as the spend signal: each session's cumulative list cost, by agent.

The sessions list is the one place a dollar figure is readable with a workspace credential
(`docs/design/cost-monitoring.md`, Background): every session carries its cumulative `usage.list_cost`,
so one paginated listing — archived sessions included, which the API leaves out unless asked — is a
complete snapshot of the workspace. A session the API returns without the fields the total needs is a
precondition failure, never a zero: a silent zero would read as spend shrinking.
"""

from __future__ import annotations

import abc
import dataclasses
import re
from collections.abc import AsyncIterator
from typing import override

import anthropic
from anthropic.types import beta as anthropic_beta

# Sessions per page; the SDK follows the cursor to the next page as the listing is iterated.
_PAGE_SIZE = 100

# `list_cost.amount` is the cent total as a decimal string; anything else the total cannot use.
_CENTS = re.compile(r'[0-9]+')

_CURRENCY = 'USD'


class SessionRecordError(ValueError):
    """A session the API returned lacks a field the total needs, or carries one in an unexpected form."""


@dataclasses.dataclass(frozen=True)
class SessionCost:
    """One session's cumulative list cost.

    Attributes:
        agent: The name of the agent the session ran — the attribution dimension.
        cents: The cumulative list cost in USD cents, as the API reports it (rounded to the cent).
    """

    agent: str
    cents: int


class SessionCostSource(abc.ABC):
    """Every session in the workspace, archived included, each with its cumulative list cost."""

    @abc.abstractmethod
    def sessions(self) -> AsyncIterator[SessionCost]:
        """Yield every session; raise rather than skip one the total cannot use."""


class AnthropicSessionCosts(SessionCostSource):
    """The live source: the Managed Agents sessions list, read through the SDK's cursor pagination."""

    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        self._client = client

    @override
    async def sessions(self) -> AsyncIterator[SessionCost]:
        async for session in self._client.beta.sessions.list(include_archived=True, limit=_PAGE_SIZE):
            yield session_cost(session)


def session_cost(session: anthropic_beta.BetaManagedAgentsSession) -> SessionCost:
    """Read one session's attribution and cumulative list cost.

    Raises:
        SessionRecordError: The session has no list cost, a currency other than USD, an amount that is
            not a whole number of cents, or no agent name.
    """
    cost = session.usage.list_cost
    # A session that has spent nothing reports an amount of "0"; the API has not been seen to omit the field.
    if cost is None:
        raise SessionRecordError(f'precondition failed: session {session.id} carries no usage.list_cost')
    if cost.currency != _CURRENCY:
        raise SessionRecordError(
            f'precondition failed: session {session.id} lists its cost in {cost.currency!r}, not {_CURRENCY}'
        )
    if not _CENTS.fullmatch(cost.amount):
        raise SessionRecordError(
            f'precondition failed: session {session.id} list cost amount {cost.amount!r} is not a whole number of cents'
        )
    if not session.agent.name:
        raise SessionRecordError(f'precondition failed: session {session.id} names no agent')
    return SessionCost(agent=session.agent.name, cents=int(cost.amount))
