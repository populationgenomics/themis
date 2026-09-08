"""PromQL over the spend metrics, as the dashboard and the alert policies phrase it.

Every producer writes through the Telemetry API, so each metric is a native Prometheus-style series on the
`prometheus_target` resource, selected by its bare name; Claude Code's two names carry dots, which classic PromQL
cannot parse, so those take the UTF-8 form `{"name", …}`. Monitoring's PromQL is typed — `rate` and `increase` are
refused on a gauge — so a running total's window figure is `delta`, which extrapolates to the window's edges and can
differ from two points' difference by a sample interval's slope, and a counter's is `increase`. Every figure is in
the producers' unit, cents, until `dollars` divides at display; nothing here is stored.

The derived figures, each a builder below: a session's list cost is the provider's (`session_cents`), split into
runtime and web search at the flat rates with tokens as the remainder; the convert worker's spend is its token counter
priced by the program's table (`cost_prices`), each model's four token types at their list price as literal
multipliers (`convert_cents`); Claude Code's is its own USD figure scaled (`ci_cents`). Wherever figures are summed
each term is guarded to zero (`total`), since a binary operator with an empty side yields nothing.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Sequence

from themis_infra import claude_code_metrics, cost_metrics, cost_prices

_LEGACY_NAME = re.compile(r'^[A-Za-z_:][A-Za-z0-9_:]*$')
# RE2's metacharacters, escaped before the value is escaped again as a string.
_REGEX_METACHARACTER = re.compile(r'([.^$*+?()\[\]{}|\\])')
# What a PromQL double-quoted string escapes.
_STRING_ESCAPE = re.compile(r'([\\"])')
_CENTS_PER_DOLLAR = 100
_TOKENS_PER_MTOK = 1_000_000
_SECONDS_PER_HOUR = 3600
# An instant read of a gauge takes the newest sample within this many of the writer's ticks: one missed tick
# (scheduler jitter, a failed run) must not read as a total vanishing.
_LOOKBACK_TICKS = 3
# Operators that bind looser than `/`: an expression with one at the top level needs parentheses before `/ 100`.
_LOOSER_THAN_DIVISION = frozenset({'+', '-', '==', '!=', '<', '<=', '>', '>=', 'and', 'or', 'unless'})


class Kind(enum.Enum):
    """How a metric is written, which fixes its window function."""

    GAUGE = 'gauge'
    COUNTER = 'counter'


_KINDS: dict[str, Kind] = {
    cost_metrics.SESSION_LIST_COST_CENTS: Kind.GAUGE,
    cost_metrics.SESSION_TOKENS: Kind.GAUGE,
    cost_metrics.SESSION_ACTIVE_SECONDS: Kind.GAUGE,
    cost_metrics.SESSION_WEB_SEARCH_REQUESTS: Kind.GAUGE,
    cost_metrics.EXPORTER_LAST_SUCCESS_TIMESTAMP_SECONDS: Kind.GAUGE,
    cost_metrics.REQUEST_TOKENS: Kind.COUNTER,
    claude_code_metrics.TOKEN_USAGE: Kind.COUNTER,
    claude_code_metrics.COST_USAGE: Kind.COUNTER,
}
_WINDOW_FUNCTION = {Kind.GAUGE: 'delta', Kind.COUNTER: 'increase'}

METRICS = frozenset(_KINDS)
"""Every metric the queries read."""


def kind(name: str) -> Kind:
    """How the named metric is written.

    Raises:
        KeyError: `name` is not one of the spend metrics.
    """
    try:
        return _KINDS[name]
    except KeyError as e:
        raise KeyError(f'not a spend metric: {name}') from e


def selector(name: str, *matchers: str) -> str:
    """The series of a spend metric, narrowed by label matchers (`agent="x"`, or a dashboard template variable).

    A name classic PromQL cannot parse — Claude Code's dotted names — moves inside the braces, quoted: the UTF-8
    form Monitoring documents for OTLP-ingested names.

    Raises:
        KeyError: `name` is not one of the spend metrics.
    """
    kind(name)
    if _LEGACY_NAME.match(name):
        return name + ('{' + ', '.join(matchers) + '}' if matchers else '')
    return '{' + ', '.join((f'"{name}"', *matchers)) + '}'


def _string_literal(value: str) -> str:
    """`value` as a PromQL double-quoted string."""
    return '"' + _STRING_ESCAPE.sub(r'\\\1', value) + '"'


def equals(label: str, value: str) -> str:
    """A matcher for one label value."""
    return f'{label}={_string_literal(value)}'


def not_among(label: str, values: Iterable[str]) -> str:
    """A matcher for a label value outside a fixed set: an anchored RE2 alternation of the values.

    Two layers of escaping, RE2's first and the string's second, so a value's metacharacter reaches RE2 escaped and
    a backslash or quote in it survives the string.
    """
    alternatives = '|'.join(_REGEX_METACHARACTER.sub(r'\\\1', value) for value in sorted(values))
    return f'{label}!~{_string_literal(f"^({alternatives})$")}'


def window(name: str, duration: str, *matchers: str) -> str:
    """How much each selected series grew over the trailing `duration`, in the metric's own unit.

    `delta` of a gauge of a running total, `increase` of a counter: the one function Monitoring's typed PromQL
    accepts for each.
    """
    return f'{_WINDOW_FUNCTION[kind(name)]}({selector(name, *matchers)}[{duration}])'


def latest(name: str, *matchers: str, tick_minutes: int) -> str:
    """The newest value of each selected gauge series, read within a few ticks of the writer's schedule.

    Raises:
        ValueError: `name` is a counter, whose current value is a total since its process started, not a figure.
    """
    if kind(name) is not Kind.GAUGE:
        raise ValueError(f'{name} is a counter; only a gauge has a current value')
    return f'last_over_time({selector(name, *matchers)}[{_LOOKBACK_TICKS * tick_minutes}m])'


def silence(name: str, duration: str) -> str:
    """One element when no series of the metric has a sample in the trailing `duration`, else nothing."""
    return f'absent_over_time({selector(name)}[{duration}])'


def _aggregate(operator: str, expression: str, by: Sequence[str] = ()) -> str:
    if by:
        return f'{operator} by ({", ".join(by)}) ({expression})'
    return f'{operator}({expression})'


def growth(name: str, duration: str, *matchers: str, by: Sequence[str] = ()) -> str:
    """The summed window figure of a metric over the trailing `duration`, one series per `by` combination."""
    return _aggregate('sum', window(name, duration, *matchers), by)


def current_total(name: str, *matchers: str, by: Sequence[str] = (), tick_minutes: int) -> str:
    """The summed newest value of a gauge, one series per `by` combination."""
    return _aggregate('sum', latest(name, *matchers, tick_minutes=tick_minutes), by)


def session_cents(duration: str, *matchers: str, by: Sequence[str] = ()) -> str:
    """Managed Agents list cost over `duration`, in cents: the provider's own cumulative figure, differenced."""
    return growth(cost_metrics.SESSION_LIST_COST_CENTS, duration, *matchers, by=by)


