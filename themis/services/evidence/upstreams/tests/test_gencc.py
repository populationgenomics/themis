"""GenCC submissions table: TSV parsing, HGNC-id keying, per-entity grouping, mechanism notes.

The recorded fixture is a submissions-export subset (tab-separated, with an embedded-newline
``submitted_as_notes`` field) spanning the GenCC tiers. Parsing is exercised from bytes; no
network.
"""

from __future__ import annotations

import csv
import io
import itertools
import pathlib
from collections.abc import Sequence

import pytest

from themis.services.evidence.upstreams import gencc
from themis.svcv4 import gene_disease_validity

_FIXTURE = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'gencc.tsv'

# The columns the parser requires; the real export carries 31.
_REQUIRED = [
    'gene_curie',
    'disease_title',
    'disease_curie',
    'moi_curie',
    'moi_title',
    'classification_title',
    'submitter_title',
    'submitted_as_notes',
    'submitted_run_date',
]

_AD = 'HP:0000006'
_AR = 'HP:0000007'


def _tsv(header: Sequence[str], *rows: Sequence[str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter='\t')
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode()


def _row(disease: str, moi: str, classification: str, submitter: str, notes: str = '') -> list[str]:
    return ['HGNC:9999', f'label {disease}', disease, moi, 'a mode', classification, submitter, notes, '2020-01-01']


def _table() -> gencc.GenCC:
    return gencc.GenCC.from_bytes(_FIXTURE.read_bytes())


def _entity(result: gencc.GenCCResult, disease_curie: str) -> gencc.Entity:
    return next(entity for entity in result.entities if entity.disease_curie == disease_curie)


def test_lookup_carries_the_source_stamp() -> None:
    result = _table().lookup('HGNC:1100')
    assert result is not None
    assert result.source == 'GenCC'
    assert result.dataset_versions  # newest submitted_run_date
    assert result.query == 'HGNC:1100'


def test_every_submission_lands_on_exactly_one_entity() -> None:
    result = _table().lookup('HGNC:1100')
    assert result is not None
    rows = result.raw['rows']
    assert isinstance(rows, list)
    assert sum(len(entity.submissions) for entity in result.entities) == len(rows)
    keys = [(entity.disease_curie, entity.moi_curie) for entity in result.entities]
    assert len(set(keys)) == len(keys) > 1


def test_disagreeing_submitters_reduce_to_the_entity_strongest_and_are_all_kept() -> None:
    result = _table().lookup('HGNC:1100')
    assert result is not None
    entity = _entity(result, 'MONDO:0011450')  # one submitter says Definitive, another Strong
    assert len({s.classification for s in entity.submissions}) > 1
    strongest = max(entity.submissions, key=lambda s: gene_disease_validity.rank(s.classification))
    assert entity.classification == strongest.classification
    assert len({s.submitter for s in entity.submissions}) == len(entity.submissions)


def test_entities_are_not_reduced_across_the_gene() -> None:
    result = _table().lookup('HGNC:16636')  # a gene whose entities are curated at differing strengths
    assert result is not None
    assert len({entity.classification for entity in result.entities}) > 1


def test_the_same_term_under_two_modes_is_two_entities() -> None:
    table = gencc.GenCC.from_bytes(
        _tsv(
            _REQUIRED,
            _row('MONDO:1', _AD, 'Definitive', 'Submitter X'),
            _row('MONDO:1', _AR, 'Limited', 'Submitter Y'),
        )
    )
    result = table.lookup('HGNC:9999')
    assert result is not None
    assert {(e.disease_curie, e.moi_curie, e.classification) for e in result.entities} == {
        ('MONDO:1', _AD, 'Definitive'),
        ('MONDO:1', _AR, 'Limited'),
    }


# Strongest -> weakest; `Supportive` sits between `Moderate` and `Limited`, the two
# contradicted tiers carry the ` Evidence` suffix, `No Known Disease Relationship` is weakest.
_ORDER = [
    'Definitive', 'Strong', 'Moderate', 'Supportive', 'Limited',
    'Disputed Evidence', 'Refuted Evidence', 'No Known Disease Relationship',
]  # fmt: skip


@pytest.mark.parametrize(('stronger', 'weaker'), list(itertools.pairwise(_ORDER)))
def test_adjacent_tier_ordering_within_one_entity(stronger: str, weaker: str) -> None:
    table = gencc.GenCC.from_bytes(
        _tsv(
            _REQUIRED,
            _row('MONDO:1', _AD, weaker, 'Submitter X'),
            _row('MONDO:1', _AD, stronger, 'Submitter Y'),
        )
    )
    result = table.lookup('HGNC:9999')
    assert result is not None
    assert [entity.classification for entity in result.entities] == [stronger]


def test_mechanism_notes_ride_with_the_submission_that_carries_them() -> None:
    result = _table().lookup('HGNC:1100')
    assert result is not None
    entity = _entity(result, 'MONDO:0700268')
    noted = [s for s in entity.submissions if s.mechanism_note]
    assert noted
    assert any('loss-of-function' in s.mechanism_note for s in noted)
    assert any('\n' in s.mechanism_note for s in noted)  # embedded-newline TSV field
    # An entity whose submitters filed no note carries none, rather than borrowing another entity's.
    assert not any(s.mechanism_note for s in _entity(result, 'MONDO:0054748').submissions)


def test_absent_gene_is_none() -> None:
    assert _table().lookup('HGNC:404040') is None


def test_case_insensitive() -> None:
    assert _table().lookup('hgnc:1100') is not None


@pytest.mark.parametrize('dropped', ['classification_title', 'moi_curie', 'moi_title'])
def test_missing_required_column_raises_value_error(dropped: str) -> None:
    header = [col for col in _REQUIRED if col != dropped]
    row = [
        value for col, value in zip(_REQUIRED, _row('MONDO:1', _AD, 'Definitive', 'X'), strict=True) if col != dropped
    ]
    with pytest.raises(ValueError, match='missing columns'):
        gencc.GenCC.from_bytes(_tsv(header, row))


def test_empty_export_raises_value_error() -> None:
    with pytest.raises(ValueError, match='no submission rows'):
        gencc.GenCC.from_bytes(_tsv(_REQUIRED))


def test_unknown_classification_raises_value_error() -> None:
    table = gencc.GenCC.from_bytes(_tsv(_REQUIRED, _row('MONDO:1', _AD, 'Bogus', 'Ambry')))
    with pytest.raises(ValueError, match='unknown gene-disease validity classification'):
        table.lookup('HGNC:9999')
