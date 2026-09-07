"""Reading one session's attribution and cost off the SDK's session type, and what is refused.

Sessions are built with `model_construct` — the SDK's own types, without the dozens of unrelated
required fields a validated construction would demand.
"""

from __future__ import annotations

import pytest
from anthropic import types as anthropic_types
from anthropic.types import beta as anthropic_beta

from themis.services.cost_exporter import sessions


def _session(
    *,
    session_id: str = 'sesn_01',
    agent: str = 'SVCv4 variant classifier',
    amount: str | None = '215',
    currency: str = 'USD',
) -> anthropic_beta.BetaManagedAgentsSession:
    list_cost = (
        None if amount is None else anthropic_types.BetaMonetaryAmount.model_construct(amount=amount, currency=currency)
    )
    return anthropic_beta.BetaManagedAgentsSession.model_construct(
        id=session_id,
        agent=anthropic_beta.BetaManagedAgentsSessionAgent.model_construct(name=agent),
        usage=anthropic_beta.BetaManagedAgentsSessionUsage.model_construct(list_cost=list_cost),
    )


def test_a_session_reads_as_its_agent_and_cent_total() -> None:
    assert sessions.session_cost(_session()) == sessions.SessionCost(agent='SVCv4 variant classifier', cents=215)


def test_a_zero_cost_session_is_zero_cents() -> None:
    assert sessions.session_cost(_session(amount='0')).cents == 0


@pytest.mark.parametrize(
    ('session', 'complaint'),
    [
        pytest.param(_session(amount=None), 'no usage.list_cost', id='no-list-cost'),
        pytest.param(_session(currency='EUR'), "'EUR', not USD", id='foreign-currency'),
        pytest.param(_session(amount='12.5'), 'not a whole number of cents', id='fractional-amount'),
        pytest.param(_session(amount='-3'), 'not a whole number of cents', id='negative-amount'),
        pytest.param(_session(amount=''), 'not a whole number of cents', id='empty-amount'),
        pytest.param(_session(agent=''), 'names no agent', id='no-agent-name'),
    ],
)
def test_a_session_the_total_cannot_use_is_a_precondition_failure(
    session: anthropic_beta.BetaManagedAgentsSession, complaint: str
) -> None:
    with pytest.raises(sessions.SessionRecordError, match=complaint) as failure:
        sessions.session_cost(session)
    assert 'sesn_01' in str(failure.value)
