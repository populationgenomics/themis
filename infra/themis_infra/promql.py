"""PromQL selectors and matchers, quoted as the Telemetry API's series need.

Every producer writes through the Telemetry API, so each metric is a native Prometheus-style series on the
`prometheus_target` resource, selected by its bare name. A name or label classic PromQL cannot parse — an OpenTelemetry
semantic-convention name such as `rpc.server.call.duration`, or Claude Code's dotted names — takes the UTF-8 form
Monitoring documents for OTLP-ingested series: the name moves inside the braces, quoted, and a label is quoted where it
is matched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_LEGACY_NAME = re.compile(r'^[A-Za-z_:][A-Za-z0-9_:]*$')
# RE2's metacharacters, escaped before the value is escaped again as a string.
_REGEX_METACHARACTER = re.compile(r'([.^$*+?()\[\]{}|\\])')
# What a PromQL double-quoted string escapes.
_STRING_ESCAPE = re.compile(r'([\\"])')


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
