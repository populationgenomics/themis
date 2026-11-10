"""The PromQL builders: the selector's form follows the name, the window figure the kind, and dollars divide once."""

from __future__ import annotations

import decimal
import re
from collections.abc import Callable

import pytest

from themis_infra import claude_code_metrics, cost_metrics, cost_prices, cost_promql, promql

# The freshness window every read of a running total tolerates as staleness; the tests' windows exceed it.
_FRESHNESS = 30
_NOT_IN_TABLE = 'claude-3-opus-20240229'
_AWKWARD_VALUES = ('claude-3.5', 'back\\slash', 'quo"te', 'a|b')
_GAUGES = sorted(name for name in cost_promql.METRICS if cost_promql.kind(name) is cost_promql.Kind.GAUGE)
_COUNTERS = sorted(name for name in cost_promql.METRICS if cost_promql.kind(name) is cost_promql.Kind.COUNTER)


# One label matcher of a selector: the label, the operator and the string literal's body.
_MATCHER = re.compile(r'(\w+)(=~|!~|!=|=)"((?:[^"\\]|\\.)*)"')


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


@pytest.mark.parametrize(('duration', 'expected'), [('30s', 30), ('30m', 1800), ('1h', 3600), ('7d', 604_800)])
def test_a_duration_of_one_unit_is_read_in_seconds(duration: str, expected: int) -> None:
    assert cost_promql.seconds(duration) == expected


@pytest.mark.parametrize('duration', ['1h30m', '90', 'h', '1y'])
def test_a_duration_of_another_form_is_refused(duration: str) -> None:
    with pytest.raises(ValueError, match='duration'):
        cost_promql.seconds(duration)


@pytest.mark.parametrize('name', _GAUGES)
def test_a_gauges_window_figure_is_the_exact_rise_read_within_the_freshness_window(name: str) -> None:
    # Never extrapolated: the newest sample less the newest a window earlier, each within the tolerance; or the
    # rise since the first sample for a series younger than the window.
    figure = cost_promql.rise(name, '1h', 'agent="a"', freshness_minutes=_FRESHNESS)
    selected = re.escape(cost_promql.selector(name, 'agent="a"'))
    assert 'delta(' not in figure
    match = re.fullmatch(
        rf'sum\(\(\(last_over_time\({selected}\[(\d+)m\]\) - last_over_time\({selected}\[(\d+)m\] offset 1h\)\)'
        rf' or \(last_over_time\({selected}\[(\d+)m\]\) - min_over_time\({selected}\[1h\]\)\)\)\)',
        figure,
    )
    assert match, figure
    assert {int(g) for g in match.groups()} == {_FRESHNESS}
    with pytest.raises(ValueError, match='not a counter'):
        cost_promql.increase(name, '1h')


@pytest.mark.parametrize('name', _COUNTERS)
def test_a_counters_window_figure_is_its_increase(name: str) -> None:
    assert cost_promql.increase(name, '1h', by=['x']) == f'sum by (x) (increase({cost_promql.selector(name)}[1h]))'
    with pytest.raises(ValueError, match='not a gauge'):
        cost_promql.rise(name, '1h', freshness_minutes=_FRESHNESS)


@pytest.mark.parametrize('duration', [f'{_FRESHNESS}m', f'{_FRESHNESS - 5}m', f'{_FRESHNESS * 60}s'])
def test_a_rise_over_a_window_within_the_tolerance_is_refused(duration: str) -> None:
    # Both ends could read the same sample.
    with pytest.raises(ValueError, match='does not exceed'):
        cost_promql.rise(cost_metrics.SESSION_LIST_COST_CENTS, duration, freshness_minutes=_FRESHNESS)


def test_every_session_figure_is_one_exact_rise_per_gauge_with_its_fallback() -> None:
    figures = {
        cost_promql.session_cents('1h', freshness_minutes=_FRESHNESS): 1,
        cost_promql.session_runtime_cents('1h', freshness_minutes=_FRESHNESS): 1,
        cost_promql.session_search_cents('1h', freshness_minutes=_FRESHNESS): 1,
        cost_promql.session_token_cents('1h', freshness_minutes=_FRESHNESS): 3,
    }
    for figure, gauges in figures.items():
        assert 'delta(' not in figure
        assert figure.count(' offset 1h)') == gauges, figure
        assert figure.count(' or (last_over_time(') == gauges, figure
        assert figure.count('min_over_time(') == gauges, figure
        assert set(re.findall(r'\[(\w+)\]', figure)) == {'1h', f'{_FRESHNESS}m'}, figure


