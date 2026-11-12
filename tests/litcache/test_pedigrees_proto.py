"""The pedigree extraction contract refuses a construction that cannot say what was read."""

from __future__ import annotations

import protovalidate
import pytest

from themis.litcache.models import litcache_pb2, pedigrees_pb2


def _construction(*read: pedigrees_pb2.PedigreeFigure) -> pedigrees_pb2.PedigreeExtractionConstruction:
    return pedigrees_pb2.PedigreeExtractionConstruction(
        model='claude-sonnet-5-5', harness_version='1.abc', figures_read=read
    )


def _failure(page: int, reason: str = 'stopped with refusal') -> pedigrees_pb2.PageFailure:
    return pedigrees_pb2.PageFailure(pdf_page=page, reason=reason)


def test_a_record_of_a_paper_s_pedigrees_says_what_was_read_and_what_failed() -> None:
    record = litcache_pb2.Record(
        deps=['a' * 64],
        pedigree_extraction=_construction(
            pedigrees_pb2.PedigreeFigure(figure=0),
            pedigrees_pb2.PedigreeFigure(figure=2, failures=[_failure(5)]),
        ),
    )
    protovalidate.validate(record)


def test_a_construction_that_read_nothing_is_refused() -> None:
    with pytest.raises(protovalidate.ValidationError, match='read'):
        protovalidate.validate(_construction())


@pytest.mark.parametrize(
    'figure',
    [
        pedigrees_pb2.PedigreeFigure(figure=-1),
        pedigrees_pb2.PedigreeFigure(figure=1, failures=[_failure(5, '')]),
        pedigrees_pb2.PedigreeFigure(figure=1, failures=[_failure(0)]),
        pedigrees_pb2.PedigreeFigure(figure=1, failures=[_failure(5), _failure(5, 'rejected')]),
    ],
)
def test_a_figure_is_an_index_and_each_failure_names_its_page_once_and_says_why(
    figure: pedigrees_pb2.PedigreeFigure,
) -> None:
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(figure)


def test_a_figure_listed_twice_is_refused() -> None:
    twice = _construction(
        pedigrees_pb2.PedigreeFigure(figure=2),
        pedigrees_pb2.PedigreeFigure(figure=2, failures=[_failure(5)]),
    )

    with pytest.raises(protovalidate.ValidationError, match='listed once'):
        protovalidate.validate(twice)
