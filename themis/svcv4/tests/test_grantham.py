"""Tests for the Grantham matrix and the four MIS_INF sub-rules."""

from __future__ import annotations

import decimal
import itertools

import pytest

from themis.rpc import clinvar_pb2
from themis.svcv4 import grantham, reference
from themis.svcv4.tests import responses

D = decimal.Decimal


def test_matrix_anchors() -> None:
    assert grantham.distance('C', 'W') == 215  # published maximum
    assert grantham.distance('L', 'I') == 5  # published minimum
    assert grantham.distance('S', 'R') == 110
    assert grantham.distance('D', 'E') == 45


def test_matrix_is_symmetric_with_zero_diagonal() -> None:
    codes = grantham._ORDER
    for a in codes:
        assert grantham.distance(a, a) == 0
    for a, b in itertools.combinations(codes, 2):
        assert grantham.distance(a, b) == grantham.distance(b, a)


def test_matrix_covers_all_190_pairs() -> None:
    assert len(grantham._MATRIX) == 190
    distances = [grantham.distance(a, b) for a, b in itertools.combinations(grantham._ORDER, 2)]
    assert min(distances) == 5
    assert max(distances) == 215


@pytest.mark.parametrize('rejected', ['X', '', '   ', 'SR', 'DEM', 'Glycine', 'Xaa', 'Ter'])
def test_matrix_rejects_anything_but_a_standard_residue(rejected: str) -> None:
    # 'SR'/'DEM' are runs of the canonical order and '' is a substring of it: a membership test
    # against the order *string* would let all three reach the matrix and raise KeyError instead.
    with pytest.raises(ValueError, match='amino-acid'):
        grantham.distance(rejected, 'A')


@pytest.mark.parametrize('spelling', [('Gly', 'Arg'), ('GLY', 'ARG'), ('gly', 'arg'), ('G', 'Arg'), ('g', 'R')])
def test_one_and_three_letter_codes_name_the_same_pair(spelling: tuple[str, str]) -> None:
    # Protein HGVS renders residues three-letter (`p.(Gly1166Arg)`), so both spellings reach here.
    assert grantham.distance(*spelling) == grantham.distance('G', 'R')


def test_three_letter_table_covers_every_residue_exactly_once() -> None:
    # A gap or a duplicate would leave a residue unreachable through its three-letter spelling.
    assert sorted(grantham._THREE_LETTER.values()) == sorted(grantham._ORDER)


def test_rejection_names_the_accepted_forms() -> None:
    with pytest.raises(ValueError, match='three-letter'):
        grantham.distance('Glycine', 'Arg')


# --- the four MIS_INF sub-rules ------------------------------------------------------------------


def _same_aa(classification: str) -> grantham.InformativeMissense:
    return grantham.InformativeMissense(classification=classification, same_amino_acid=True)


def _distinct(classification: str, grantham_distance: int) -> grantham.InformativeMissense:
    return grantham.InformativeMissense(
        classification=classification, same_amino_acid=False, grantham_distance=grantham_distance
    )


def test_subrule1_same_amino_acid_pathogenic(ref: reference.Reference) -> None:
    # Same-codon strong weights: +4 first P, +2 each additional.
    assert grantham.mis_inf_points(ref, 100, (_same_aa('P'),)) == D('4')
    assert grantham.mis_inf_points(ref, 100, (_same_aa('P'), _same_aa('LP'))) == D('6')


def test_subrule2_distinct_pathogenic_grantham_below_vbc(ref: reference.Reference) -> None:
    # Distinct-AA P with Grantham <= VBC's -> +2; a larger Grantham fails the comparison -> 0.
    assert grantham.mis_inf_points(ref, 100, (_distinct('P', 60),)) == D('2')
    assert grantham.mis_inf_points(ref, 100, (_distinct('P', 150),)) == D('0')


def test_subrule3_distinct_benign_grantham_above_vbc(ref: reference.Reference) -> None:
    assert grantham.mis_inf_points(ref, 100, (_distinct('B', 150),)) == D('-2')
    assert grantham.mis_inf_points(ref, 100, (_distinct('B', 60),)) == D('0')


def test_subrule4_same_amino_acid_benign(ref: reference.Reference) -> None:
    assert grantham.mis_inf_points(ref, 100, (_same_aa('B'),)) == D('-4')


