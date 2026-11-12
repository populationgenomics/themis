"""The figure annotation contract refuses a figure set whose places a reader could not find."""

from __future__ import annotations

import protovalidate
import pytest

from themis.litcache.models import figures_pb2, litcache_pb2


def _place(
    page: int = 3, *, left: float = 0.1, top: float = 0.2, right: float = 0.9, bottom: float = 0.6
) -> figures_pb2.Place:
    return figures_pb2.Place(pdf_page=page, box=figures_pb2.Box(left=left, top=top, right=right, bottom=bottom))


def _panel(place: figures_pb2.Place | None = None) -> figures_pb2.Panel:
    return figures_pb2.Panel(label='A', kinds=[figures_pb2.FIGURE_KIND_PEDIGREE], place=place or _place())


def test_a_figure_drawn_on_one_page_with_its_legend_on_the_next_is_valid() -> None:
    figure = figures_pb2.Figure(label='Figure 1', legend=_place(4), panels=[_panel()])
    protovalidate.validate(figures_pb2.FigureSet(figures=[figure]))
    record = litcache_pb2.Record(
        deps=['a' * 64],
        figure_annotation=figures_pb2.FigureAnnotationConstruction(model='claude-haiku-5-5', harness_version='1.abc'),
    )
    protovalidate.validate(record)


def test_a_panel_drawing_two_kinds_has_both() -> None:
    panel = figures_pb2.Panel(
        kinds=[figures_pb2.FIGURE_KIND_PEDIGREE, figures_pb2.FIGURE_KIND_SEQUENCE_CHROMATOGRAM], place=_place()
    )
    protovalidate.validate(figures_pb2.Figure(panels=[panel]))


def test_a_paper_with_no_figures_is_an_empty_set() -> None:
    protovalidate.validate(figures_pb2.FigureSet())


@pytest.mark.parametrize(
    'figure',
    [
        figures_pb2.Figure(label='Figure 1'),  # no panels
        figures_pb2.Figure(panels=[figures_pb2.Panel(kinds=[figures_pb2.FIGURE_KIND_CHART])]),  # a panel with no place
        figures_pb2.Figure(panels=[figures_pb2.Panel(place=_place())]),  # a panel with no kind
        figures_pb2.Figure(panels=[figures_pb2.Panel(kinds=[figures_pb2.FIGURE_KIND_UNSPECIFIED], place=_place())]),
        figures_pb2.Figure(  # a kind twice
            panels=[figures_pb2.Panel(kinds=[figures_pb2.FIGURE_KIND_CHART] * 2, place=_place())]
        ),
        figures_pb2.Figure(panels=[_panel(_place(0))]),  # pages count from 1
        figures_pb2.Figure(panels=[_panel(_place(left=0.5, right=0.5))]),  # an empty box
        figures_pb2.Figure(panels=[_panel(_place(bottom=1.2))]),  # a box off the page
    ],
)
def test_a_figure_a_reader_could_not_find_is_refused(figure: figures_pb2.Figure) -> None:
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(figure)