def test_only_a_gauge_has_a_current_value_read_within_the_freshness_window() -> None:
    latest = cost_promql.latest(cost_metrics.SESSION_LIST_COST_CENTS, 'agent="a"', freshness_minutes=_FRESHNESS)
    assert latest == f'last_over_time({cost_metrics.SESSION_LIST_COST_CENTS}{{agent="a"}}[{_FRESHNESS}m])'
    with pytest.raises(ValueError, match='not a gauge'):
        cost_promql.latest(cost_metrics.REQUEST_TOKENS, freshness_minutes=_FRESHNESS)


def test_silence_is_absence_over_the_whole_metric() -> None:
    heartbeat = cost_metrics.EXPORTER_LAST_SUCCESS_TIMESTAMP_SECONDS
    assert cost_promql.silence(heartbeat, '30m') == f'absent_over_time({heartbeat}[30m])'


def test_a_dotted_model_id_is_matched_literally() -> None:
    assert promql.not_among('model', ['claude-3.5', 'c']) == 'model!~"^(c|claude-3\\\\.5)$"'
    assert promql.equals('model', 'claude-3.5') == 'model="claude-3.5"'


@pytest.mark.parametrize('value', _AWKWARD_VALUES)
def test_matchers_survive_both_escaping_layers(value: str) -> None:
    # Decoded as PromQL decodes the string, the equality matcher is the value itself and the RE2 accepts it — and
    # nothing that differs by a metacharacter's reading.
    [equal] = re.findall(r'^model=(".*")$', promql.equals('model', value))
    assert _decode_string_literal(equal) == value
    [literal] = re.findall(r'^model!~(".*")$', promql.not_among('model', [value, 'other']))
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
    total = cost_promql.workspace_cents('1h', freshness_minutes=_FRESHNESS)
    sessions, convert, ci = top_level_terms(total)
    # Sessions and CI read one series set each and can be empty; the convert figure is a sum of guarded terms.
    assert sessions == cost_promql.guarded(cost_promql.session_cents('1h', freshness_minutes=_FRESHNESS))
    assert ci == cost_promql.guarded(cost_promql.ci_cents('1h'))
    assert convert == cost_promql.convert_cents('1h')
    assert not convert.endswith(' or vector(0))')
    # The window, and the tolerance each end of a rise reads within; nothing else.
    assert set(re.findall(r'\[(\w+)\]', total)) == {'1h', f'{_FRESHNESS}m'}


def _priced_terms() -> dict[str, list[str]]:
    """By priced model, every term its figure has to carry: one per token type of each of its rate cards."""
    terms: dict[str, list[str]] = {}
    for model, card in cost_prices.LIST_PRICES_CENTS_PER_MTOK.items():
        terms[model] = [
            f'sum(increase({cost_metrics.REQUEST_TOKENS}{{{cost_metrics.MODEL_LABEL}="{model}", '
            f'{cost_metrics.TYPE_LABEL}="{token_type}"}}[1h])) * {price} or vector(0)'
            for token_type, price in card.items()
        ]
    for model, cards in cost_prices.LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH.items():
        terms[model] = [
            f'sum(increase({cost_metrics.REQUEST_TOKENS}{{{cost_metrics.MODEL_LABEL}="{model}", '
            f'{cost_metrics.PROMPT_LENGTH_LABEL}="{prompt_length}", '
            f'{cost_metrics.TYPE_LABEL}="{token_type}"}}[1h])) * {price} or vector(0)'
            for prompt_length, card in cards.items()
            for token_type, price in card.items()
        ]
    return terms


