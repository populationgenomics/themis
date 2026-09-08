"""A run's contract: the day read at the window's end; one post naming the total and every producer, or none.

The query source and the channel are in-memory fakes; the Prometheus and Slack adapters have their own tests.
"""

from __future__ import annotations

import datetime
import json
import zoneinfo
from collections.abc import Mapping, Sequence
from typing import override

import pytest

from themis.services.cost_exporter import promql, report, report_queries, slack

_END = datetime.datetime(2026, 9, 6, 22, 0, tzinfo=datetime.UTC)
_HOUR = datetime.timedelta(hours=1)
_SYDNEY = zoneinfo.ZoneInfo('Australia/Sydney')
_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'

_QUERIES = report_queries.Queries(
    day=report_queries.DayQueries(**{key: f'day/{key}' for key in report_queries.DAY_KEYS}),
    hourly=report_queries.HourlyQueries(**{key: f'hourly/{key}' for key in report_queries.HOURLY_KEYS}),
)


def _points(*values: float) -> tuple[promql.Point, ...]:
    return tuple(promql.Point(_END - _HOUR * (len(values) - 1 - i), v) for i, v in enumerate(values))


class _Source(promql.PromQL):
    """Answers each query from a table; a query the table lacks has no series."""

    def __init__(
        self,
        instants: Mapping[str, float] | None = None,
        ranges: Mapping[str, Sequence[promql.Point]] | None = None,
    ) -> None:
        self._instants = dict(instants or {})
        self._ranges = dict(ranges or {})
        self.instant_queries: list[tuple[str, datetime.datetime]] = []
        self.range_queries: list[tuple[str, datetime.datetime, datetime.datetime, datetime.timedelta]] = []

    @override
    def instant(self, query: str, *, at: datetime.datetime) -> float | None:
        self.instant_queries.append((query, at))
        return self._instants.get(query)

    @override
    def range(
        self, query: str, *, start: datetime.datetime, end: datetime.datetime, step: datetime.timedelta
    ) -> tuple[promql.Point, ...]:
        self.range_queries.append((query, start, end, step))
        return tuple(self._ranges.get(query, ()))


class _Channel(slack.ReportChannel):
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, bytes, str]] = []

    @override
    def post(self, channel_id: str, text: str, png: bytes, filename: str) -> None:
        self.posts.append((channel_id, text, png, filename))


def _deadline(seconds: int = 60) -> report.Deadline:
    return report.Deadline(seconds, clock=lambda: 0.0)


def _run(source: _Source, channel: _Channel, *, deadline: report.Deadline | None = None) -> report.Outcome:
    return report.run(
        _QUERIES, source, channel, channel_id='C01', now=lambda: _END, deadline=deadline or _deadline(), tz=_SYDNEY
    )


def _day(**cents: float | None) -> report.Day:
    return report.Day(**{key: cents.get(key) for key in report_queries.DAY_KEYS})


def test_nothing_moved_posts_nothing_and_reads_no_hourly_series() -> None:
    source = _Source(instants={'day/sessions': 0.0, 'day/sessions_runtime': 0.0})
    channel = _Channel()

    outcome = _run(source, channel)

    assert outcome.posted is False
    assert channel.posts == []
    assert source.range_queries == []


def test_spend_posts_once_with_the_total_first_then_every_producer() -> None:
    source = _Source(
        instants={
            'day/sessions': 1234,
            'day/sessions_runtime': 100,
            'day/sessions_search': 34,
            'day/sessions_tokens': 1100,
            'day/ci': 250,
        },
        ranges={'hourly/sessions': _points(100, 200, 934), 'hourly/ci': _points(250)},
    )
    channel = _Channel()

    outcome = _run(source, channel)

    assert outcome.posted is True
    [(channel_id, text, png, filename)] = channel.posts
    assert channel_id == 'C01'
    assert png.startswith(_PNG_SIGNATURE)
    assert filename == 'themis-spend-2026-09-07.png'
    # 22:00 UTC on the 6th is 08:00 on the 7th in Sydney (AEST, UTC+10); the total is the three producers, so the
    # sessions split is not counted twice, and a producer with no series says so rather than reading as $0.00.
    assert text.splitlines() == [
        'Themis spend in the 24 h to Mon 7 Sep 2026 08:00 (Australia/Sydney): $14.84',
        '• Managed Agents sessions: $12.34 — runtime $1.00, tokens $11.00, web search $0.34',
        '• Convert worker: no data',
        '• CI (Claude Code): $2.50',
    ]


