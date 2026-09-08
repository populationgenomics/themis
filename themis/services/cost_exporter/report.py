"""The morning spend report (`python -m themis.services.cost_exporter.report`): one run, then exit.

A Cloud Run Job execution on the report's schedule (`infra/themis_infra/cost_report.py`), from the exporter's image.
The environment carries the queries as the program renders them, the GCP project whose metrics they read, the Slack
channel and bot token the report posts with, the schedule's time zone and the run's deadline; all are read before any
network call, and a missing or malformed one fails the run naming it. The run reads the day's figures at the
window's end — the total across producers, then each producer, the sessions figure split into runtime, tokens and
web search — and posts them once with a chart of the window hour by hour, or posts nothing when no figure moved. A
producer no series answered for reads as no data, not as nothing spent; a figure below zero is shown as it is. The
heading names the window's end in the schedule's zone.
"""

from __future__ import annotations

import dataclasses
import datetime
import functools
import logging
import os
import time
import zoneinfo
from collections.abc import Callable, Mapping

import google.auth
from google.auth.transport import requests as google_auth_requests

from themis.services.cost_exporter import chart, env, money, promql, report_queries, slack

_QUERIES_VAR = 'THEMIS_COST_REPORT_QUERIES'
_DEADLINE_VAR = 'THEMIS_COST_REPORT_DEADLINE_SECONDS'
_TIME_ZONE_VAR = 'THEMIS_COST_REPORT_TIME_ZONE'

_WINDOW = datetime.timedelta(hours=24)
_STEP = datetime.timedelta(hours=1)
_SCOPES = ('https://www.googleapis.com/auth/cloud-platform',)

_SESSIONS_TITLE = 'Managed Agents sessions'
_CONVERT_TITLE = 'Convert worker'
_CI_TITLE = 'CI (Claude Code)'

_logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Settings:
    """The run's configuration, read whole from the environment before any network call.

    Attributes:
        queries: The report's PromQL, as the program renders it.
        project: The GCP project whose metrics are queried.
        slack_channel_id: The channel the report is posted to, by id.
        slack_bot_token: The bot token of the Slack app that posts.
        deadline_seconds: The run's time budget, well under the Job's timeout.
        time_zone: The schedule's zone, which the report dates its window in.
    """

    queries: report_queries.Queries
    project: str
    slack_channel_id: str
    slack_bot_token: str = dataclasses.field(repr=False)
    deadline_seconds: int
    time_zone: zoneinfo.ZoneInfo


def settings_from(environ: Mapping[str, str]) -> Settings:
    """Read the run's settings from `environ`; a missing or malformed value exits naming its variable."""
    try:
        queries = report_queries.parse(env.require(environ, _QUERIES_VAR))
    except ValueError as e:
        raise SystemExit(f'{_QUERIES_VAR}: {e}') from None
    zone_name = env.require(environ, _TIME_ZONE_VAR)
    try:
        time_zone = zoneinfo.ZoneInfo(zone_name)
    except zoneinfo.ZoneInfoNotFoundError:
        raise SystemExit(f'{_TIME_ZONE_VAR} names no known time zone: {zone_name!r}') from None
    return Settings(
        queries=queries,
        project=env.require(environ, 'THEMIS_COST_REPORT_PROJECT'),
        slack_channel_id=env.require(environ, 'THEMIS_COST_REPORT_SLACK_CHANNEL_ID'),
        slack_bot_token=env.require(environ, 'THEMIS_COST_REPORT_SLACK_BOT_TOKEN'),
        deadline_seconds=env.positive_seconds(environ, _DEADLINE_VAR),
        time_zone=time_zone,
    )