def session_runtime_cents(duration: str, *matchers: str, by: Sequence[str] = ()) -> str:
    """The runtime share of session list cost over `duration`: active seconds at the flat hourly rate."""
    seconds = growth(cost_metrics.SESSION_ACTIVE_SECONDS, duration, *matchers, by=by)
    return f'{seconds} / {_SECONDS_PER_HOUR} * {cost_prices.SESSION_RUNTIME_CENTS_PER_HOUR}'


def session_search_cents(duration: str, *matchers: str, by: Sequence[str] = ()) -> str:
    """The web-search share of session list cost over `duration`: requests at the flat per-request rate."""
    requests = growth(cost_metrics.SESSION_WEB_SEARCH_REQUESTS, duration, *matchers, by=by)
    return f'{requests} * {cost_prices.WEB_SEARCH_CENTS_PER_REQUEST}'


def session_token_cents(duration: str, *matchers: str, by: Sequence[str] = ()) -> str:
    """The token share of session list cost over `duration`: what the two flat-rate components leave of it.

    The four gauges are written together each run, so their windows hold the same samples and the difference
    lines up; the result is parenthesized, so a caller can scale it.
    """
    return (
        f'({session_cents(duration, *matchers, by=by)}'
        f' - {session_runtime_cents(duration, *matchers, by=by)}'
        f' - {session_search_cents(duration, *matchers, by=by)})'
    )


