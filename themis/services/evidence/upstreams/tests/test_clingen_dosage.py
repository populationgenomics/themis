"""ClinGen gene-dosage table: label->score mapping, HGNC-id keying, absent-vs-zero distinction.

The recorded fixture is a real gene-dosage download trimmed to genes covering every
haploinsufficiency label. Parsing is exercised from bytes; no network.
"""

from __future__ import annotations

import csv
import io
import pathlib

import pytest

from themis.services.evidence.upstreams import clingen_dosage

_FIXTURE = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'clingen_dosage.csv'


def _csv(*data_rows: list[str]) -> bytes:
    header = ['GENE SYMBOL', 'HGNC ID', 'HAPLOINSUFFICIENCY', 'TRIPLOSENSITIVITY', 'ONLINE REPORT', 'DATE']
    rule = ['+++'] * len(header)
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
    writer.writerow(['CLINGEN DOSAGE SENSITIVITY CURATIONS'])
    writer.writerow(['FILE CREATED: 2026-07-24'])
    writer.writerow(['WEBPAGE: https://search.clinicalgenome.org/kb/gene-dosage'])
    writer.writerow(rule)
    writer.writerow(header)
    writer.writerow(rule)
    writer.writerows(data_rows)
    return buffer.getvalue().encode()


def _table() -> clingen_dosage.ClinGenDosage:
    return clingen_dosage.ClinGenDosage.from_bytes(_FIXTURE.read_bytes())


@pytest.mark.parametrize(
    ('hgnc_id', 'expected'),
    [
        ('HGNC:20', 0),  # AARS1: No Evidence for Haploinsufficiency
        ('HGNC:3571', 1),  # ACSL4: Little Evidence
        ('HGNC:6743', 2),  # CAPRIN1: Emerging Evidence
        ('HGNC:1100', 3),  # BRCA1: Sufficient Evidence
        ('HGNC:18149', 30),  # A4GALT: Gene Associated with Autosomal Recessive Phenotype
        ('HGNC:1094', 40),  # BPHL: Dosage Sensitivity Unlikely
    ],
)
def test_haploinsufficiency_score_mapping(hgnc_id: str, expected: int) -> None:
    result = _table().lookup(hgnc_id)
    assert result is not None
    assert result.haploinsufficiency_score == expected


def test_provenance_and_query() -> None:
    result = _table().lookup('HGNC:1100')  # BRCA1
    assert result is not None
    assert result.source == 'ClinGen Dosage Sensitivity'
    assert result.dataset_versions == ('2026-07-24',)
    assert result.query == 'HGNC:1100'
    assert isinstance(result.raw['row'], dict)


def test_absent_gene_is_none_distinct_from_score_zero() -> None:
    table = _table()
    assert table.lookup('HGNC:404040') is None  # absent
    zero = table.lookup('HGNC:20')  # AARS1: present, scored 0
    assert zero is not None
    assert zero.haploinsufficiency_score == 0


def test_case_insensitive() -> None:
    assert _table().lookup('hgnc:1100') is not None


def test_missing_header_raises_value_error() -> None:
    with pytest.raises(ValueError, match='FILE CREATED'):
        clingen_dosage.ClinGenDosage.from_bytes(b'"junk"\n"row"\n')


def test_unknown_label_raises_value_error() -> None:
    body = _csv(
        ['TEST', 'HGNC:1', 'Mild Evidence for Haploinsufficiency', 'No Evidence for Triplosensitivity', 'url', 'date']
    )
    table = clingen_dosage.ClinGenDosage.from_bytes(body)
    with pytest.raises(ValueError, match='unknown ClinGen haploinsufficiency label'):
        table.lookup('HGNC:1')
