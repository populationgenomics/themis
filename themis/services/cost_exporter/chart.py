"""The report's chart: the last 24 hours as one panel of hourly bars, stacked by producer.

Drawn on a bare `Figure` with the Agg canvas, never through `pyplot`: pyplot keeps a process-global registry of open
figures, and a Job that draws once and exits has no use for it. The figure is rendered to PNG bytes in memory for
the Slack upload.
"""

from __future__ import annotations

import dataclasses
import datetime
import io
from collections.abc import Sequence

from matplotlib import axes as matplotlib_axes
from matplotlib import dates as matplotlib_dates
from matplotlib import figure as matplotlib_figure
from matplotlib import patches as matplotlib_patches
from matplotlib import ticker as matplotlib_ticker
from matplotlib.backends import backend_agg

from themis.services.cost_exporter import money

_WIDTH_INCHES = 10
_HEIGHT_INCHES = 4
_DPI = 144
# Each bar is the hour ending at its point's instant; matplotlib's date axis counts days.
_BAR_WIDTH_DAYS = 1 / 24
# Categorical hues in a fixed order, distinct to every colour vision for up to three series; the surface colour
# drawn as each bar's edge is the gap that separates stacked segments and neighbouring hours.
_SERIES_COLOURS = ('#2a78d6', '#eb6834', '#1baf7a')
_SURFACE = '#fcfcfb'
_INK = '#0b0b0b'
_SECONDARY_INK = '#52514e'
_MUTED_INK = '#898781'
_GRID = '#e1e0d9'
_AXIS = '#c3c2b7'


@dataclasses.dataclass(frozen=True)
class Series:
    """One producer's hourly figures.

    Attributes:
        label: The producer's name, as the legend shows it.
        points: The dollars spent in the hour ending at each instant, in time order.
    """

    label: str
    points: Sequence[tuple[datetime.datetime, float]]


def render(
    title: str, series: Sequence[Series], *, window: tuple[datetime.datetime, datetime.datetime], tz: datetime.tzinfo
) -> bytes:
    """The series as one PNG: hourly bars stacked by series over `window`, the x axis labelled in `tz`.

    A negative hour — a deleted session lowers a running total — stacks below the baseline, apart from the positives.

    Raises:
        ValueError: No series, or more than the palette has hues for.
    """
    if not series:
        raise ValueError('precondition failed: a chart has at least one series')
    if len(series) > len(_SERIES_COLOURS):
        raise ValueError(f'precondition failed: at most {len(_SERIES_COLOURS)} series, got {len(series)}')
    figure = matplotlib_figure.Figure(figsize=(_WIDTH_INCHES, _HEIGHT_INCHES), layout='constrained', facecolor=_SURFACE)
    backend_agg.FigureCanvasAgg(figure)
    axes = figure.add_subplot()
    _stack_bars(axes, series)
    _style_axes(axes, title=title, series=series, window=window, tz=tz)
    buffer = io.BytesIO()
    figure.savefig(buffer, format='png', dpi=_DPI, facecolor=_SURFACE)
    return buffer.getvalue()


def _stack_bars(axes: matplotlib_axes.Axes, series: Sequence[Series]) -> None:
    """One bar per hour per series: positives stacked up from the baseline, negatives down from it."""
    above: dict[float, float] = {}
    below: dict[float, float] = {}
    for one, colour in zip(series, _SERIES_COLOURS, strict=False):
        centres, heights, bottoms = [], [], []
        for at, dollars in one.points:
            if dollars == 0:
                continue
            centre = float(matplotlib_dates.date2num(at)) - _BAR_WIDTH_DAYS / 2
            stack = above if dollars > 0 else below
            centres.append(centre)
            heights.append(dollars)
            bottoms.append(stack.get(centre, 0.0))
            stack[centre] = stack.get(centre, 0.0) + dollars
        axes.bar(centres, heights, width=_BAR_WIDTH_DAYS, bottom=bottoms, color=colour, edgecolor=_SURFACE, linewidth=1)


def _style_axes(
    axes: matplotlib_axes.Axes,
    *,
    title: str,
    series: Sequence[Series],
    window: tuple[datetime.datetime, datetime.datetime],
    tz: datetime.tzinfo,
) -> None:
    """The title, dollar ticks, the window on a date axis in `tz`, recessive grid and spines, and the legend."""
    axes.set_facecolor(_SURFACE)
    axes.set_title(title, loc='left', color=_INK)
    axes.set_ylabel('USD per hour', color=_SECONDARY_INK)
    axes.set_xlim(float(matplotlib_dates.date2num(window[0])), float(matplotlib_dates.date2num(window[1])))
    locator = matplotlib_dates.AutoDateLocator(tz=tz)
    axes.xaxis.set_major_locator(locator)
    axes.xaxis.set_major_formatter(matplotlib_dates.ConciseDateFormatter(locator, tz=tz))
    axes.yaxis.set_major_formatter(matplotlib_ticker.FuncFormatter(lambda amount, _: money.format_dollars(amount)))
    axes.grid(visible=True, axis='y', color=_GRID, linewidth=1)
    axes.set_axisbelow(True)
    axes.axhline(0, color=_AXIS, linewidth=1)
    for side in ('top', 'right'):
        axes.spines[side].set_visible(False)
    for side in ('left', 'bottom'):
        axes.spines[side].set_color(_AXIS)
    axes.tick_params(colors=_MUTED_INK, labelcolor=_MUTED_INK)
    # Explicit handles: a producer with nothing to draw stays in the legend, so the reader sees it was covered.
    handles = [
        matplotlib_patches.Patch(facecolor=colour, label=one.label)
        for one, colour in zip(series, _SERIES_COLOURS, strict=False)
    ]
    axes.legend(handles=handles, loc='upper left', frameon=False, fontsize='small', labelcolor=_SECONDARY_INK)
    if not any(one.points for one in series):
        axes.text(0.5, 0.5, 'no data', transform=axes.transAxes, ha='center', va='center', color=_MUTED_INK)
