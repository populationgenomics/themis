"""The PromQL builders: the selector's form follows the name, the window function the kind, and dollars divide once."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from themis_infra import claude_code_metrics, cost_metrics, cost_prices, cost_promql

_TICK = 5
_NOT_IN_TABLE = 'claude-3-opus-20240229'
_AWKWARD_VALUES = ('claude-3.5', 'back\\slash', 'quo"te', 'a|b')


def _decode_string_literal(literal: str) -> str:
    """What PromQL reads from a double-quoted string: the quotes off, the escaped backslash and quote restored."""
    assert literal.startswith('"')
    assert literal.endswith('"')
    return re.sub(r'\\([\\"])', r'\1', literal[1:-1])


def test_a_legacy_name_selects_bare_and_a_dotted_name_selects_quoted() -> None:
    assert cost_promql.selector(cost_metrics.REQUEST_TOKENS) == cost_metrics.REQUEST_TOKENS
    assert (
        cost_promql.selector(cost_metrics.REQUEST_TOKENS, 'model="m"', 'type="input"')
        == f'{cost_metrics.REQUEST_TOKENS}{{model="m", type="input"}}'
    )
    assert cost_promql.selector(claude_code_metrics.COST_USAGE) == f'{{"{claude_code_metrics.COST_USAGE}"}}'
    assert (
        cost_promql.selector(claude_code_metrics.TOKEN_USAGE, 'type="input"')
        == f'{{"{claude_code_metrics.TOKEN_USAGE}", type="input"}}'
    )


def test_a_name_no_producer_writes_is_refused() -> None:
    with pytest.raises(KeyError, match='not a spend metric'):
        cost_promql.selector('themis_anthropic_session_list_cost')


@pytest.mark.parametrize('name', sorted(cost_promql.METRICS))
def test_the_window_function_follows_the_kind(name: str) -> None:
    # Monitoring refuses `rate`/`increase` on a gauge and `delta` is the wrong figure for a counter.
    figure = cost_promql.window(name, '1h')
    expected = 'delta(' if cost_promql.kind(name) is cost_promql.Kind.GAUGE else 'increase('
    assert figure.startswith(expected)
    assert figure.endswith('[1h])')
    assert 'rate(' not in figure


def test_only_a_gauge_has_a_current_value_and_it_survives_a_missed_tick() -> None:
    latest = cost_promql.latest(cost_metrics.SESSION_LIST_COST_CENTS, 'agent="a"', tick_minutes=_TICK)
    match = re.fullmatch(
        rf'last_over_time\({re.escape(cost_metrics.SESSION_LIST_COST_CENTS)}\{{agent="a"\}}\[(\d+)m\]\)', latest
    )
    assert match, latest
    lookback = int(match.group(1))
    assert lookback % _TICK == 0
    assert lookback >= 2 * _TICK
    with pytest.raises(ValueError, match='counter'):
        cost_promql.latest(cost_metrics.REQUEST_TOKENS, tick_minutes=_TICK)


def test_silence_is_absence_over_the_whole_metric() -> None:
    heartbeat = cost_metrics.EXPORTER_LAST_SUCCESS_TIMESTAMP_SECONDS
    assert cost_promql.silence(heartbeat, '30m') == f'absent_over_time({heartbeat}[30m])'


def test_a_dotted_model_id_is_matched_literally() -> None:
    assert cost_promql.not_among('model', ['claude-3.5', 'c']) == 'model!~"^(c|claude-3\\\\.5)$"'
    assert cost_promql.equals('model', 'claude-3.5') == 'model="claude-3.5"'


@pytest.mark.parametrize('value', _AWKWARD_VALUES)
def test_matchers_survive_both_escaping_layers(value: str) -> None:
    # Decoded as PromQL decodes the string, the equality matcher is the value itself and the RE2 accepts it — and
    # nothing that differs by a metacharacter's reading.
    [equal] = re.findall(r'^model=(".*")$', cost_promql.equals('model', value))
    assert _decode_string_literal(equal) == value
    [literal] = re.findall(r'^model!~(".*")$', cost_promql.not_among('model', [value, 'other']))
    regex = re.compile(_decode_string_literal(literal))
    assert regex.fullmatch(value)
    assert regex.fullmatch('other')
    assert not regex.fullmatch(value.replace('.', 'x').replace('|', 'x') + 'x')


def test_a_total_guards_every_term_only_when_there_is_more_than_one() -> None:
    assert cost_promql.total(['a']) == 'a'
    assert cost_promql.total(['a', 'b']) == '((a or vector(0)) + (b or vector(0)))'
    assert cost_promql.summed(['a', 'b']) == '(a + b)'
    assert cost_promql.summed(['a']) == 'a'
    with pytest.raises(ValueError, match='at least one'):
        cost_promql.summed([])


def test_the_workspace_total_guards_the_producers_that_can_be_empty(
    top_level_terms: Callable[[str], list[str]],
) -> None:
    total = cost_promql.workspace_cents('30m')
    sessions, convert, ci = top_level_terms(total)
    # Sessions and CI read one series set each and can be empty; the convert figure is a sum of guarded terms.
    assert sessions == cost_promql.guarded(cost_promql.session_cents('30m'))
    assert ci == cost_promql.guarded(cost_promql.ci_cents('30m'))
    assert convert == cost_promql.convert_cents('30m')
    assert not convert.endswith(' or vector(0))')
    assert set(re.findall(r'\[(\w+)\]', total)) == {'30m'}


def test_convert_cents_prices_each_models_four_types_at_the_table(
    top_level_terms: Callable[[str], list[str]],
) -> None:
    cents = cost_promql.convert_cents('1h')
    table = cost_prices.LIST_PRICES_CENTS_PER_MTOK
    for model, row in table.items():
        for token_type, price in row.items():
            term = (
                f'sum(increase({cost_metrics.REQUEST_TOKENS}{{{cost_metrics.MODEL_LABEL}="{model}", '
                f'{cost_metrics.TYPE_LABEL}="{token_type}"}}[1h])) * {price} or vector(0)'
            )
            assert term in cents, term
    # One never-empty figure per model, each scaled from cents per million tokens, added without a guard of its own;
    # inside it every type term is guarded.
    models = top_level_terms(cents)
    assert len(models) == len(table)
    for figure in models:
        assert figure.endswith(' / 1000000'), figure
        assert len(top_level_terms(figure.removesuffix(' / 1000000'))) == len(cost_metrics.TOKEN_TYPES)
    assert set(re.findall(r'\* (\d+) or vector', cents)) == {str(p) for row in table.values() for p in row.values()}


def test_convert_cents_by_model_carries_the_model_label_and_drops_an_idle_model() -> None:
    by_model = cost_promql.convert_cents_by_model('1h')
    series = by_model.split(' or label_replace(')
    assert len(series) == len(cost_prices.LIST_PRICES_CENTS_PER_MTOK)
    for model in cost_prices.LIST_PRICES_CENTS_PER_MTOK:
        assert f', "{cost_metrics.MODEL_LABEL}", "{model}", "", "") > 0' in by_model, model


def test_unpriced_usage_is_every_model_outside_the_table() -> None:
    unpriced = cost_promql.unpriced_request_tokens('1h')
    assert unpriced.startswith(f'sum by ({cost_metrics.MODEL_LABEL}, {cost_metrics.TYPE_LABEL}) (increase(')
    [literal] = re.findall(rf'{cost_metrics.MODEL_LABEL}!~(".*?")\}}', unpriced)
    regex = re.compile(_decode_string_literal(literal))
    for model in cost_prices.LIST_PRICES_CENTS_PER_MTOK:
        assert regex.fullmatch(model), model
    assert not regex.fullmatch(_NOT_IN_TABLE)
    assert literal.count('|') == len(cost_prices.LIST_PRICES_CENTS_PER_MTOK) - 1


def test_the_session_components_price_at_the_flat_rates_and_tokens_are_the_remainder() -> None:
    runtime = cost_promql.session_runtime_cents('1h')
    search = cost_promql.session_search_cents('1h')
    assert runtime.endswith(f'/ 3600 * {cost_prices.SESSION_RUNTIME_CENTS_PER_HOUR}')
    assert search.endswith(f'* {cost_prices.WEB_SEARCH_CENTS_PER_REQUEST}')
    assert cost_promql.session_token_cents('1h') == f'({cost_promql.session_cents("1h")} - {runtime} - {search})'


@pytest.mark.parametrize(
    ('cents', 'expected'),
    [
        ('sum(x)', 'sum(x) / 100'),
        ('sum by (a) (x) / 3600 * 8', 'sum by (a) (x) / 3600 * 8 / 100'),
        ('sum(x) - sum(y)', '(sum(x) - sum(y)) / 100'),
        ('sum(x) or vector(0)', '(sum(x) or vector(0)) / 100'),
        ('(sum(x) - sum(y))', '(sum(x) - sum(y)) / 100'),
        # A comparison binds looser than `/` too: bare, `sum(x) > 0 / 100` would compare against a hundredth.
        ('sum(x) > 0', '(sum(x) > 0) / 100'),
        ('sum(x) != 0', '(sum(x) != 0) / 100'),
        ('sum(x) <= sum(y)', '(sum(x) <= sum(y)) / 100'),
    ],
)
def test_dollars_parenthesize_what_binds_looser_than_division(cents: str, expected: str) -> None:
    assert cost_promql.dollars(cents) == expected


def test_a_one_row_table_still_divides_the_whole_by_model_figure(monkeypatch: pytest.MonkeyPatch) -> None:
    # With one model there is no `or` between series to force the parentheses; the `> 0` alone must.
    monkeypatch.setattr(cost_prices, 'LIST_PRICES_CENTS_PER_MTOK', {'claude-sonnet-5': {'input': 200, 'output': 1000}})
    by_model = cost_promql.convert_cents_by_model('1h')
    assert by_model.startswith('label_replace(')
    assert by_model.endswith('"", "") > 0')
    assert cost_promql.dollars(by_model) == f'({by_model}) / 100'