def test_sessions_with_no_series_carries_no_split() -> None:
    lines = report.message(_day(convert=500), _END, _SYDNEY).splitlines()

    assert lines[1] == '• Managed Agents sessions: no data'
    assert lines[0].endswith(': $5.00')


def test_a_share_with_no_series_reads_no_data_inside_the_split() -> None:
    lines = report.message(_day(sessions=500, sessions_runtime=100, sessions_search=0), _END, _SYDNEY).splitlines()

    assert lines[1] == '• Managed Agents sessions: $5.00 — runtime $1.00, tokens no data, web search $0.00'


def test_no_producer_with_a_series_is_no_total() -> None:
    assert _day().total is None
    assert report.message(_day(), _END, _SYDNEY).splitlines()[0].endswith(': no data')


def test_a_negative_day_is_shown_as_negative_and_still_posted() -> None:
    source = _Source(instants={'day/sessions': -1500})
    channel = _Channel()

    outcome = _run(source, channel)

    assert outcome.posted is True
    [(_, text, _, _)] = channel.posts
    assert text.splitlines()[0].endswith(': -$15.00')
    assert text.splitlines()[1].startswith('• Managed Agents sessions: -$15.00')


def test_a_share_moving_while_the_total_stands_still_posts() -> None:
    # A deleted session on one agent cancelling another's spend leaves a day with something to report.
    assert _day(sessions=0, sessions_runtime=200, sessions_tokens=-200).moved is True
    assert _day(sessions=0, convert=0).moved is False
    assert _day().moved is False


def test_the_day_is_read_at_the_windows_end_and_the_hours_tile_the_window() -> None:
    source = _Source(instants={'day/ci': 1})

    _run(source, _Channel())

    assert source.instant_queries == [(f'day/{key}', _END) for key in report_queries.DAY_KEYS]
    # Each evaluation is the hour ending at it, so 24 of them tile the window when the first sits one hour in.
    assert source.range_queries == [
        (f'hourly/{key}', _END - 23 * _HOUR, _END, _HOUR) for key in report_queries.HOURLY_KEYS
    ]


def test_hourly_cents_reach_the_chart_as_dollars_under_the_producers_names() -> None:
    source = _Source(ranges={'hourly/convert': _points(150, 25)})

    series = report.read_hourly(_QUERIES.hourly, source, start=_END - 24 * _HOUR, end=_END, deadline=_deadline())

    assert [one.label for one in series] == ['Managed Agents sessions', 'Convert worker', 'CI (Claude Code)']
    assert [value for _, value in series[1].points] == [1.5, 0.25]
    assert series[0].points == []


def test_a_figure_is_dollars_or_no_data() -> None:
    assert report.figure(1234) == '$12.34'
    assert report.figure(None) == 'no data'


def test_the_heading_and_the_chart_name_the_windows_end_in_sydney_time() -> None:
    assert report.window_end(_END, _SYDNEY) == 'Mon 7 Sep 2026 08:00 (Australia/Sydney)'
    assert report.chart_title(_END, _SYDNEY).endswith('24 h to Mon 7 Sep 2026 08:00 (Australia/Sydney)')
    assert report.chart_filename(_END, _SYDNEY) == 'themis-spend-2026-09-07.png'


