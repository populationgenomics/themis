"""PromQL against Cloud Monitoring: the Prometheus HTTP API over the project's metrics, each query one figure.

Monitoring serves its metrics through the Prometheus query API, so the report runs the queries the infrastructure
renders for it (`infra/themis_infra/cost_report.py`) rather than re-deriving a figure from raw points. Every query
is an aggregate with no label left, so its result is one series or none: none is nothing to read — no producer
wrote in the window — and two or more is a fault in the query, which fails the read. The API returns every value as
a decimal string; a value that is not a finite number fails the read the same way.
"""

from __future__ import annotations

import abc
import datetime
import json
import math
from collections.abc import Mapping, Sequence
from typing import NamedTuple, Protocol, override

import requests

_API = 'https://monitoring.googleapis.com/v1/projects/{project}/location/global/prometheus/api/v1'
# Per request; the run's deadline is checked between requests, and the Job's timeout backstops a request that hangs.
_REQUEST_TIMEOUT_SECONDS = 30.0


class QueryError(ValueError):
    """The endpoint did not answer a query with a result the report can read."""


class Point(NamedTuple):
    """One figure of a range result, stamped with its evaluation instant (UTC)."""

    at: datetime.datetime
    value: float


class PromQL(abc.ABC):
    """PromQL evaluated against the project's metrics; each query yields one series or none."""

    @abc.abstractmethod
    def instant(self, query: str, *, at: datetime.datetime) -> float | None:
        """Evaluate `query` at `at`: the one series' value, or `None` when no series matched."""

    @abc.abstractmethod
    def range(
        self, query: str, *, start: datetime.datetime, end: datetime.datetime, step: datetime.timedelta
    ) -> tuple[Point, ...]:
        """Evaluate `query` every `step` over [`start`, `end`]: the one series' points in time order, or none."""


class Transport(Protocol):
    """The one HTTP call the client makes; `google.auth.transport.requests.AuthorizedSession` satisfies it."""

    def post(self, url: str, *, json: Mapping[str, str], timeout: float) -> requests.Response: ...


class CloudMonitoringPromQL(PromQL):
    """The live client: Monitoring's Prometheus endpoint for the project, `query` and `query_range`.

    Each request's parameters go in a JSON body, the form Google's discovery schema gives the endpoint.
    """

    def __init__(self, transport: Transport, project: str) -> None:
        self._transport = transport
        self._api = _API.format(project=project)

    @override
    def instant(self, query: str, *, at: datetime.datetime) -> float | None:
        return instant_value(self._call('query', {'query': query, 'time': _rfc3339(at)}))

    @override
    def range(
        self, query: str, *, start: datetime.datetime, end: datetime.datetime, step: datetime.timedelta
    ) -> tuple[Point, ...]:
        body = {
            'query': query,
            'start': _rfc3339(start),
            'end': _rfc3339(end),
            'step': f'{int(step.total_seconds())}s',
        }
        return range_points(self._call('query_range', body))

    def _call(self, endpoint: str, body: Mapping[str, str]) -> object:
        response = self._transport.post(f'{self._api}/{endpoint}', json=body, timeout=_REQUEST_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise QueryError(f'{endpoint} answered HTTP {response.status_code}: {response.text}')
        try:
            return json.loads(response.text)
        except json.JSONDecodeError as e:
            raise QueryError(f'{endpoint} answered with a body that is not JSON: {response.text}') from e


def instant_value(body: object) -> float | None:
    """The one series' value in an instant query's response body, or `None` for an empty result.

    Raises:
        QueryError: The query did not succeed, the result is not a vector, it holds more than one series, or the
            value is not a finite number.
    """
    result = _only_result(body, expected_type='vector')
    if result is None:
        return None
    return _point(_field(result, 'value'), result).value


def range_points(body: object) -> tuple[Point, ...]:
    """The one series' points in a range query's response body, in time order; empty for an empty result.

    Raises:
        QueryError: The query did not succeed, the result is not a matrix, it holds more than one series, or a
            value is not a finite number.
    """
    result = _only_result(body, expected_type='matrix')
    if result is None:
        return ()
    values = _field(result, 'values')
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise QueryError(f'result values are not a list: {result!r}')
    return tuple(_point(point, result) for point in values)


def _rfc3339(at: datetime.datetime) -> str:
    if at.tzinfo is None:
        raise ValueError('precondition failed: an evaluation instant must be timezone-aware')
    return at.astimezone(datetime.UTC).isoformat()


def _only_result(body: object, *, expected_type: str) -> Mapping[str, object] | None:
    if not isinstance(body, Mapping):
        raise QueryError(f'response is not an object: {body!r}')
    if body.get('status') != 'success':
        raise QueryError(f'query did not succeed: {json.dumps(body)}')
    data = body.get('data')
    if not isinstance(data, Mapping):
        raise QueryError(f'response carries no data object: {json.dumps(body)}')
    if data.get('resultType') != expected_type:
        raise QueryError(f'expected a {expected_type} result, got {data.get("resultType")!r}')
    results = data.get('result')
    if isinstance(results, str) or not isinstance(results, Sequence):
        raise QueryError(f'result is not a list: {json.dumps(body)}')
    if not results:
        return None
    if len(results) > 1:
        raise QueryError(f'expected one series, got {len(results)}: {json.dumps(body)}')
    [result] = results
    if not isinstance(result, Mapping):
        raise QueryError(f'result is not an object: {json.dumps(body)}')
    return result


def _field(result: Mapping[str, object], name: str) -> object:
    if name not in result:
        raise QueryError(f'result carries no {name}: {result!r}')
    return result[name]


def _point(point: object, result: Mapping[str, object]) -> Point:
    """The instant (UTC) and figure of a `[timestamp, "value"]` pair."""
    if isinstance(point, str) or not isinstance(point, Sequence) or len(point) != 2:
        raise QueryError(f'point is not a [timestamp, value] pair: {point!r} in {result!r}')
    at, text = point
    if isinstance(at, bool) or not isinstance(at, int | float):
        raise QueryError(f'timestamp {at!r} is not a number in {result!r}')
    if not isinstance(text, str):
        raise QueryError(f'value {text!r} is not the string the API returns in {result!r}')
    try:
        value = float(text)
    except ValueError as e:
        raise QueryError(f'value {text!r} is not a number in {result!r}') from e
    if not math.isfinite(value):
        raise QueryError(f'value {text!r} is not finite in {result!r}')
    return Point(datetime.datetime.fromtimestamp(at, datetime.UTC), value)
