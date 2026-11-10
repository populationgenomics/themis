"""The request-token counter: one direct Messages API call's tokens, by model, type, stop reason and prompt length.

A session's spend reaches the monitor through the sessions list; a direct call's only observer is the
caller, so every such call feeds this counter from its response's `usage`
(`docs/design/cost-monitoring.md`). Tokens, not dollars: the response carries no price; the dashboard's
queries carry the list prices they are multiplied by. Some list prices turn on the length of the request's
prompt, which no sum of tokens over many requests can recover, so each call's tokens are also counted under the
side of that boundary its prompt fell on (`prompt_length`).
"""

from __future__ import annotations

from typing import Protocol

from opentelemetry import metrics

from themis.telemetry import names

# A prompt of more tokens than this is billed at the model's higher list price, where it has one
# (https://platform.claude.com/docs/en/about-claude/pricing).
_PROMPT_LENGTH_BOUNDARY = 100_000


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


def prompt_length(usage: TokenUsage) -> names.PromptLength:
    """The side of the list price's prompt-length boundary one response's request fell on.

    A request's prompt is every token it sent, the cached ones included: the uncached input, the cache reads and
    the cache writes.
    """
    counts = components(usage)
    prompt = counts[names.TokenType.INPUT] + counts[names.TokenType.CACHE_READ] + counts[names.TokenType.CACHE_CREATION]
    return names.PromptLength.OVER_100K if prompt > _PROMPT_LENGTH_BOUNDARY else names.PromptLength.UP_TO_100K


class RequestTokens:
    """The `themis_anthropic_request_tokens` counter over one meter."""

    def __init__(self, meter: metrics.Meter) -> None:
        self._counter = meter.create_counter(
            names.REQUEST_TOKENS,
            description=(
                'Tokens consumed by direct Messages API calls, by model, token type, stop reason and prompt length.'
            ),
        )

    def add(self, usage: TokenUsage, *, model: str, stop_reason: str) -> None:
        """Count one response's tokens, one add per component — zero included, so every series exists.

        Args:
            usage: The response's `usage`: one request's, since the prompt length is read off it.
            model: The model id the response reports.
            stop_reason: The response's stop reason; a truncated or refused turn is billed like any other.
        """
        length = prompt_length(usage)
        for token_type, count in components(usage).items():
            self._counter.add(
                count,
                {
                    names.MODEL_LABEL: model,
                    names.TYPE_LABEL: token_type.value,
                    names.STOP_REASON_LABEL: stop_reason,
                    names.PROMPT_LENGTH_LABEL: length.value,
                },
            )
