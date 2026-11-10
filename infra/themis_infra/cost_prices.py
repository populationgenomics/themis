"""Anthropic list prices as the program's queries price tokens with: the per-model tables and the flat rates.

Cents per million tokens by model id and token type, at the published list price
(https://platform.claude.com/docs/en/about-claude/pricing). No series carries a price: the tables are rendered into
the PromQL the dashboard and the alerts read as literal multipliers (`cost_promql.convert_cents`), so the convert
worker's token counter is priced by what stands here. A model absent from both tables is not priced at zero and
forgotten: its usage pages through the unpriced-usage alert, and the row is added when a caller starts using the
model. Rows are keyed by the id the API reports in `message.model` — Haiku 4.5's is the dated snapshot, the others are
undated.

Most models have one rate card, whatever the request (`LIST_PRICES_CENTS_PER_MTOK`). A model priced by prompt length
has two (`LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH`): a request whose prompt is over 100,000 tokens pays the higher
card on every token of it, so the counter says which side each request fell on (`cost_metrics.PROMPT_LENGTHS`) and
each card prices the tokens counted under its side. A model is in one table or the other, never both
(`priced_models`).

On every card `cacheRead` is a tenth of the input rate, except Sonnet 5.5 at a twentieth and Fable 5.1 and 5.2 at a
fortieth; `cacheCreation` counts both cache lifetimes together and is priced at the 5-minute write rate, 1.25 times
the input rate. A price that is not a whole number of cents is a `decimal.Decimal`, never a float: the published
prices are decimal, and a float cannot hold most of them exactly. The flat rates price the session gauges, which carry
no per-model dimension. Every card names exactly the four `type` values (`cost_metrics.TOKEN_TYPES`) and follows those
derivations; a test holds the tables to both.
"""

from __future__ import annotations

import decimal
from collections.abc import Mapping

RateCard = Mapping[str, int | decimal.Decimal]
"""Cents per million tokens, by token type."""

LIST_PRICES_CENTS_PER_MTOK: Mapping[str, RateCard] = {
    'claude-sonnet-5': {'input': 200, 'output': 1000, 'cacheRead': 20, 'cacheCreation': 250},
    'claude-sonnet-5-5': {'input': 200, 'output': 1000, 'cacheRead': 10, 'cacheCreation': 250},
    'claude-opus-5': {'input': 500, 'output': 2500, 'cacheRead': 50, 'cacheCreation': 625},
    'claude-opus-4-8': {'input': 500, 'output': 2500, 'cacheRead': 50, 'cacheCreation': 625},
    'claude-fable-5-1': {'input': 1000, 'output': 5000, 'cacheRead': 25, 'cacheCreation': 1250},
    'claude-fable-5-2': {'input': 800, 'output': 4000, 'cacheRead': 20, 'cacheCreation': 1000},
    'claude-haiku-4-5-20251001': {'input': 100, 'output': 500, 'cacheRead': 10, 'cacheCreation': 125},
}

# By model, then by the `prompt_length` value a request's tokens are counted under.
LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH: Mapping[str, Mapping[str, RateCard]] = {
    'claude-haiku-5-5': {
        'upTo100k': {'input': 10, 'output': 50, 'cacheRead': 1, 'cacheCreation': decimal.Decimal('12.5')},
        'over100k': {'input': 50, 'output': 250, 'cacheRead': 5, 'cacheCreation': decimal.Decimal('62.5')},
    },
}

# A session's runtime, priced on its `usage.active_seconds`.
SESSION_RUNTIME_CENTS_PER_HOUR = 8
# A server-side web search, counted in `usage.server_tool_use.web_search_requests`.
WEB_SEARCH_CENTS_PER_REQUEST = 1


def priced_models() -> list[str]:
    """Every model the tables price, sorted.

    Raises:
        ValueError: A model is in both tables, which leaves it with two prices.
    """
    in_both = sorted(set(LIST_PRICES_CENTS_PER_MTOK) & set(LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH))
    if in_both:
        raise ValueError(f'priced both at one rate card and by prompt length: {", ".join(in_both)}')
    return sorted([*LIST_PRICES_CENTS_PER_MTOK, *LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH])
