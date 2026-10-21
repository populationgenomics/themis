"""The loader shared by the mirrors: an undeclared key is dropped from the mirror and kept in the text."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping

import pytest

from themis.litcache import mirror
from themis.litcache.models import openalex_pb2


def _as_is(record: Mapping[str, object]) -> dict[str, object]:
    return dict(record)


def _load(record: Mapping[str, object]) -> mirror.Loaded[openalex_pb2.Work]:
    return mirror.load(record, openalex_pb2.Work(), index='openalex', wrap=_as_is)


def test_declared_keys_load() -> None:
    work = _load({'title': 'T', 'publication_year': 2020, 'ids': {'pmid': 'p'}}).work
    assert (work.title, work.publication_year, work.ids.pmid) == ('T', 2020, 'p')


@pytest.mark.parametrize(
    'record',
    [{'title': 'T', 'novelty': 1}, {'title': 'T', 'ids': {'pmid': 'p', 'novelty': 'x'}}],
    ids=['top-level', 'nested'],
)
def test_an_undeclared_key_is_dropped_from_the_mirror_and_kept_in_the_text(record: dict[str, object]) -> None:
    loaded = _load(record)
    assert loaded.work.title == 'T'
    assert mirror.parse_text(loaded.text) == record


def test_a_value_a_declared_field_cannot_hold_is_drift_naming_the_key_and_the_index() -> None:
    with pytest.raises(mirror.SchemaDriftError) as excinfo:
        _load({'publication_year': 'twenty-twenty'})
    assert excinfo.value.index == 'openalex'
    assert 'publication_year' in excinfo.value.detail
    assert '\n' not in excinfo.value.detail


def test_null_reads_as_absent() -> None:
    work = _load({'title': None, 'authorships': None, 'biblio': None}).work
    assert not work.HasField('title')
    assert not work.HasField('biblio')
    assert len(work.authorships) == 0


def test_the_wrapping_reaches_the_mirror_and_not_the_text() -> None:
    record = {'abstract_inverted_index': {'The': [0]}}

    def wrap(source: Mapping[str, object]) -> dict[str, object]:
        index = source['abstract_inverted_index']
        assert isinstance(index, Mapping)
        return {'abstract_inverted_index': {word: {'positions': positions} for word, positions in index.items()}}

    loaded = mirror.load(record, openalex_pb2.Work(), index='openalex', wrap=wrap)
    assert list(loaded.work.abstract_inverted_index['The'].positions) == [0]
    assert mirror.parse_text(loaded.text) == record


def test_the_text_is_ascii_and_parses_back_to_the_record() -> None:
    record = {'title': 'Gène ✓', 'ids': {'pmid': 'p'}, 'extra': [1, 2.5, None, {'k': True}]}
    text = mirror.to_text(record)
    assert text.isascii()
    assert json.loads(text) == record


@pytest.mark.parametrize('number', [math.nan, math.inf, -math.inf])
def test_a_number_json_text_cannot_carry_is_drift_naming_its_path(number: float) -> None:
    with pytest.raises(mirror.SchemaDriftError, match=r'counts\[1\]\.value'):
        _load({'title': 'T', 'counts': [{'value': 1}, {'value': number}]})


@pytest.mark.parametrize('text', ['', '[]', 'null', '"x"', '{', '{"n": NaN}', '{"n": -Infinity}'])
def test_parse_text_rejects_anything_but_a_json_object(text: str) -> None:
    with pytest.raises(ValueError, match=r'Expecting|expected a JSON object|not standard JSON'):
        mirror.parse_text(text)


@pytest.mark.parametrize(
    ('record', 'key'),
    [
        ({'title': 'T', 'ids': 'https://pubmed.ncbi.nlm.nih.gov/1'}, 'ids'),
        ({'title': 'T', 'ids': ['p']}, 'ids'),
        ({'title': 'T', 'ids': ''}, 'ids'),
        ({'title': 'T', 'authorships': ['Jane Doe']}, r'authorships\[0\]'),
    ],
    ids=['string', 'list', 'empty-string', 'list-element'],
)
def test_a_declared_object_given_another_shape_drifts(record: Mapping[str, object], key: str) -> None:
    # Not loaded as an empty message: that would store the paper without what the field held.
    with pytest.raises(mirror.SchemaDriftError, match=rf'{key} is \w+, where an object is declared'):
        _load(record)