def guarded(term: str) -> str:
    """`term`, or zero where it is empty: what a sum needs of an operand that can be an empty vector."""
    return f'({term} or vector(0))'


def summed(terms: Sequence[str]) -> str:
    """The sum of figures none of which can be empty, parenthesized; a single term stays bare.

    Raises:
        ValueError: No terms.
    """
    if not terms:
        raise ValueError('a sum needs at least one term')
    if len(terms) == 1:
        return terms[0]
    return '(' + ' + '.join(terms) + ')'


def total(terms: Sequence[str]) -> str:
    """The sum of several figures any of which can be empty, as one.

    A sum over no series is an empty vector, and a binary operator with an empty side yields nothing, so with more
    than one term each is guarded to zero; a single term stays bare, so a figure with no data reads as no data.

    Raises:
        ValueError: No terms.
    """
    if len(terms) == 1:
        return terms[0]
    return summed([guarded(term) for term in terms])


def _model_cents(model: str, duration: str) -> str:
    """One model's convert spend over `duration`, in cents: each token type's growth at its list price, summed."""
    of_model = equals(cost_metrics.MODEL_LABEL, model)
    priced = [
        f'{growth(cost_metrics.REQUEST_TOKENS, duration, of_model, equals(cost_metrics.TYPE_LABEL, token_type))}'
        f' * {cents}'
        for token_type, cents in cost_prices.LIST_PRICES_CENTS_PER_MTOK[model].items()
    ]
    return f'{total(priced)} / {_TOKENS_PER_MTOK}'


def convert_cents(duration: str) -> str:
    """The convert worker's direct-call spend over `duration`, in cents, over every model the price table prices.

    A model's figure is a sum of guarded terms and so never empty; the models add bare. A model the table lacks
    contributes nothing — priced at nothing — which `unpriced_request_tokens` watches for.
    """
    return summed([_model_cents(model, duration) for model in sorted(cost_prices.LIST_PRICES_CENTS_PER_MTOK)])


def convert_cents_by_model(duration: str) -> str:
    """The convert worker's spend over `duration` as one series per model with usage, carrying the model label.

    Each model's figure is a label-less sum, so the label is put back with `label_replace` and the models joined
    with `or`, which unions series of distinct label sets. `increase` is never negative, so `> 0` drops a model
    with no usage in the window rather than drawing it at zero.
    """
    return ' or '.join(
        f'label_replace({_model_cents(model, duration)}, "{cost_metrics.MODEL_LABEL}", "{model}", "", "") > 0'
        for model in sorted(cost_prices.LIST_PRICES_CENTS_PER_MTOK)
    )


def unpriced_request_tokens(duration: str) -> str:
    """Request tokens over `duration`, by model and type, for every model the price table has no row for."""
    outside_table = not_among(cost_metrics.MODEL_LABEL, cost_prices.LIST_PRICES_CENTS_PER_MTOK)
    key = (cost_metrics.MODEL_LABEL, cost_metrics.TYPE_LABEL)
    return growth(cost_metrics.REQUEST_TOKENS, duration, outside_table, by=key)


def ci_dollars(duration: str, *, by: Sequence[str] = ()) -> str:
    """Claude Code's CI spend over `duration`, in USD: its own figure, from its own price table."""
    return growth(claude_code_metrics.COST_USAGE, duration, by=by)


def ci_cents(duration: str) -> str:
    """Claude Code's CI spend over `duration`, in cents, for summing with the other producers'."""
    return f'{ci_dollars(duration)} * {_CENTS_PER_DOLLAR}'


def workspace_cents(duration: str) -> str:
    """Every producer's spend over `duration` as one figure, in cents.

    The sessions and CI figures are guarded, since either can be empty; the convert figure is never empty and adds
    bare.
    """
    return summed([guarded(session_cents(duration)), convert_cents(duration), guarded(ci_cents(duration))])


def _has_loose_top_level_operator(expression: str) -> bool:
    depth = 0
    for token in expression.split(' '):
        if depth == 0 and token in _LOOSER_THAN_DIVISION:
            return True
        depth += token.count('(') - token.count(')')
    return False


def dollars(cents: str) -> str:
    """A cents figure in dollars, for display; the metrics hold cents."""
    if _has_loose_top_level_operator(cents):
        cents = f'({cents})'
    return f'{cents} / {_CENTS_PER_DOLLAR}'
