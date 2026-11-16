"""The pedigree extraction contract: what a construction must say, and the record of a set per figure."""

from __future__ import annotations

import protovalidate
import pytest
from grus.models import pedigree_pb2

from themis.litcache.models import litcache_pb2, paper_pedigrees_pb2, pedigrees_pb2


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


def test_a_figure_read_in_one_conversation_says_why_it_failed() -> None:
    construction = _construction(
        pedigrees_pb2.PedigreeFigure(figure=0),
        pedigrees_pb2.PedigreeFigure(figure=1, failure='stopped with refusal'),
    )

    protovalidate.validate(construction)
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(pedigrees_pb2.PedigreeFigure(figure=1, failure=''))


def test_a_figure_fails_by_page_or_whole_not_both() -> None:
    figure = pedigrees_pb2.PedigreeFigure(figure=1, failures=[_failure(5)], failure='stopped with refusal')

    with pytest.raises(protovalidate.ValidationError, match='one reason'):
        protovalidate.validate(figure)


def test_a_construction_where_every_figure_failed_is_refused() -> None:
    with pytest.raises(protovalidate.ValidationError, match='accepted'):
        protovalidate.validate(_construction(pedigrees_pb2.PedigreeFigure(figure=0, failure='stopped with refusal')))


def test_a_paper_s_pedigrees_are_a_grus_set_per_figure_keyed_by_its_index() -> None:
    record = paper_pedigrees_pb2.PaperPedigrees(figures={0: pedigree_pb2.PedigreeSet(), 3: pedigree_pb2.PedigreeSet()})

    protovalidate.validate(record)


@pytest.mark.parametrize(
    'record',
    [
        paper_pedigrees_pb2.PaperPedigrees(),
        paper_pedigrees_pb2.PaperPedigrees(figures={-1: pedigree_pb2.PedigreeSet()}),
    ],
)
def test_a_record_holds_an_accepted_figure_keyed_by_an_index(record: paper_pedigrees_pb2.PaperPedigrees) -> None:
    with pytest.raises(protovalidate.ValidationError):
        protovalidate.validate(record)


def test_the_manifest_s_stubs_need_no_grus() -> None:
    # The manifest imports the construction; only the record's bytes import grus's schema.
    imported = {dependency.name for dependency in pedigrees_pb2.DESCRIPTOR.dependencies}
    assert 'grus/models/pedigree.proto' not in imported
