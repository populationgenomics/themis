"""The request-token counter: what one direct Messages API call consumed, by model, token type and stop reason.

A session's spend reaches the monitor through the sessions list; a direct call's only observer is the
caller, so every such call feeds this counter from its response's `usage`
(`docs/design/cost-monitoring.md`). Tokens, not dollars: the response carries no price; the dashboard's
queries carry the list prices they are multiplied by.
"""

from __future__ import annotations

from typing import Protocol

from opentelemetry import metrics

from themis.telemetry import names


class TokenUsage(Protocol):
    """The token counts on a Messages API response's `usage`; `anthropic.types.Usage` has this shape."""

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...

    @property
    def cache_read_input_tokens(self) -> int | None: ...

    @property
    def cache_creation_input_tokens(self) -> int | None: ...


def components(usage: TokenUsage) -> dict[names.TokenType, int]:
    """The four token components of one response.

    The cache counts are optional on the response: they arrived with prompt caching and are absent
    where no cache was touched, so an absent one is zero tokens, not a missing input.
    """
    return {
        names.TokenType.INPUT: usage.input_tokens,
        names.TokenType.OUTPUT: usage.output_tokens,
        names.TokenType.CACHE_READ: usage.cache_read_input_tokens or 0,
        names.TokenType.CACHE_CREATION: usage.cache_creation_input_tokens or 0,
    }


class RequestTokens:
    """The `themis_anthropic_request_tokens` counter over one meter."""

    def __init__(self, meter: metrics.Meter) -> None:
        self._counter = meter.create_counter(
            names.REQUEST_TOKENS,
            description='Tokens consumed by direct Messages API calls, by model, token type and stop reason.',
        )

    def add(self, usage: TokenUsage, *, model: str, stop_reason: str) -> None:
        """Count one response's tokens, one add per component — zero included, so every series exists.

        Args:
            usage: The response's `usage`.
            model: The model id the response reports.
            stop_reason: The response's stop reason; a truncated or refused turn is billed like any other.
        """
        for token_type, count in components(usage).items():
            self._counter.add(
                count,
                {names.MODEL_LABEL: model, names.TYPE_LABEL: token_type.value, names.STOP_REASON_LABEL: stop_reason},
            )