def test_convert_cents_prices_each_token_type_of_each_rate_card_at_the_tables(
    top_level_terms: Callable[[str], list[str]],
) -> None:
    cents = cost_promql.convert_cents('1h')
    priced = _priced_terms()
    assert sorted(priced) == cost_prices.priced_models()
    # One never-empty figure per model, each scaled from cents per million tokens, added without a guard of its own;
    # inside it every term is guarded, and the terms are exactly the model's cards' token types.
    figures = top_level_terms(cents)
    assert len(figures) == len(priced)
    for model, figure in zip(cost_prices.priced_models(), figures, strict=True):
        assert figure.endswith(' / 1000000'), figure
        terms = top_level_terms(figure.removesuffix(' / 1000000'))
        assert sorted(terms) == sorted(f'({term})' for term in priced[model]), model


def test_a_model_priced_by_prompt_length_prices_each_side_of_the_boundary_at_its_own_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A request over the boundary pays the higher card on every token of it, so neither card may price the other
    # side's tokens, and none may be priced whatever their length.
    monkeypatch.setattr(cost_prices, 'LIST_PRICES_CENTS_PER_MTOK', {})
    monkeypatch.setattr(
        cost_prices,
        'LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH',
        {'m': {'upTo100k': {'input': 10}, 'over100k': {'input': 50}}},
    )
    tokens = cost_metrics.REQUEST_TOKENS
    assert cost_promql.convert_cents('1h') == (
        f'((sum(increase({tokens}{{model="m", prompt_length="upTo100k", type="input"}}[1h])) * 10 or vector(0))'
        f' + (sum(increase({tokens}{{model="m", prompt_length="over100k", type="input"}}[1h])) * 50 or vector(0)))'
        ' / 1000000'
    )


def test_a_price_that_is_not_whole_cents_renders_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cost_prices, 'LIST_PRICES_CENTS_PER_MTOK', {'m': {'cacheCreation': decimal.Decimal('12.5')}})
    monkeypatch.setattr(cost_prices, 'LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH', {})
    assert cost_promql.convert_cents('1h').endswith('[1h])) * 12.5 / 1000000')


def test_convert_cents_by_model_carries_the_model_label_and_drops_an_idle_model() -> None:
    by_model = cost_promql.convert_cents_by_model('1h')
    series = by_model.split(' or label_replace(')
    assert len(series) == len(cost_prices.priced_models())
    for model in cost_prices.priced_models():
        assert f', "{cost_metrics.MODEL_LABEL}", "{model}", "", "") > 0' in by_model, model


def test_unpriced_usage_is_every_model_outside_the_tables() -> None:
    unpriced = cost_promql.unpriced_request_tokens('1h')
    assert f'sum by ({cost_metrics.MODEL_LABEL}, {cost_metrics.TYPE_LABEL}) (increase(' in unpriced
    [literal] = re.findall(rf'{cost_metrics.MODEL_LABEL}!~(".*?")\}}', unpriced)
    regex = re.compile(_decode_string_literal(literal))
    for model in cost_prices.priced_models():
        assert regex.fullmatch(model), model
    assert not regex.fullmatch(_NOT_IN_TABLE)
    assert literal.count('|') == len(cost_prices.priced_models()) - 1


@pytest.mark.parametrize('model', sorted(cost_prices.LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH))
def test_unpriced_usage_is_also_a_model_priced_by_prompt_length_counted_under_no_card(model: str) -> None:
    # Its rate cards select by prompt length, so tokens naming none (an empty label, to a matcher) or one without a
    # card would otherwise be priced at nothing with the model's row in place.
    unpriced = cost_promql.unpriced_request_tokens('1h')
    [literal] = re.findall(
        rf'{cost_metrics.MODEL_LABEL}="{re.escape(model)}", {cost_metrics.PROMPT_LENGTH_LABEL}!~(".*?")\}}', unpriced
    )
    regex = re.compile(_decode_string_literal(literal))
    for prompt_length in cost_metrics.PROMPT_LENGTHS:
        assert regex.fullmatch(prompt_length), prompt_length
    assert not regex.fullmatch('')
    assert not regex.fullmatch('over200k')
    # Split by prompt length too, so a series says which length has no card.
    key = f'{cost_metrics.MODEL_LABEL}, {cost_metrics.TYPE_LABEL}, {cost_metrics.PROMPT_LENGTH_LABEL}'
    of_model = f'{cost_metrics.REQUEST_TOKENS}{{{cost_metrics.MODEL_LABEL}="{model}", '
    assert f'sum by ({key}) (increase({of_model}' in unpriced


