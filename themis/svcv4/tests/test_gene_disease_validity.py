"""The curators' validity vocabulary: its strength order, and the gate level each classification reads as."""

from __future__ import annotations

import itertools
from collections.abc import Callable

import pytest

from themis.rpc import gene_disease_pb2
from themis.svcv4 import gene_disease_validity, reference, scoring

# Strongest to weakest across both curators; `Supportive` is GenCC-only and sits below `Moderate`,
# and the two contradicted tiers carry GenCC's ` Evidence` spelling alongside ClinGen's.
_ORDER = [
    'Definitive', 'Strong', 'Moderate', 'Supportive', 'Limited',
    'Disputed', 'Refuted', 'No Known Disease Relationship',
]  # fmt: skip


@pytest.mark.parametrize(('stronger', 'weaker'), list(itertools.pairwise(_ORDER)))
def test_adjacent_classifications_rank_in_order(stronger: str, weaker: str) -> None:
    assert gene_disease_validity.rank(stronger) > gene_disease_validity.rank(weaker)


@pytest.mark.parametrize(('clingen', 'gencc'), [('Disputed', 'Disputed Evidence'), ('Refuted', 'Refuted Evidence')])
def test_the_two_spellings_of_a_contradicted_tier_rank_and_gate_alike(clingen: str, gencc: str) -> None:
    assert gene_disease_validity.rank(clingen) == gene_disease_validity.rank(gencc)
    assert gene_disease_validity.gate_level(clingen) == gene_disease_validity.gate_level(gencc)


def test_every_classification_gates_at_a_level_the_reference_carries(ref: reference.Reference) -> None:
    # The map and the gate table are edited independently, so the library holds itself to the join.
    gene_disease_validity.check_gate_levels(ref)
    for classification in gene_disease_validity.CLASSIFICATIONS:
        assert gene_disease_validity.gate_level(classification) in ref.gate


def test_every_classification_below_moderate_caps_a_likely_pathogenic(ref: reference.Reference) -> None:
    # What the map is for. SM18 caps at LP from Moderate down, so a classification ranking below it
    # must reach a level that caps — `Supportive` and `No Known Disease Relationship` included,
    # which the enum names no member after and a caller could not map by eye.
    moderate = gene_disease_validity.rank('Moderate')
    weaker = [c for c in gene_disease_validity.CLASSIFICATIONS if gene_disease_validity.rank(c) < moderate]
    assert weaker  # non-empty rules out a vacuous pass
    for classification in weaker:
        assert scoring.apply_gate(ref, 'LP', gene_disease_validity.gate_level(classification)).capped


def test_a_stronger_classification_never_gates_more_restrictively(ref: reference.Reference) -> None:
    # The rank and the gate level are two columns of one table and could disagree: `Supportive`
    # gating below `Limited` would cap a class the weaker classification permits.
    permitted = {
        name: len(ref.gate[gene_disease_validity.gate_level(name)].allows)
        for name in gene_disease_validity.CLASSIFICATIONS
    }
    ranked = sorted(gene_disease_validity.CLASSIFICATIONS, key=gene_disease_validity.rank, reverse=True)
    for stronger, weaker in itertools.pairwise(ranked):
        assert permitted[stronger] >= permitted[weaker]


@pytest.mark.parametrize(
    'lookup',
    [gene_disease_validity.validate, gene_disease_validity.rank, gene_disease_validity.gate_level],
)
def test_a_classification_outside_the_vocabulary_raises(lookup: Callable[[str], object]) -> None:
    with pytest.raises(ValueError, match='unknown gene-disease validity classification'):
        lookup('Probably Fine')


def test_a_level_the_reference_does_not_gate_is_refused(ref: reference.Reference) -> None:
    dropped = gene_disease_pb2.GATE_LEVEL_LIMITED
    stale = reference.Reference(**{**vars(ref), 'gate': {k: v for k, v in ref.gate.items() if k != dropped}})
    with pytest.raises(reference.ReferenceDataError, match='GATE_LEVEL_LIMITED'):
        gene_disease_validity.check_gate_levels(stale)