class Deadline:
    """The run's time budget, checked between network calls.

    The clients are synchronous and each request carries its own timeout, so the budget is enforced at the seams: a
    check that finds it spent raises before the next call starts. The Job's timeout backstops a call that hangs.
    """

    def __init__(self, seconds: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._seconds = seconds
        self._clock = clock
        self._started = clock()

    def check(self) -> None:
        """Raise `TimeoutError` once the budget is spent."""
        if self._clock() - self._started > self._seconds:
            raise TimeoutError(f'the run exceeded its {self._seconds}s deadline')


@dataclasses.dataclass(frozen=True)
class Day:
    """The trailing day's figures, in cents; `None` where no series answered — nothing to read, not zero spend.

    Attributes:
        sessions: Managed Agents list cost.
        sessions_runtime: Its runtime share.
        sessions_search: Its web-search share.
        sessions_tokens: Its token share.
        convert: The convert worker's derived spend.
        ci: Claude Code's spend in CI.
    """

    sessions: float | None
    sessions_runtime: float | None
    sessions_search: float | None
    sessions_tokens: float | None
    convert: float | None
    ci: float | None

    @property
    def total(self) -> float | None:
        """Every producer with a figure, summed; `None` when none has one."""
        figures = [figure for figure in (self.sessions, self.convert, self.ci) if figure is not None]
        return sum(figures) if figures else None

    @property
    def moved(self) -> bool:
        """Whether any figure is non-zero: what makes the day worth a post."""
        return any(figure for figure in dataclasses.astuple(self) if figure is not None)


@dataclasses.dataclass(frozen=True)
class Outcome:
    """What a run read and whether it posted, for the entrypoint to log."""

    day: Day
    posted: bool


def figure(cents: float | None) -> str:
    """A day figure for a reader: dollars, or `no data` where no series answered."""
    return 'no data' if cents is None else money.dollars(cents)


def window_end(at: datetime.datetime, tz: zoneinfo.ZoneInfo) -> str:
    """The window's end as the reader's morning: `Tue 8 Sep 2026 08:00 (Australia/Sydney)`."""
    local = at.astimezone(tz)
    return f'{local:%a} {local.day} {local:%b %Y %H:%M} ({tz.key})'


def read_day(
    queries: report_queries.DayQueries, source: promql.PromQL, *, at: datetime.datetime, deadline: Deadline
) -> Day:
    """The day's figures, each query evaluated at `at`."""

    def read(query: str) -> float | None:
        deadline.check()
        return source.instant(query, at=at)

    return Day(
        sessions=read(queries.sessions),
        sessions_runtime=read(queries.sessions_runtime),
        sessions_search=read(queries.sessions_search),
        sessions_tokens=read(queries.sessions_tokens),
        convert=read(queries.convert),
        ci=read(queries.ci),
    )


def read_hourly(
    queries: report_queries.HourlyQueries,
    source: promql.PromQL,
    *,
    start: datetime.datetime,
    end: datetime.datetime,
    deadline: Deadline,
) -> list[chart.Series]:
    """Each producer's hourly series over (`start`, `end`], in dollars, as the chart draws them."""

    def read(label: str, query: str) -> chart.Series:
        deadline.check()
        # An evaluation covers the hour ending at it, so the first is one step in: 24 bars tile the window.
        points = source.range(query, start=start + _STEP, end=end, step=_STEP)
        return chart.Series(label, [(at, money.to_dollars(value)) for at, value in points])

    return [
        read(_SESSIONS_TITLE, queries.sessions),
        read(_CONVERT_TITLE, queries.convert),
        read(_CI_TITLE, queries.ci),
    ]


def message(day: Day, at: datetime.datetime, tz: zoneinfo.ZoneInfo) -> str:
    """The post's text: the total, then one line per producer, the sessions line carrying its split."""
    split = ''
    if day.sessions is not None:
        split = (
            f' — runtime {figure(day.sessions_runtime)}, tokens {figure(day.sessions_tokens)}, '
            f'web search {figure(day.sessions_search)}'
        )
    return '\n'.join(
        [
            f'Themis spend in the 24 h to {window_end(at, tz)}: {figure(day.total)}',
            f'• {_SESSIONS_TITLE}: {figure(day.sessions)}{split}',
            f'• {_CONVERT_TITLE}: {figure(day.convert)}',
            f'• {_CI_TITLE}: {figure(day.ci)}',
        ]
    )


def chart_title(at: datetime.datetime, tz: zoneinfo.ZoneInfo) -> str:
    """The chart's heading, dated by the window's end in `tz`."""
    return f'Themis spend per hour, by producer — 24 h to {window_end(at, tz)}'


def chart_filename(at: datetime.datetime, tz: zoneinfo.ZoneInfo) -> str:
    """The chart's filename, dated by the window's end in `tz`."""
    return f'themis-spend-{at.astimezone(tz):%Y-%m-%d}.png'


def run(
    queries: report_queries.Queries,
    source: promql.PromQL,
    channel: slack.ReportChannel,
    *,
    channel_id: str,
    now: Callable[[], datetime.datetime],
    deadline: Deadline,
    tz: zoneinfo.ZoneInfo,
) -> Outcome:
    """Read the trailing 24 hours, then post the report once — unless no figure moved.

    Args:
        queries: The report's PromQL.
        source: Where the queries are evaluated.
        channel: Where the report is posted.
        channel_id: The channel posted to.
        now: The clock the window ends at, read once before any query.
        deadline: The run's time budget.
        tz: The zone the report dates its window in.

    Raises:
        ValueError: `now` returned a naive datetime.
        TimeoutError: The deadline passed between two calls.
        Exception: Whatever the source or the channel raises; nothing is posted after a failed read.
    """
    end = now()
    if end.tzinfo is None:
        raise ValueError('precondition failed: the clock must return a timezone-aware datetime')
    start = end - _WINDOW
    day = read_day(queries.day, source, at=end, deadline=deadline)
    _log(day)
    if not day.moved:
        _logger.info('no figure moved in the 24 h to %s; nothing posted', end.isoformat())
        return Outcome(day=day, posted=False)
    series = read_hourly(queries.hourly, source, start=start, end=end, deadline=deadline)
    png = chart.render(chart_title(end, tz), series, window=(start, end), tz=tz)
    deadline.check()
    channel.post(channel_id, message(day, end, tz), png, chart_filename(end, tz))
    return Outcome(day=day, posted=True)


def _log(day: Day) -> None:
    for name, cents in dataclasses.asdict(day).items():
        _logger.info('%s: %s in the last 24 h', name, figure(cents))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings = settings_from(os.environ)
    deadline = Deadline(settings.deadline_seconds)
    credentials, _ = google.auth.default(scopes=_SCOPES)
    with google_auth_requests.AuthorizedSession(credentials) as session:
        try:
            outcome = run(
                settings.queries,
                promql.CloudMonitoringPromQL(session, settings.project),
                slack.SlackChannel(slack.web_client(settings.slack_bot_token)),
                channel_id=settings.slack_channel_id,
                now=functools.partial(datetime.datetime.now, datetime.UTC),
                deadline=deadline,
                tz=settings.time_zone,
            )
        except TimeoutError:
            raise SystemExit(f'the run exceeded its {settings.deadline_seconds}s deadline') from None
    if outcome.posted:
        _logger.info('posted to %s', settings.slack_channel_id)


if __name__ == '__main__':
    main()
