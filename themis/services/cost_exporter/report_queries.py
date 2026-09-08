"""The report's queries as the program renders them into the Job's environment: a fixed, flat shape, parsed strictly.

The Pulumi program (`infra/themis_infra/cost_report.py`, `report_json`) is the one source of the PromQL; this side
knows only the names — which producer figure each query is, and whether it is read at an instant over the day or as
an hourly range for the chart — and fails the run at startup, naming the path, on a key missing or unexpected, so the
two sides cannot drift silently. Every query yields cents.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence


@dataclasses.dataclass(frozen=True)
class DayQueries:
    """PromQL for the trailing day's figures, each evaluated at one instant.

    Attributes:
        sessions: Managed Agents list cost, the provider's own figure.
        sessions_runtime: Its runtime share, at the flat hourly rate.
        sessions_search: Its web-search share, at the flat per-request rate.
        sessions_tokens: Its token share, what the two flat-rate shares leave of it.
        convert: The convert worker's spend, derived from the price table.
        ci: Claude Code's spend in CI, its own figure.
    """

    sessions: str
    sessions_runtime: str
    sessions_search: str
    sessions_tokens: str
    convert: str
    ci: str


@dataclasses.dataclass(frozen=True)
class HourlyQueries:
    """PromQL for the trailing hour's figure per producer, evaluated as a range at hourly steps.

    Attributes:
        sessions: Managed Agents list cost.
        convert: The convert worker's derived spend.
        ci: Claude Code's spend in CI.
    """

    sessions: str
    convert: str
    ci: str


@dataclasses.dataclass(frozen=True)
class Queries:
    """The report's queries: the day's figures and the chart's series."""

    day: DayQueries
    hourly: HourlyQueries


DAY_KEYS = tuple(field.name for field in dataclasses.fields(DayQueries))
"""The keys of the rendering's `day` object, in the report's order."""
HOURLY_KEYS = tuple(field.name for field in dataclasses.fields(HourlyQueries))
"""The keys of the rendering's `hourly` object, in the report's order."""
_ROOT_KEYS = ('day', 'hourly')


def parse(text: str) -> Queries:
    """The queries from the rendered JSON.

    Raises:
        ValueError: The text is not JSON, or some part of it is not the shape the program renders — a key missing or
            unexpected, a query not a non-empty string. The message names the path.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f'queries: not JSON: {e}') from e
    root = _object(document, 'queries', _ROOT_KEYS)
    return Queries(
        day=DayQueries(**_strings(root['day'], 'queries.day', DAY_KEYS)),
        hourly=HourlyQueries(**_strings(root['hourly'], 'queries.hourly', HOURLY_KEYS)),
    )


def _object(value: object, path: str, keys: Sequence[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f'{path}: expected an object, got {type(value).__name__}')
    missing = set(keys) - value.keys()
    if missing:
        raise ValueError(f'{path}: missing {sorted(missing)}')
    unexpected = value.keys() - set(keys)
    if unexpected:
        raise ValueError(f'{path}: unexpected {sorted(unexpected)}')
    return value


def _strings(value: object, path: str, keys: Sequence[str]) -> dict[str, str]:
    record = _object(value, path, keys)
    strings = {}
    for key in keys:
        query = record[key]
        if not isinstance(query, str) or not query:
            raise ValueError(f'{path}.{key}: expected a non-empty string, got {query!r}')
        strings[key] = query
    return strings