def test_subrules_sum_and_cap(ref: reference.Reference) -> None:
    # Four same-AA P (+4 +2 +2 +2 = +10) capped to the MIS_INF ceiling of +8.
    assert grantham.mis_inf_points(ref, 100, tuple(_same_aa('P') for _ in range(4))) == D('8')


def test_vus_informative_scores_zero(ref: reference.Reference) -> None:
    assert grantham.mis_inf_points(ref, 100, (_same_aa('VUS'),)) == D('0')


def test_motif_rule_awards_two_when_no_other_informative(ref: reference.Reference) -> None:
    assert grantham.mis_inf_points(ref, 100, (), motif_qualifies=True) == D('2')


def test_motif_rule_voided_by_any_informative(ref: reference.Reference) -> None:
    # A benign informative variant voids the motif award (no net +2 on top of the -2).
    assert grantham.mis_inf_points(ref, 100, (_distinct('B', 150),), motif_qualifies=True) == D('-2')


@pytest.mark.parametrize(
    ('expression', 'predicted'),
    [
        ('NP_001035957.1:p.Gly1166Arg', False),
        ('p.Gly1166Arg', False),
        ('p.(Gly1166Arg)', True),
        ('p.G1166R', False),
        ('p.(gly1166ARG)', True),
    ],
)
def test_a_substitution_is_read_in_either_form(expression: str, predicted: bool) -> None:
    parsed = grantham.substitution(expression)
    assert (parsed.reference_aa, parsed.codon, parsed.variant_aa) == ('G', 1166, 'R')
    assert parsed.predicted is predicted
    assert parsed.distance == grantham.distance('Gly', 'Arg')


@pytest.mark.parametrize(
    'expression',
    [
        'p.Gly1166=',  # synonymous
        'p.Gly1166Ter',  # nonsense
        'p.Gly1166*',
        'p.(Gly1166fs)',  # frameshift
        'p.Gly1166del',
        'p.Gly1166_Arg1167insGly',
        'p.Met1ext-5',
        'p.(Gly1166Arg',  # unbalanced
        'NM_000123.4:c.3496G>C',  # a coding expression, not a protein one
        '',
    ],
)
def test_anything_that_is_not_a_single_substitution_is_refused(expression: str) -> None:
    # Each of these has a Grantham comparison that looks computable and is not: the rules compare
    # two residues at one codon, and none of these names two.
    with pytest.raises(ValueError, match=r'substitution|nonsense change|standard amino-acid'):
        grantham.substitution(expression)


def _record(classification: str) -> clinvar_pb2.ClinVarRecord:
    return responses.clinvar_record('VCV1', classification=classification)


def test_an_informative_variant_at_the_same_codon_derives_its_own_comparison() -> None:
    informative = grantham.informative_from_record(
        _record('Likely pathogenic'),
        protein_change=grantham.substitution('p.Gly1166Ala'),
        vbc=grantham.substitution('p.Gly1166Arg'),
    )
    assert informative.classification == 'LP'
    assert informative.same_amino_acid is False
    assert informative.grantham_distance == grantham.distance('Gly', 'Ala')


def test_the_same_residue_reached_by_another_nucleotide_is_the_same_amino_acid() -> None:
    informative = grantham.informative_from_record(
        _record('Pathogenic'),
        protein_change=grantham.substitution('p.Gly1166Arg'),
        vbc=grantham.substitution('p.(Gly1166Arg)'),
    )
    assert informative.same_amino_acid is True


def test_a_record_at_another_codon_is_not_an_informative_variant_of_this_one() -> None:
    with pytest.raises(ValueError, match='within one codon'):
        grantham.informative_from_record(
            _record('Pathogenic'),
            protein_change=grantham.substitution('p.Gly1200Arg'),
            vbc=grantham.substitution('p.Gly1166Arg'),
        )


def test_two_expressions_disagreeing_about_the_reference_residue_are_refused() -> None:
    with pytest.raises(ValueError, match='different proteins'):
        grantham.informative_from_record(
            _record('Pathogenic'),
            protein_change=grantham.substitution('p.Ala1166Arg'),
            vbc=grantham.substitution('p.Gly1166Arg'),
        )


def test_a_record_the_rules_score_at_no_strength_is_refused() -> None:
    # A conflicting record is left out of the informative set, not counted at zero.
    with pytest.raises(ValueError, match='no strength'):
        grantham.informative_from_record(
            _record('Conflicting classifications of pathogenicity'),
            protein_change=grantham.substitution('p.Gly1166Ala'),
            vbc=grantham.substitution('p.Gly1166Arg'),
        )
