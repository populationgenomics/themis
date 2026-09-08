"""The spend metrics' vocabulary as the program's dashboards and alert queries read it.

The producers write these names (`themis/telemetry/names.py`); the program cannot import that module, so this
is its copy, held to it name for name by `themis/services/cost_exporter/tests/test_metric_contract.py`. A series
reaches a query as `prometheus.googleapis.com/<name>/gauge` (or `/counter`), with the labels named here. What the
tokens cost is the queries' own multiplier, not a series.
"""

from __future__ import annotations

SESSION_LIST_COST_CENTS = 'themis_anthropic_session_list_cost_cents'
SESSION_TOKENS = 'themis_anthropic_session_tokens'
SESSION_ACTIVE_SECONDS = 'themis_anthropic_session_active_seconds'
SESSION_WEB_SEARCH_REQUESTS = 'themis_anthropic_session_web_search_requests'
EXPORTER_LAST_SUCCESS_TIMESTAMP_SECONDS = 'themis_anthropic_exporter_last_success_timestamp_seconds'
REQUEST_TOKENS = 'themis_anthropic_request_tokens'

AGENT_LABEL = 'agent'
MODEL_LABEL = 'model'
TYPE_LABEL = 'type'
STOP_REASON_LABEL = 'stop_reason'

# The `type` label's values, in the producers' order.
TOKEN_TYPES = ('input', 'output', 'cacheRead', 'cacheCreation')
