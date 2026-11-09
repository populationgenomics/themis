"""Reading Prometheus API bodies as one figure, and what the live client sends and fails on.

The bodies are the endpoint's documented shapes; the transport is a fake returning real `requests.Response` objects
or raising real `requests` errors, so the client's handling of status, body and a request with no response is what
is asserted, with no network.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping, Sequence

import pytest
import requests

from themis.services.cost_exporter import promql

_START = datetime.datetime(2026, 9, 6, 22, 0, tzinfo=datetime.UTC)
_END = _START + datetime.timedelta(hours=24)


def _vector(*results: tuple[Mapping[str, str], str]) -> dict[str, object]:
    return {
        'status': 'success',
        'data': {
            'resultType': 'vector',
            'result': [{'metric': metric, 'value': [1757196000.0, value]} for metric, value in results],
        },
    }


def _matrix(*results: Sequence[tuple[float, str]]) -> dict[str, object]:
    return {
        'status': 'success',
        'data': {
            'resultType': 'matrix',
            'result': [{'metric': {}, 'values': [list(point) for point in values]} for values in results],
        },
    }


def test_an_instant_result_reads_as_the_one_series_value() -> None:
    assert promql.instant_value(_vector(({}, '215.5'))) == 215.5
    assert promql.instant_value(_vector(({}, '-100'))) == -100.0


def test_a_range_result_reads_as_the_one_series_points_with_utc_instants() -> None:
    points = promql.range_points(_matrix([(1757196000, '1'), (1757199600, '2.5')]))

    assert [point.value for point in points] == [1.0, 2.5]
    assert all(point.at.tzinfo is datetime.UTC for point in points)
    assert points[1].at - points[0].at == datetime.timedelta(hours=1)


def test_an_empty_result_is_no_figure_not_zero() -> None:
    assert promql.instant_value(_vector()) is None
    assert promql.range_points(_matrix()) == ()


def test_more_than_one_series_is_a_fault_in_the_query() -> None:
    with pytest.raises(promql.QueryError, match='expected one series, got 2'):
        promql.instant_value(_vector(({'agent': 'a'}, '1'), ({'agent': 'b'}, '2')))
    with pytest.raises(promql.QueryError, match='expected one series, got 2'):
        promql.range_points(_matrix([(1757196000, '1')], [(1757196000, '2')]))


@pytest.mark.parametrize(
    ('body', 'complaint'),
    [
        pytest.param(
            {'status': 'error', 'errorType': 'bad_data', 'error': 'parse error at char 3'},
            'parse error at char 3',
            id='query-error-carries-the-body',
        ),
        pytest.param(_vector(({}, 'NaN')), 'not finite', id='nan'),
        pytest.param(_vector(({}, '+Inf')), 'not finite', id='infinite'),
        pytest.param(_vector(({}, 'lots')), 'not a number', id='not-a-number'),
        pytest.param(_matrix(), 'expected a vector result', id='matrix-for-an-instant'),
        pytest.param({'status': 'success'}, 'no data object', id='no-data'),
        pytest.param([], 'not an object', id='not-an-object'),
    ],
)
def test_a_body_the_report_cannot_use_is_a_query_error(body: object, complaint: str) -> None:
    with pytest.raises(promql.QueryError, match=complaint):
        promql.instant_value(body)


def test_a_range_body_with_a_vector_is_a_query_error() -> None:
    with pytest.raises(promql.QueryError, match='expected a matrix result'):
        promql.range_points(_vector())


def _response(status: int, body: object) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    return response


class _Transport:
    """Answers every request with `outcome`: a response to return, or an error to raise."""

    def __init__(self, outcome: requests.Response | Exception) -> None:
        self._outcome = outcome
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def post(self, url: str, *, json: Mapping[str, str], timeout: float) -> requests.Response:
        self.calls.append((url, dict(json), timeout))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def test_the_client_posts_an_instant_query_as_a_json_body_to_the_projects_endpoint() -> None:
    transport = _Transport(_response(200, _vector(({}, '1'))))

    client = promql.CloudMonitoringPromQL(transport, 'themis-test')
    value = client.instant('sum(x)', at=_END)

    assert value == 1.0
    [(url, body, timeout)] = transport.calls
    assert url == 'https://monitoring.googleapis.com/v1/projects/themis-test/location/global/prometheus/api/v1/query'
    assert body == {'query': 'sum(x)', 'time': _END.isoformat()}
    assert 0 < timeout <= 30


def test_the_client_posts_a_range_query_with_rfc3339_bounds_and_a_step_in_seconds() -> None:
    transport = _Transport(_response(200, _matrix()))

    promql.CloudMonitoringPromQL(transport, 'themis-test').range(
        'q', start=_START, end=_END, step=datetime.timedelta(hours=1)
    )

    [(url, body, _)] = transport.calls
    assert url.endswith('/api/v1/query_range')
    assert body['query'] == 'q'
    assert datetime.datetime.fromisoformat(body['start']) == _START
    assert datetime.datetime.fromisoformat(body['end']) == _END
    assert body['step'] == '3600s'


def test_a_naive_bound_fails_before_any_call() -> None:
    transport = _Transport(_response(200, _matrix()))
    with pytest.raises(ValueError, match='timezone-aware'):
        promql.CloudMonitoringPromQL(transport, 'p').range(
            'q', start=_START.replace(tzinfo=None), end=_END, step=datetime.timedelta(hours=1)
        )
    assert transport.calls == []


def test_a_non_200_answer_is_a_query_error_carrying_its_body() -> None:
    transport = _Transport(_response(403, {'error': {'message': 'monitoring.timeSeries.list denied'}}))
    with pytest.raises(promql.QueryError, match=r'HTTP 403.*monitoring\.timeSeries\.list denied'):
        promql.CloudMonitoringPromQL(transport, 'p').instant('q', at=_END)


def test_a_200_that_is_not_json_is_a_query_error() -> None:
    response = requests.Response()
    response.status_code = 200
    response._content = b'<html>gateway</html>'
    with pytest.raises(promql.QueryError, match='not JSON'):
        promql.CloudMonitoringPromQL(_Transport(response), 'p').instant('q', at=_END)


@pytest.mark.parametrize(
    'failure',
    [
        pytest.param(requests.ReadTimeout('read timed out'), id='read-timeout'),
        pytest.param(requests.ConnectTimeout('connect timed out'), id='connect-timeout'),
        pytest.param(requests.ConnectionError('connection reset'), id='connection-lost-before-the-response'),
        pytest.param(requests.exceptions.ChunkedEncodingError('incomplete read'), id='connection-lost-mid-body'),
        pytest.param(requests.exceptions.ContentDecodingError('bad gzip'), id='body-that-does-not-decode'),
    ],
)
def test_a_request_with_no_whole_response_is_unavailable_after_one_request(failure: Exception) -> None:
    transport = _Transport(failure)

    with pytest.raises(promql.UnavailableError, match='got no response') as raised:
        promql.CloudMonitoringPromQL(transport, 'p').instant('q', at=_END)

    assert raised.value.__cause__ is failure
    assert len(transport.calls) == 1


@pytest.mark.parametrize('status', [500, 503])
def test_a_5xx_is_unavailable_carrying_its_body(status: int) -> None:
    transport = _Transport(_response(status, {'status': 'error', 'error': 'backend unavailable'}))
    with pytest.raises(promql.UnavailableError, match=rf'HTTP {status}.*backend unavailable'):
        promql.CloudMonitoringPromQL(transport, 'p').range('q', start=_START, end=_END, step=_END - _START)


@pytest.mark.parametrize(
    'failure',
    [
        pytest.param(requests.exceptions.InvalidURL('no host'), id='a-request-that-cannot-be-sent'),
        pytest.param(requests.TooManyRedirects('30 redirects'), id='a-redirect-loop'),
        pytest.param(RuntimeError('credentials could not be refreshed'), id='not-from-requests'),
    ],
)
def test_an_error_that_is_not_a_missing_response_reaches_the_caller_as_itself(failure: Exception) -> None:
    with pytest.raises(type(failure)) as raised:
        promql.CloudMonitoringPromQL(_Transport(failure), 'p').instant('q', at=_END)
    assert raised.value is failure


@pytest.mark.parametrize(
    'answer',
    [
        pytest.param(_response(400, {'status': 'error', 'error': 'parse error'}), id='bad-query'),
        pytest.param(_response(403, {'error': {'message': 'denied'}}), id='denied'),
        pytest.param(_response(429, {'error': {'message': 'quota exceeded'}}), id='over-quota'),
        pytest.param(_response(200, {'status': 'error', 'error': 'execution error'}), id='200-with-an-error-body'),
    ],
)
def test_a_4xx_or_an_error_body_is_not_unavailable(answer: requests.Response) -> None:
    with pytest.raises(promql.QueryError) as raised:
        promql.CloudMonitoringPromQL(_Transport(answer), 'p').instant('q', at=_END)
    assert not isinstance(raised.value, promql.UnavailableError)
