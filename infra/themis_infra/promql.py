"""PromQL selectors and matchers, quoted as the Telemetry API's series need.

Every producer writes through the Telemetry API, so each metric is a native Prometheus-style series on the
`prometheus_target` resource, selected by its bare name. A name or label classic PromQL cannot parse — an OpenTelemetry
semantic-convention name such as `rpc.server.call.duration`, or Claude Code's dotted names — takes the UTF-8 form
Monitoring documents for OTLP-ingested series: the name moves inside the braces, quoted, and a label is quoted where it
is matched.

Monitoring also bounds how often a PromQL condition may be evaluated by how far back its query reads
(`evaluation_floor`), and refuses a retest window once that reaches past `RETEST_LOOKBACK_LIMIT`.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable

_LEGACY_NAME = re.compile(r'^[A-Za-z_:][A-Za-z0-9_:]*$')
# RE2's metacharacters, escaped before the value is escaped again as a string.
_REGEX_METACHARACTER = re.compile(r'([.^$*+?()\[\]{}|\\])')
# What a PromQL double-quoted string escapes.
_STRING_ESCAPE = re.compile(r'([\\"])')
_DURATION = re.compile(r'(\d+)(ms|s|m|h|d|w|y)')
_DURATION_UNITS = {
    'ms': datetime.timedelta(milliseconds=1),
    's': datetime.timedelta(seconds=1),
    'm': datetime.timedelta(minutes=1),
    'h': datetime.timedelta(hours=1),
    'd': datetime.timedelta(days=1),
    'w': datetime.timedelta(weeks=1),
    'y': datetime.timedelta(days=365),
}
# Monitoring's shortest evaluation interval for a query reading back up to each lookback
# (https://cloud.google.com/monitoring/promql/promql-in-alerting); past the last, an hour or more.
_EVALUATION_FLOORS = (
    (datetime.timedelta(hours=25), datetime.timedelta(seconds=30)),
    (datetime.timedelta(days=8), datetime.timedelta(minutes=5)),
)
# A query reading back further than this takes no retest window: its condition's duration is zero.
RETEST_LOOKBACK_LIMIT = _EVALUATION_FLOORS[0][0]


def selector(name: str, *matchers: str) -> str:
    """The series of a metric, narrowed by label matchers built here (`equals`, `among`, `not_among`)."""
    if _LEGACY_NAME.match(name):
        return name + ('{' + ', '.join(matchers) + '}' if matchers else '')
    return '{' + ', '.join((_string_literal(name), *matchers)) + '}'


def equals(label: str, value: str) -> str:
    """A matcher for one label value."""
    return f'{_label(label)}={_string_literal(value)}'


def among(label: str, values: Iterable[str]) -> str:
    """A matcher for a label value inside a fixed set: an anchored RE2 alternation of the values."""
    return f'{_label(label)}=~{_alternation(values)}'


def not_among(label: str, values: Iterable[str]) -> str:
    """A matcher for a label value outside a fixed set: an anchored RE2 alternation of the values.

    Two layers of escaping, RE2's first and the string's second, so a value's metacharacter reaches RE2 escaped and
    a backslash or quote in it survives the string.
    """
    return f'{_label(label)}!~{_alternation(values)}'


def _alternation(values: Iterable[str]) -> str:
    alternatives = '|'.join(_REGEX_METACHARACTER.sub(r'\\\1', value) for value in sorted(values))
    return _string_literal(f'^({alternatives})$')


def _label(label: str) -> str:
    return label if _LEGACY_NAME.match(label) else _string_literal(label)


def _string_literal(value: str) -> str:
    """`value` as a PromQL double-quoted string."""
    return '"' + _STRING_ESCAPE.sub(r'\\\1', value) + '"'


def duration(text: str) -> datetime.timedelta:
    """A PromQL duration (`5m`, `180h`, `1h30m`) as a timedelta.

    Raises:
        ValueError: `text` is not a PromQL duration.
    """
    if not text or _DURATION.sub('', text):
        raise ValueError(f'not a PromQL duration: {text!r}')
    return sum((int(count) * _DURATION_UNITS[unit] for count, unit in _DURATION.findall(text)), datetime.timedelta())


def evaluation_floor(lookback: datetime.timedelta) -> datetime.timedelta:
    """The shortest evaluation interval Monitoring accepts for a condition whose query reads `lookback` back.

    Raises:
        ValueError: `lookback` sits on the edge of a band, which Monitoring does not say which side it falls on, or
            past eight days, where Monitoring evaluates hourly at best.
    """
    for edge, floor in _EVALUATION_FLOORS:
        if lookback == edge:
            raise ValueError(f'a {lookback} lookback is on the edge of an evaluation band; keep inside one')
        if lookback < edge:
            return floor
    raise ValueError(f'a {lookback} lookback is past {_EVALUATION_FLOORS[-1][0]}, which evaluates hourly at best')
