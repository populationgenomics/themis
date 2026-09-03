"""The strict loader shared by the mirrors: a key the mirror lacks is drift, never a dropped field."""

from __future__ import annotations

import pytest

from themis.litcache import mirror
from themis.litcache.models import openalex_pb2


def test_declared_keys_load() -> None:
    work = mirror.parse_strict(
        {'title': 'T', 'publication_year': 2020, 'ids': {'pmid': 'p'}}, openalex_pb2.Work(), index='x'
    )
    assert (work.title, work.publication_year, work.ids.pmid) == ('T', 2020, 'p')


def test_an_undeclared_key_is_drift_naming_the_key_and_the_index() -> None:
    with pytest.raises(mirror.SchemaDriftError) as excinfo:
        mirror.parse_strict({'title': 'T', 'novelty': 1}, openalex_pb2.Work(), index='openalex')
    assert excinfo.value.index == 'openalex'
    assert 'novelty' in excinfo.value.detail
    assert '\n' not in excinfo.value.detail
    assert 'openalex' in str(excinfo.value)


def test_an_undeclared_nested_key_is_drift() -> None:
    with pytest.raises(mirror.SchemaDriftError, match='novelty'):
        mirror.parse_strict({'ids': {'novelty': 'x'}}, openalex_pb2.Work(), index='openalex')


def test_a_value_the_field_cannot_hold_is_drift() -> None:
    with pytest.raises(mirror.SchemaDriftError, match='publication_year'):
        mirror.parse_strict({'publication_year': 'twenty-twenty'}, openalex_pb2.Work(), index='openalex')


def test_null_reads_as_absent() -> None:
    work = mirror.parse_strict({'title': None, 'authorships': None, 'biblio': None}, openalex_pb2.Work(), index='x')
    assert not work.HasField('title')
    assert not work.HasField('biblio')
    assert len(work.authorships) == 0
