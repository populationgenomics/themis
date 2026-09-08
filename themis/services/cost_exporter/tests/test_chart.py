"""The chart renders to a PNG for whatever the window held, and refuses what it cannot draw."""

from __future__ import annotations

import datetime
import zoneinfo

import pytest

from themis.services.cost_exporter import chart

_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
_END = datetime.datetime(2026, 9, 6, 22, 0, tzinfo=datetime.UTC)
_START = _END - datetime.timedelta(hours=24)
_TZ = zoneinfo.ZoneInfo('Australia/Sydney')


def _hours(*values: float) -> list[tuple[datetime.datetime, float]]:
    return [(_START + datetime.timedelta(hours=index + 1), value) for index, value in enumerate(values)]


def _render(*series: chart.Series) -> bytes:
    return chart.render('Spend', series, window=(_START, _END), tz=_TZ)


def test_three_producers_stack_into_one_png() -> None:
    png = _render(
        chart.Series('sessions', _hours(1.0, 2.5, 3.0)),
        chart.Series('convert', _hours(0.0, 0.5)),
        chart.Series('CI', _hours(0.25, 0.25, 0.25, 0.25)),
    )

    assert png.startswith(_PNG_SIGNATURE)


def test_a_negative_hour_renders() -> None:
    # A deleted session lowers a running total; the bar goes below the baseline rather than failing the run.
    assert _render(chart.Series('sessions', _hours(1.0, -2.0, 0.5)), chart.Series('CI', _hours(0.1))).startswith(
        _PNG_SIGNATURE
    )


def test_producers_with_nothing_to_draw_still_render() -> None:
    assert _render(chart.Series('sessions', []), chart.Series('convert', []), chart.Series('CI', [])).startswith(
        _PNG_SIGNATURE
    )


def test_no_series_fails() -> None:
    with pytest.raises(ValueError, match='at least one series'):
        _render()


def test_more_series_than_the_palette_has_hues_for_fails() -> None:
    with pytest.raises(ValueError, match='at most 3 series'):
        _render(*(chart.Series(str(index), _hours(1.0)) for index in range(4)))
