"""The request-token counter: four series per (model, stop reason), cumulative across calls, zero components kept."""

from __future__ import annotations

import dataclasses

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


def test_one_call_is_four_series_labelled_by_model_type_and_stop_reason() -> None:
    counter, reader = _counter()

    counter.add(_Usage(4321, 8765, 100, 200), model='claude-sonnet-5', stop_reason='end_turn')

    def series(token_type: str) -> in_memory_metrics.Labels:
        return in_memory_metrics.labels(model='claude-sonnet-5', type=token_type, stop_reason='end_turn')

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
    assert collected[in_memory_metrics.labels(model='m', type='input', stop_reason='end_turn')] == 110
    assert collected[in_memory_metrics.labels(model='m', type='cacheRead', stop_reason='end_turn')] == 0
    assert collected[in_memory_metrics.labels(model='m', type='cacheRead', stop_reason='max_tokens')] == 5
    assert collected[in_memory_metrics.labels(model='m', type='output', stop_reason='max_tokens')] == 2
