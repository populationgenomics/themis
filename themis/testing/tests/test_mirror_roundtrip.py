"""The round-trip gate itself: what it reports, and what it deliberately does not."""

from __future__ import annotations

import pytest

from themis.litcache.models import openalex_pb2
from themis.testing import mirror_roundtrip


def _identity(document: dict[str, object]) -> dict[str, object]:
    return document


def test_normalise_drops_what_proto_cannot_distinguish_from_absence() -> None:
    source = {'a': None, 'b': [], 'c': {}, 'd': {'e': None, 'f': [1, None, None]}, 'g': [None]}
    assert mirror_roundtrip.normalise(source) == {'d': {'f': [1]}}


def test_normalise_drops_null_list_elements() -> None:
    assert mirror_roundtrip.normalise({'roles': [None, 'author', None]}) == {'roles': ['author']}


@pytest.mark.parametrize(
    ('source', 'back', 'expected'),
    [
        ({'a': 1}, {}, '$.a: lost'),
        ({}, {'a': 1}, '$.a: only after the round trip'),
        ({'a': 1}, {'a': '1'}, "$.a: 1 before, '1' after"),
        ({'a': [1, 2]}, {'a': [1]}, '$.a: 2 items before, 1 after'),
        ({'a': {'b': {'c': True}}}, {'a': {'b': {'c': False}}}, '$.a.b.c: True before, False after'),
    ],
)
def test_differences_names_the_path_and_the_kind_of_loss(source: object, back: object, expected: str) -> None:
    assert expected in list(mirror_roundtrip.differences(source, back))


def test_differences_is_empty_for_equal_documents() -> None:
    document = {'a': [1, {'b': 'x'}], 'c': 2.5}
    assert list(mirror_roundtrip.differences(document, document)) == []


def test_assert_lossless_passes_a_faithful_mirror_and_fails_a_lossy_one() -> None:
    work = openalex_pb2.Work(title='T', publication_year=2020)
    mirror_roundtrip.assert_lossless({'title': 'T', 'publication_year': 2020}, work, _identity)
    with pytest.raises(AssertionError, match=r'\$\.language: lost'):
        mirror_roundtrip.assert_lossless({'title': 'T', 'publication_year': 2020, 'language': 'en'}, work, _identity)
