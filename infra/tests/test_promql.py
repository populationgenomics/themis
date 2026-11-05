"""A name or label classic PromQL cannot parse takes the quoted form; a classic one stays bare.

Also: a lookback maps to the evaluation interval Monitoring accepts for it, and one on a band's edge is refused.
"""

from __future__ import annotations

import datetime

import pytest

from themis_infra import promql


def test_a_dotted_name_moves_inside_the_braces_quoted() -> None:
    assert promql.selector('rpc.server.call.duration_count') == '{"rpc.server.call.duration_count"}'
    assert promql.selector('themis_x', promql.equals('reason', 'invalid')) == 'themis_x{reason="invalid"}'


def test_a_dotted_label_is_quoted_where_it_is_matched() -> None:
    matcher = promql.among('rpc.response.status_code', ['UNKNOWN', 'INTERNAL'])
    assert matcher == '"rpc.response.status_code"=~"^(INTERNAL|UNKNOWN)$"'
    assert promql.selector('rpc.server.call.duration_count', matcher) == (
        '{"rpc.server.call.duration_count", "rpc.response.status_code"=~"^(INTERNAL|UNKNOWN)$"}'
    )


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('5m', datetime.timedelta(minutes=5)),
        ('180h', datetime.timedelta(hours=180)),
        ('1h30m', datetime.timedelta(minutes=90)),
        ('300s', datetime.timedelta(seconds=300)),
    ],
)
def test_a_duration_reads_as_promql_writes_it(text: str, expected: datetime.timedelta) -> None:
    assert promql.duration(text) == expected


@pytest.mark.parametrize('text', ['', '5', 'm', '5 m', '5x', '-5m'])
def test_a_malformed_duration_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match='not a PromQL duration'):
        promql.duration(text)


@pytest.mark.parametrize(
    ('lookback', 'floor'),
    [
        (datetime.timedelta(minutes=5), datetime.timedelta(seconds=30)),
        (datetime.timedelta(hours=24, minutes=59), datetime.timedelta(seconds=30)),
        (datetime.timedelta(hours=25, minutes=1), datetime.timedelta(minutes=5)),
        (datetime.timedelta(hours=180), datetime.timedelta(minutes=5)),
    ],
)
def test_the_evaluation_floor_follows_the_lookbacks_band(
    lookback: datetime.timedelta, floor: datetime.timedelta
) -> None:
    assert promql.evaluation_floor(lookback) == floor


@pytest.mark.parametrize('lookback', [datetime.timedelta(hours=25), datetime.timedelta(days=8)])
def test_a_lookback_on_a_bands_edge_is_refused(lookback: datetime.timedelta) -> None:
    with pytest.raises(ValueError, match='edge'):
        promql.evaluation_floor(lookback)


def test_a_lookback_past_eight_days_is_refused() -> None:
    with pytest.raises(ValueError, match='hourly'):
        promql.evaluation_floor(datetime.timedelta(days=30))
