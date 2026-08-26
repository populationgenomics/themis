"""ClinGen gene-validity table: preamble parsing, HGNC-id keying, and per-entity curations.

The recorded fixture is a real gene-validity download trimmed to genes that span the classification
tiers (BRCA1 plus single- and mixed-tier genes). Parsing is exercised from bytes; no network.
"""

from __future__ import annotations

import csv
import io
import pathlib

import pytest

from themis.services.evidence.upstreams import clingen_validity
from themis.svcv4 import gene_disease_validity

_FIXTURE = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'clingen_validity.csv'


def _csv(*data_rows: list[str]) -> bytes:
    header = [
        'GENE SYMBOL', 'GENE ID (HGNC)', 'DISEASE LABEL', 'DISEASE ID (MONDO)', 'MOI',
        'SOP', 'CLASSIFICATION', 'ONLINE REPORT', 'CLASSIFICATION DATE', 'GCEP',
    ]  # fmt: skip
    rule = ['+++'] * len(header)
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
    writer.writerow(['CLINGEN GENE DISEASE VALIDITY CURATIONS'])
    writer.writerow(['FILE CREATED: 2026-07-24'])
    writer.writerow(['WEBPAGE: https://search.clinicalgenome.org/kb/gene-validity'])
    writer.writerow(rule)
    writer.writerow(header)
    writer.writerow(rule)
    writer.writerows(data_rows)
    return buffer.getvalue().encode()


def _table() -> clingen_validity.ClinGenValidity:
    return clingen_validity.ClinGenValidity.from_bytes(_FIXTURE.read_bytes())


def test_lookup_carries_the_source_stamp() -> None:
    result = _table().lookup('HGNC:1100')  # BRCA1
    assert result is not None
    assert result.source == 'ClinGen Gene Validity'
    assert result.dataset_versions == ('2026-07-24',)
    assert result.query == 'HGNC:1100'


def test_lookup_is_case_insensitive() -> None:
    assert _table().lookup('hgnc:1100') is not None


@pytest.mark.parametrize('hgnc_id', ['HGNC:1100', 'HGNC:47', 'HGNC:20091', 'HGNC:143', 'HGNC:118'])
def test_every_curated_row_is_returned_as_its_own_entity(hgnc_id: str) -> None:
    # Each row is a (term, mode) ClinGen curates; a lookup that dropped or merged one would answer a
    # gene whose entities it had already chosen among.
    result = _table().lookup(hgnc_id)
    assert result is not None
    rows = result.raw['rows']
    assert isinstance(rows, list)
    assert {(c.mondo_id, c.moi, c.classification) for c in result.curations} == {
        (row['DISEASE ID (MONDO)'], row['MOI'], row['CLASSIFICATION']) for row in rows
    }


def test_a_curation_carries_the_entity_key_and_a_gateable_classification() -> None:
    result = _table().lookup('HGNC:1100')
    assert result is not None
    # BRCA1 is curated for two entities differing in both term and mode; neither shadows the other.
    assert len({(c.mondo_id, c.moi) for c in result.curations}) == len(result.curations) > 1
    for curation in result.curations:
        assert curation.mondo_id.startswith('MONDO:')
        assert curation.disease_label
        assert gene_disease_validity.gate_level(curation.classification)


def test_a_gene_curated_at_several_tiers_keeps_them_all() -> None:
    result = _table().lookup('HGNC:143')  # ACTC1: Definitive + Moderate + No Known Disease Relationship
    assert result is not None
    assert len({c.classification for c in result.curations}) > 1


def test_the_same_term_curated_under_two_modes_stays_two_entities() -> None:
    result = _table().lookup('HGNC:118')  # ACO2: mitochondrial disease curated AD and AR
    assert result is not None
    by_term: dict[str, set[str]] = {}
    for curation in result.curations:
        by_term.setdefault(curation.mondo_id, set()).add(curation.moi)
    assert any(len(modes) > 1 for modes in by_term.values())


def test_absent_gene_is_none() -> None:
    assert _table().lookup('HGNC:404040') is None


def test_missing_header_raises_value_error() -> None:
    with pytest.raises(ValueError, match='FILE CREATED'):
        clingen_validity.ClinGenValidity.from_bytes(b'"nonsense","payload"\n"1","2"\n')


def test_unknown_classification_raises_value_error() -> None:
    body = _csv(['TEST', 'HGNC:1', 'disease', 'MONDO:1', 'AD', 'SOP1', 'Bogus', 'url', 'date', 'gcep'])
    table = clingen_validity.ClinGenValidity.from_bytes(body)
    with pytest.raises(ValueError, match='unknown gene-disease validity classification'):
        table.lookup('HGNC:1')
