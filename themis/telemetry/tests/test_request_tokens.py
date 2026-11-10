"""The request-token counter: four series per (model, stop reason, prompt length), cumulative, zero components kept."""

from __future__ import annotations

import dataclasses

import pytest
from opentelemetry.sdk import metrics as sdk_metrics
from opentelemetry.sdk.metrics import export as metrics_export

from themis.telemetry import names, request_tokens
from themis.testing import in_memory_metrics


@dataclasses.dataclass(frozen=True)
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


def _counter() -> tuple[request_tokens.RequestTokens, metrics_export.InMemoryMetricReader]:
    reader = metrics_export.InMemoryMetricReader()
    provider = sdk_metrics.MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    return request_tokens.RequestTokens(provider.get_meter('test')), reader


def _series(
    *, model: str, token_type: str, stop_reason: str, prompt_length: str = 'upTo100k'
) -> in_memory_metrics.Labels:
    return in_memory_metrics.labels(model=model, type=token_type, stop_reason=stop_reason, prompt_length=prompt_length)


def test_one_call_is_four_series_labelled_by_model_type_stop_reason_and_prompt_length() -> None:
    counter, reader = _counter()

    counter.add(_Usage(4321, 8765, 100, 200), model='claude-sonnet-5', stop_reason='end_turn')

    def series(token_type: str) -> in_memory_metrics.Labels:
        return _series(model='claude-sonnet-5', token_type=token_type, stop_reason='end_turn')

    assert in_memory_metrics.collected(reader)[names.REQUEST_TOKENS] == {
        series('input'): 4321,
        series('output'): 8765,
        series('cacheRead'): 100,
        series('cacheCreation'): 200,
    }


def test_calls_accumulate_and_cache_counts_the_response_omits_are_zero() -> None:
    counter, reader = _counter()

    counter.add(_Usage(10, 20), model='m', stop_reason='end_turn')
    counter.add(_Usage(1, 2, cache_read_input_tokens=5), model='m', stop_reason='max_tokens')
    counter.add(_Usage(100, 200), model='m', stop_reason='end_turn')

    collected = in_memory_metrics.collected(reader)[names.REQUEST_TOKENS]
    assert collected[_series(model='m', token_type='input', stop_reason='end_turn')] == 110
    assert collected[_series(model='m', token_type='cacheRead', stop_reason='end_turn')] == 0
    assert collected[_series(model='m', token_type='cacheRead', stop_reason='max_tokens')] == 5
    assert collected[_series(model='m', token_type='output', stop_reason='max_tokens')] == 2


@pytest.mark.parametrize(
    ('usage', 'expected'),
    [
        pytest.param(_Usage(100_000, 1), names.PromptLength.UP_TO_100K, id='at-the-boundary'),
        pytest.param(_Usage(100_001, 1), names.PromptLength.OVER_100K, id='one-past-the-boundary'),
        # The prompt is everything the request sent: a cached prefix counts whether it was read or written.
        pytest.param(_Usage(1, 1, cache_read_input_tokens=100_000), names.PromptLength.OVER_100K, id='cache-read'),
        pytest.param(_Usage(1, 1, cache_creation_input_tokens=100_000), names.PromptLength.OVER_100K, id='cache-write'),
        pytest.param(_Usage(50_000, 1, 25_000, 25_000), names.PromptLength.UP_TO_100K, id='all-three-at-the-boundary'),
        # The answer is not part of the prompt, however long.
        pytest.param(_Usage(1, 500_000), names.PromptLength.UP_TO_100K, id='long-output'),
    ],
)
def test_a_requests_prompt_length_is_its_input_and_cached_tokens_against_the_boundary(
    usage: _Usage, expected: names.PromptLength
) -> None:
    assert request_tokens.prompt_length(usage) is expected


def test_each_call_counts_all_four_components_under_its_own_prompt_length() -> None:
    counter, reader = _counter()

    counter.add(_Usage(90_000, 7), model='m', stop_reason='end_turn')
    counter.add(_Usage(10, 11, cache_creation_input_tokens=150_000), model='m', stop_reason='end_turn')

    def series(token_type: str, prompt_length: str) -> in_memory_metrics.Labels:
        return _series(model='m', token_type=token_type, stop_reason='end_turn', prompt_length=prompt_length)

    assert in_memory_metrics.collected(reader)[names.REQUEST_TOKENS] == {
        series('input', 'upTo100k'): 90_000,
        series('output', 'upTo100k'): 7,
        series('cacheRead', 'upTo100k'): 0,
        series('cacheCreation', 'upTo100k'): 0,
        series('input', 'over100k'): 10,
        series('output', 'over100k'): 11,
        series('cacheRead', 'over100k'): 0,
        series('cacheCreation', 'over100k'): 150_000,
    }