def test_a_naive_clock_fails_before_any_query() -> None:
    source = _Source()
    with pytest.raises(ValueError, match='timezone-aware'):
        report.run(
            _QUERIES,
            source,
            _Channel(),
            channel_id='C01',
            now=lambda: _END.replace(tzinfo=None),
            deadline=_deadline(),
            tz=_SYDNEY,
        )
    assert source.instant_queries == []


def test_a_spent_deadline_stops_the_run_before_the_next_call() -> None:
    ticks = iter([0.0, 61.0])
    deadline = report.Deadline(60, clock=lambda: next(ticks))
    source = _Source(instants={'day/sessions': 1})
    channel = _Channel()

    with pytest.raises(TimeoutError, match='60s deadline'):
        _run(source, channel, deadline=deadline)

    assert source.instant_queries == []
    assert channel.posts == []


def test_a_failed_read_posts_nothing() -> None:
    class _Failing(_Source):
        @override
        def instant(self, query: str, *, at: datetime.datetime) -> float | None:
            raise promql.QueryError('query did not succeed')

    channel = _Channel()
    with pytest.raises(promql.QueryError):
        _run(_Failing(), channel)
    assert channel.posts == []


_ENV = {
    'THEMIS_COST_REPORT_QUERIES': json.dumps(
        {
            'day': dict.fromkeys(report_queries.DAY_KEYS, 'q'),
            'hourly': dict.fromkeys(report_queries.HOURLY_KEYS, 'q'),
        }
    ),
    'THEMIS_COST_REPORT_PROJECT': 'themis-test',
    'THEMIS_COST_REPORT_SLACK_CHANNEL_ID': 'C0123',
    'THEMIS_COST_REPORT_SLACK_BOT_TOKEN': 'xoxb-test',
    'THEMIS_COST_REPORT_DEADLINE_SECONDS': '120',
    'THEMIS_COST_REPORT_TIME_ZONE': 'Australia/Sydney',
}


def test_a_complete_environment_reads_as_the_settings() -> None:
    settings = report.settings_from(_ENV)

    assert settings.project == 'themis-test'
    assert settings.slack_channel_id == 'C0123'
    assert settings.slack_bot_token == 'xoxb-test'
    assert settings.deadline_seconds == 120
    assert settings.time_zone.key == 'Australia/Sydney'
    assert settings.queries.day.sessions == 'q'
    assert 'xoxb-test' not in repr(settings)


def test_an_unknown_time_zone_exits_naming_the_variable() -> None:
    with pytest.raises(SystemExit, match='THEMIS_COST_REPORT_TIME_ZONE'):
        report.settings_from({**_ENV, 'THEMIS_COST_REPORT_TIME_ZONE': 'Mars/Olympus_Mons'})


@pytest.mark.parametrize('missing', sorted(_ENV))
def test_a_missing_variable_exits_naming_it(missing: str) -> None:
    environ = {name: value for name, value in _ENV.items() if name != missing}
    with pytest.raises(SystemExit, match=missing):
        report.settings_from(environ)


def test_malformed_queries_exit_naming_the_variable_and_the_path() -> None:
    broken = json.loads(_ENV['THEMIS_COST_REPORT_QUERIES'])
    del broken['day']['ci']
    with pytest.raises(SystemExit, match=r"THEMIS_COST_REPORT_QUERIES: queries\.day: missing \['ci'\]"):
        report.settings_from({**_ENV, 'THEMIS_COST_REPORT_QUERIES': json.dumps(broken)})


@pytest.mark.parametrize('deadline', ['0', '-5', '1.5', 'soon'])
def test_a_deadline_that_is_not_a_positive_second_count_exits(deadline: str) -> None:
    with pytest.raises(SystemExit, match='positive whole number of seconds'):
        report.settings_from({**_ENV, 'THEMIS_COST_REPORT_DEADLINE_SECONDS': deadline})
