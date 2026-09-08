"""The metric vocabulary of Anthropic spend: one fixed set of names and labels, shared by every producer.

The cost exporter writes the session gauges and its heartbeat, a direct Messages API caller the
request counter, and the Pulumi program's dashboards and alerts read them by these names
(`infra/themis_infra/cost_metrics.py` carries its own copy, which a test holds to this module).
Underscores, no dots: a dotted name needs UTF-8 quoting in PromQL.
"""

from __future__ import annotations

import enum

# Gauges the cost exporter sets once per run, cumulative over every session of the agent.
SESSION_LIST_COST_CENTS = 'themis_anthropic_session_list_cost_cents'
SESSION_TOKENS = 'themis_anthropic_session_tokens'
SESSION_ACTIVE_SECONDS = 'themis_anthropic_session_active_seconds'
SESSION_WEB_SEARCH_REQUESTS = 'themis_anthropic_session_web_search_requests'
# The exporter's heartbeat: the Unix time of its last successful run, written whether or not the workspace
# has any session, so its absence is what the freshness alert watches.
EXPORTER_LAST_SUCCESS_TIMESTAMP_SECONDS = 'themis_anthropic_exporter_last_success_timestamp_seconds'
# The counter a direct Messages API call increments with each of its token components.
REQUEST_TOKENS = 'themis_anthropic_request_tokens'

# The instrumentation scope every producer's meter carries; the API stamps it on each series, so it is one
# fixed name rather than a module path that a move would change.
METER_NAME = 'themis'

AGENT_LABEL = 'agent'
MODEL_LABEL = 'model'
TYPE_LABEL = 'type'
STOP_REASON_LABEL = 'stop_reason'


class TokenType(enum.StrEnum):
    """The `type` label's values — Claude Code's vocabulary for a turn's token components.

    `CACHE_CREATION` is `cache_creation_input_tokens` whole: both cache lifetimes together.
    """

    INPUT = 'input'
    OUTPUT = 'output'
    CACHE_READ = 'cacheRead'
    CACHE_CREATION = 'cacheCreation'