def _selects(matchers: str, series: dict[str, str]) -> bool:
    """Whether a selector's matchers select `series`; a label the series lacks reads as empty, as PromQL has it."""
    for label, operator, literal in _MATCHER.findall(matchers):
        value = series.get(label, '')
        text = _decode_string_literal(f'"{literal}"')
        selected = {
            '=': value == text,
            '!=': value != text,
            '=~': re.fullmatch(text, value) is not None,
            '!~': re.fullmatch(text, value) is None,
        }[operator]
        if not selected:
            return False
    return True


def _selecting(query: str, series: dict[str, str]) -> int:
    """How many of the query's selectors over the request-token counter select `series`."""
    selectors = re.findall(rf'{cost_metrics.REQUEST_TOKENS}\{{(.*?)\}}\[', query)
    assert selectors, query
    return sum(_selects(matchers, series) for matchers in selectors)


@pytest.mark.parametrize('prompt_length', [*cost_metrics.PROMPT_LENGTHS, None, 'over200k'])
@pytest.mark.parametrize('model', [*cost_prices.priced_models(), _NOT_IN_TABLE])
def test_every_series_is_priced_by_one_rate_card_or_reported_unpriced(model: str, prompt_length: str | None) -> None:
    # Never both and never neither: a series two terms select is counted twice, and one no term selects is spend
    # that reaches neither the dashboard nor the alert.
    series = {
        cost_metrics.MODEL_LABEL: model,
        cost_metrics.TYPE_LABEL: 'input',
        cost_metrics.STOP_REASON_LABEL: 'end_turn',
    }
    if prompt_length is not None:
        series[cost_metrics.PROMPT_LENGTH_LABEL] = prompt_length
    priced = _selecting(cost_promql.convert_cents('1h'), series)
    unpriced = _selecting(cost_promql.unpriced_request_tokens('1h'), series)
    assert priced + unpriced == 1, (priced, unpriced)
    # One rate card prices a model's tokens whatever their prompt length; a card of several prices only its own.
    has_a_card = model in cost_prices.LIST_PRICES_CENTS_PER_MTOK or (
        model in cost_prices.LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH
        and prompt_length in cost_prices.LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH[model]
    )
    assert priced == int(has_a_card)


def test_the_unpriced_parts_compare_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # `or` binds looser than a comparison: unparenthesized, `> 0` would apply to the last part alone.
    several = cost_promql.unpriced_request_tokens('1h')
    assert ') or sum by (' in several
    assert several.startswith('(sum by (')
    assert several.endswith(')')
    # The opening parenthesis closes at the very end, so it encloses every part.
    depth = 0
    for position, char in enumerate(several):
        depth += (char == '(') - (char == ')')
        assert depth > 0 or position == len(several) - 1, several[: position + 1]

    monkeypatch.setattr(cost_prices, 'LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH', {})
    one = cost_promql.unpriced_request_tokens('1h')
    assert ' or ' not in one
    assert one.startswith('sum by (')


def test_the_session_components_price_at_the_flat_rates_and_tokens_are_the_remainder() -> None:
    runtime = cost_promql.session_runtime_cents('1h', freshness_minutes=_FRESHNESS)
    search = cost_promql.session_search_cents('1h', freshness_minutes=_FRESHNESS)
    assert runtime.endswith(f'/ 3600 * {cost_prices.SESSION_RUNTIME_CENTS_PER_HOUR}')
    assert search.endswith(f'* {cost_prices.WEB_SEARCH_CENTS_PER_REQUEST}')
    tokens = cost_promql.session_token_cents('1h', freshness_minutes=_FRESHNESS)
    sessions = cost_promql.session_cents('1h', freshness_minutes=_FRESHNESS)
    assert tokens == f'({sessions} - {runtime} - {search})'


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
    monkeypatch.setattr(cost_prices, 'LIST_PRICES_CENTS_PER_MTOK_BY_PROMPT_LENGTH', {})
    by_model = cost_promql.convert_cents_by_model('1h')
    assert by_model.startswith('label_replace(')
    assert by_model.endswith('"", "") > 0')
    assert cost_promql.dollars(by_model) == f'({by_model}) / 100'
