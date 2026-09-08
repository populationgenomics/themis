"""PromQL over the spend metrics, as the dashboard, the alert policies and the report phrase it.

Every producer writes through the Telemetry API, so each metric is a native Prometheus-style series on the
`prometheus_target` resource, selected by its bare name; Claude Code's two names carry dots, which classic PromQL
cannot parse, so those take the UTF-8 form `{"name", …}`. A counter's window figure is `increase`, which is exact on
this backend: a cumulative counter carries its start time. A running total's is the exact rise between two samples,
the newest and the newest a window earlier; `delta` is never used, since it extrapolates to the window's edges — on a
young series by up to a third, and with a sawtooth as the extrapolated stretch grows between samples. A series younger
than the window falls back to its rise since its first sample in the window (`min_over_time`, the first sample of a
monotone series). The one behavioural difference: during a series' first window a session deletion reads as the rise
since the lowest point rather than as a negative step.

Every read of a running total — each end of a rise, and a current value — takes the newest sample within one
tolerance: the freshness window the alerts page the exporter on, threaded in as `freshness_minutes` so the reads'
tolerance and the alert's are one stack value. Within it a stale exporter yields its last known total, so a rise is
measured to the last known point; beyond it the figure is absent — never zero — and the freshness alert is what names
the fault. A window has to exceed the tolerance, or a rise's two ends could read one sample.

Every figure is in the producers' unit, cents, until `dollars` divides at display; nothing here is stored. The derived
figures, each a builder below: a session's list cost is the provider's (`session_cents`), split into runtime and web
search at the flat rates with tokens as the remainder; the convert worker's spend is its token counter priced by the
program's table (`cost_prices`), each model's four token types at their list price as literal multipliers
(`convert_cents`); Claude Code's is its own USD figure scaled (`ci_cents`). Wherever figures are summed each term that
can be empty is guarded to zero (`total`, `guarded`), since a binary operator with an empty side yields nothing.
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
# A PromQL duration of one unit, as the windows here are written.
_DURATION = re.compile(r'^(\d+)([smhdw])$')
_UNIT_SECONDS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86_400, 'w': 604_800}
_CENTS_PER_DOLLAR = 100
_TOKENS_PER_MTOK = 1_000_000
_SECONDS_PER_HOUR = 3600
# Operators that bind looser than `/`: an expression with one at the top level needs parentheses before `/ 100`.
_LOOSER_THAN_DIVISION = frozenset({'+', '-', '==', '!=', '<', '<=', '>', '>=', 'and', 'or', 'unless'})


class Kind(enum.Enum):
    """How a metric is written, which fixes its window figure."""

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


def _require_kind(name: str, expected: Kind) -> None:
    if kind(name) is not expected:
        raise ValueError(f'{name} is a {kind(name).value}, not a {expected.value}')


def seconds(duration: str) -> int:
    """A PromQL duration of one unit (`30m`, `1h`, `7d`) in seconds.

    Raises:
        ValueError: Not a duration of one unit.
    """
    match = _DURATION.fullmatch(duration)
    if match is None:
        raise ValueError(f'not a PromQL duration of one unit: {duration!r}')
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


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


def _aggregate(operator: str, expression: str, by: Sequence[str] = ()) -> str:
    if by:
        return f'{operator} by ({", ".join(by)}) ({expression})'
    return f'{operator}({expression})'


def _current(selected: str, freshness_minutes: int) -> str:
    return f'last_over_time({selected}[{freshness_minutes}m])'


def latest(name: str, *matchers: str, freshness_minutes: int) -> str:
    """The newest value of each selected gauge series, the newest sample within the freshness window.

    Raises:
        ValueError: `name` is a counter, whose current value is a total since its process started, not a figure.
    """
    _require_kind(name, Kind.GAUGE)
    return _current(selector(name, *matchers), freshness_minutes)


def current_total(name: str, *matchers: str, by: Sequence[str] = (), freshness_minutes: int) -> str:
    """The summed newest value of a gauge, one series per `by` combination."""
    return _aggregate('sum', latest(name, *matchers, freshness_minutes=freshness_minutes), by)


def rise(name: str, duration: str, *matchers: str, by: Sequence[str] = (), freshness_minutes: int) -> str:
    """The summed exact rise of a running total over the trailing `duration`, one series per `by` combination.

    Per series, the newest sample less the newest sample a window earlier, each the newest within the freshness
    window (both sides carry the series' labels, so they match one to one); for a series with no sample a window
    earlier, its rise since its first sample in the window. Parenthesized so `or` joins the two forms and the
    aggregation wraps the whole.

    Raises:
        ValueError: `name` is a counter, or `duration` does not exceed the freshness window, so the two ends could
            read one sample.
    """
    _require_kind(name, Kind.GAUGE)
    if seconds(duration) <= freshness_minutes * 60:
        raise ValueError(
            f'a {duration} window does not exceed the {freshness_minutes}-minute freshness window; a rise over it '
            f'could read one sample at both ends'
        )
    selected = selector(name, *matchers)
    current = _current(selected, freshness_minutes)
    a_window_earlier = f'last_over_time({selected}[{freshness_minutes}m] offset {duration})'
    first_in_window = f'min_over_time({selected}[{duration}])'
    return _aggregate('sum', f'(({current} - {a_window_earlier}) or ({current} - {first_in_window}))', by)


def increase(name: str, duration: str, *matchers: str, by: Sequence[str] = ()) -> str:
    """The summed increase of a counter over the trailing `duration`, one series per `by` combination.

    Raises:
        ValueError: `name` is a gauge; a running total's window figure is `rise`.
    """
    _require_kind(name, Kind.COUNTER)
    return _aggregate('sum', f'increase({selector(name, *matchers)}[{duration}])', by)


def silence(name: str, duration: str) -> str:
    """One element when no series of the metric has a sample in the trailing `duration`, else nothing."""
    return f'absent_over_time({selector(name)}[{duration}])'


def session_cents(duration: str, *matchers: str, by: Sequence[str] = (), freshness_minutes: int) -> str:
    """Managed Agents list cost over `duration`, in cents: the provider's own cumulative figure, differenced."""
    return rise(cost_metrics.SESSION_LIST_COST_CENTS, duration, *matchers, by=by, freshness_minutes=freshness_minutes)


def session_runtime_cents(duration: str, *matchers: str, by: Sequence[str] = (), freshness_minutes: int) -> str:
    """The runtime share of session list cost over `duration`: active seconds at the flat hourly rate."""
    active = rise(cost_metrics.SESSION_ACTIVE_SECONDS, duration, *matchers, by=by, freshness_minutes=freshness_minutes)
    return f'{active} / {_SECONDS_PER_HOUR} * {cost_prices.SESSION_RUNTIME_CENTS_PER_HOUR}'


def session_search_cents(duration: str, *matchers: str, by: Sequence[str] = (), freshness_minutes: int) -> str:
    """The web-search share of session list cost over `duration`: requests at the flat per-request rate."""
    requests = rise(
        cost_metrics.SESSION_WEB_SEARCH_REQUESTS, duration, *matchers, by=by, freshness_minutes=freshness_minutes
    )
    return f'{requests} * {cost_prices.WEB_SEARCH_CENTS_PER_REQUEST}'


def session_token_cents(duration: str, *matchers: str, by: Sequence[str] = (), freshness_minutes: int) -> str:
    """The token share of session list cost over `duration`: what the two flat-rate components leave of it.

    The four gauges are written together each run, so their windows hold the same samples and the difference
    lines up; the result is parenthesized, so a caller can scale it.
    """
    return (
        f'({session_cents(duration, *matchers, by=by, freshness_minutes=freshness_minutes)}'
        f' - {session_runtime_cents(duration, *matchers, by=by, freshness_minutes=freshness_minutes)}'
        f' - {session_search_cents(duration, *matchers, by=by, freshness_minutes=freshness_minutes)})'
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
    """One model's convert spend over `duration`, in cents: each token type's increase at its list price, summed."""
    of_model = equals(cost_metrics.MODEL_LABEL, model)
    priced = [
        f'{increase(cost_metrics.REQUEST_TOKENS, duration, of_model, equals(cost_metrics.TYPE_LABEL, token_type))}'
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
    return increase(cost_metrics.REQUEST_TOKENS, duration, outside_table, by=key)


def ci_dollars(duration: str, *, by: Sequence[str] = ()) -> str:
    """Claude Code's CI spend over `duration`, in USD: its own figure, from its own price table."""
    return increase(claude_code_metrics.COST_USAGE, duration, by=by)


def ci_cents(duration: str) -> str:
    """Claude Code's CI spend over `duration`, in cents, for summing with the other producers'."""
    return f'{ci_dollars(duration)} * {_CENTS_PER_DOLLAR}'


def workspace_cents(duration: str, *, freshness_minutes: int) -> str:
    """Every producer's spend over `duration` as one figure, in cents.

    The sessions and CI figures are guarded, since either can be empty; the convert figure is never empty and adds
    bare.
    """
    sessions = session_cents(duration, freshness_minutes=freshness_minutes)
    return summed([guarded(sessions), convert_cents(duration), guarded(ci_cents(duration))])


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
