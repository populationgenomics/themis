"""Tests for lifting an rpc response's releases onto a library value."""

from __future__ import annotations

import pytest

from themis.evidence.models import evidence_pb2
from themis.svcv4 import provenance


def _record(source: str, *versions: str) -> evidence_pb2.Provenance:
    return evidence_pb2.Provenance(source=source, dataset_versions=list(versions), query='q')


def test_every_release_of_every_record_is_carried() -> None:
    releases = provenance.releases_of([_record('gnomAD GraphQL', 'gnomad_r4', 'GRCh38'), _record('NCBI ClinVar', 'w1')])
    assert releases == (
        provenance.Release('gnomAD GraphQL', 'gnomad_r4'),
        provenance.Release('gnomAD GraphQL', 'GRCh38'),
        provenance.Release('NCBI ClinVar', 'w1'),
    )


def test_the_same_release_stated_twice_is_carried_once() -> None:
    # Two requests to one upstream stamp one record each, both naming the release they ran against.
    releases = provenance.releases_of([_record('NCBI ClinVar', 'w1'), _record('NCBI ClinVar', 'w1')])
    assert releases == (provenance.Release('NCBI ClinVar', 'w1'),)


def test_a_response_with_no_provenance_is_refused() -> None:
    with pytest.raises(ValueError, match='no provenance'):
        provenance.releases_of([])


@pytest.mark.parametrize(
    ('record', 'match'),
    [
        (evidence_pb2.Provenance(source='gnomAD GraphQL'), 'names no release'),
        (evidence_pb2.Provenance(dataset_versions=['gnomad_r4']), 'names no source'),
        (evidence_pb2.Provenance(source='gnomAD GraphQL', dataset_versions=['  ']), 'empty release'),
    ],
)
def test_a_partial_release_list_is_refused(record: evidence_pb2.Provenance, match: str) -> None:
    # The contract states the list is neither optional nor partial: a partial one reads as complete.
    with pytest.raises(ValueError, match=match):
        provenance.releases_of([record])


def test_union_keeps_first_statement_order() -> None:
    gnomad = provenance.Release('gnomAD GraphQL', 'gnomad_r4')
    clinvar = provenance.Release('NCBI ClinVar', 'w1')
    assert provenance.union((gnomad, clinvar), (clinvar,), ()) == (gnomad, clinvar)
