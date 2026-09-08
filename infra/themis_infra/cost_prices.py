"""Anthropic list prices as the program's queries price tokens with: the per-model table and the flat rates.

Cents per million tokens by model id and token type, at the published list price
(https://platform.claude.com/docs/en/about-claude/pricing). No series carries a price: the table is rendered into
the PromQL the dashboard and the alerts read as literal multipliers (`cost_promql.convert_cents`), so the convert
worker's token counter is priced by what stands here. A model absent from the table is not priced at zero and
forgotten: its usage pages through the unpriced-usage alert, and the row is added when a caller starts using the
model. Rows are keyed by the id the API reports in `message.model` — Haiku's is the dated snapshot, the others are
undated. `cacheRead` is a tenth of the input rate, except Fable 5.1 at a fortieth; `cacheCreation` counts both cache
lifetimes together and is priced at the 5-minute write rate, 1.25 times the input rate. The flat rates price the
session gauges, which carry no per-model dimension. Every row names exactly the four `type` values
(`cost_metrics.TOKEN_TYPES`) and follows those derivations; a test holds it to both.
"""

from __future__ import annotations

from collections.abc import Mapping

LIST_PRICES_CENTS_PER_MTOK: Mapping[str, Mapping[str, int]] = {
    'claude-sonnet-5': {'input': 200, 'output': 1000, 'cacheRead': 20, 'cacheCreation': 250},
    'claude-opus-5': {'input': 500, 'output': 2500, 'cacheRead': 50, 'cacheCreation': 625},
    'claude-opus-4-8': {'input': 500, 'output': 2500, 'cacheRead': 50, 'cacheCreation': 625},
    'claude-fable-5-1': {'input': 1000, 'output': 5000, 'cacheRead': 25, 'cacheCreation': 1250},
    'claude-haiku-4-5-20251001': {'input': 100, 'output': 500, 'cacheRead': 10, 'cacheCreation': 125},
}

# A session's runtime, priced on its `usage.active_seconds`.
SESSION_RUNTIME_CENTS_PER_HOUR = 8
# A server-side web search, counted in `usage.server_tool_use.web_search_requests`.
WEB_SEARCH_CENTS_PER_REQUEST = 1
