"""Reading one session's attribution and usage off the SDK's session type, and what is refused.

Sessions are built with `model_construct` — the SDK's own types, without the dozens of unrelated
required fields a validated construction would demand.
"""

from __future__ import annotations

import pytest
from anthropic import types as anthropic_types
from anthropic.types import beta as anthropic_beta

from themis.services.cost_exporter import sessions
from themis.telemetry import names

# A complete usage, as the API reports one; each test varies one field off it.
_USAGE = {
    'list_cost': ('215', 'USD'),
    'input_tokens': 1000,
    'output_tokens': 200,
    'cache_read_input_tokens': 300,
    'cache_creation': (40, 2),
    'active_seconds': 61.5,
    'server_tool_use': 3,
}


def _session(
    *,
    session_id: str = 'sesn_01',
    agent: str = 'SVCv4 variant classifier',
    **overrides: object,
) -> anthropic_beta.BetaManagedAgentsSession:
    fields = {**_USAGE, **overrides}
    list_cost = fields['list_cost']
    cache_creation = fields['cache_creation']
    server_tool_use = fields['server_tool_use']
    usage = anthropic_beta.BetaManagedAgentsSessionUsage.model_construct(
        list_cost=(
            None
            if list_cost is None
            else anthropic_types.BetaMonetaryAmount.model_construct(amount=list_cost[0], currency=list_cost[1])  # type: ignore[index]
        ),
        input_tokens=fields['input_tokens'],
        output_tokens=fields['output_tokens'],
        cache_read_input_tokens=fields['cache_read_input_tokens'],
        cache_creation=(
            None
            if cache_creation is None
            else anthropic_beta.BetaManagedAgentsCacheCreationUsage.model_construct(
                ephemeral_5m_input_tokens=cache_creation[0],  # type: ignore[index]
                ephemeral_1h_input_tokens=cache_creation[1],  # type: ignore[index]
            )
        ),
        active_seconds=fields['active_seconds'],
        server_tool_use=(
            None
            if server_tool_use is None
            else anthropic_beta.BetaManagedAgentsServerToolUsage.model_construct(web_search_requests=server_tool_use)
        ),
    )
    return anthropic_beta.BetaManagedAgentsSession.model_construct(
        id=session_id,
        agent=anthropic_beta.BetaManagedAgentsSessionAgent.model_construct(name=agent),
        usage=usage,
    )


def test_a_session_reads_as_its_agent_and_cumulative_usage() -> None:
    assert sessions.session_usage(_session()) == sessions.SessionUsage(
        agent='SVCv4 variant classifier',
        cents=215,
        tokens={
            names.TokenType.INPUT: 1000,
            names.TokenType.OUTPUT: 200,
            names.TokenType.CACHE_READ: 300,
            names.TokenType.CACHE_CREATION: 42,
        },
        active_seconds=61.5,
        web_search_requests=3,
    )


def test_a_session_that_has_done_nothing_is_all_zeros() -> None:
    usage = sessions.session_usage(
        _session(
            list_cost=('0', 'USD'),
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation=(0, 0),
            active_seconds=0.0,
            server_tool_use=0,
        )
    )
    assert usage.cents == 0
    assert set(usage.tokens.values()) == {0}
    assert usage.active_seconds == 0.0
    assert usage.web_search_requests == 0


@pytest.mark.parametrize(
    ('session', 'complaint'),
    [
        pytest.param(_session(list_cost=None), 'no usage.list_cost', id='no-list-cost'),
        pytest.param(_session(list_cost=('215', 'EUR')), "'EUR', not USD", id='foreign-currency'),
        pytest.param(_session(list_cost=('12.5', 'USD')), 'not a whole number of cents', id='fractional-amount'),
        pytest.param(_session(list_cost=('-3', 'USD')), 'not a whole number of cents', id='negative-amount'),
        pytest.param(_session(list_cost=('', 'USD')), 'not a whole number of cents', id='empty-amount'),
        pytest.param(_session(input_tokens=None), 'no usage.input_tokens', id='no-input-tokens'),
        pytest.param(_session(output_tokens=None), 'no usage.output_tokens', id='no-output-tokens'),
        pytest.param(_session(cache_read_input_tokens=None), 'no usage.cache_read_input_tokens', id='no-cache-read'),
        pytest.param(_session(cache_creation=None), 'no usage.cache_creation', id='no-cache-creation'),
        pytest.param(_session(cache_creation=(None, 2)), 'cache_creation.ephemeral_5m_input_tokens', id='no-5m'),
        pytest.param(_session(cache_creation=(40, None)), 'cache_creation.ephemeral_1h_input_tokens', id='no-1h'),
        pytest.param(_session(active_seconds=None), 'no usage.active_seconds', id='no-active-seconds'),
        pytest.param(_session(server_tool_use=None), 'no usage.server_tool_use', id='no-server-tool-use'),
        pytest.param(_session(agent=''), 'names no agent', id='no-agent-name'),
    ],
)
def test_a_session_the_totals_cannot_use_is_a_precondition_failure(
    session: anthropic_beta.BetaManagedAgentsSession, complaint: str
) -> None:
    with pytest.raises(sessions.SessionRecordError, match=complaint) as failure:
        sessions.session_usage(session)
    assert 'sesn_01' in str(failure.value)


def test_a_server_tool_usage_without_web_searches_is_a_precondition_failure() -> None:
    session = _session()
    session.usage.server_tool_use = anthropic_beta.BetaManagedAgentsServerToolUsage.model_construct(
        web_fetch_requests=1
    )
    with pytest.raises(sessions.SessionRecordError, match=r'server_tool_use\.web_search_requests'):
        sessions.session_usage(session)


def test_totals_add_every_component_of_a_session() -> None:
    first = sessions.session_usage(_session())
    second = sessions.session_usage(_session(list_cost=('5', 'USD'), input_tokens=1, active_seconds=0.5))

    totals = sessions.NO_SESSIONS.plus(first).plus(second)

    assert totals.cents == 220
    assert totals.tokens[names.TokenType.INPUT] == 1001
    assert totals.tokens[names.TokenType.CACHE_CREATION] == 84
    assert totals.active_seconds == 62.0
    assert totals.web_search_requests == 6
