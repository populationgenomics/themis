"""Tests for the combining engine — the matrix, INF-after-matrix, caps, max-path, band, gate."""

from __future__ import annotations

import decimal
import typing

import pytest

from themis.rpc import gene_disease_pb2
from themis.svcv4 import reference, scoring

D = decimal.Decimal


# --- the mechanism x exon matrix (SM18, decision-tree grid) ---------------------------------------


@pytest.mark.parametrize(
    ('mechanism', 'exon', 'expected'),
    [
        (scoring.MechanismLevel.ESTABLISHED, scoring.ExonRelevance.ALL, '1.0'),
        (scoring.MechanismLevel.LIKELY, scoring.ExonRelevance.ALL, '0.5'),
        (scoring.MechanismLevel.SUSPECTED, scoring.ExonRelevance.ALL, '0.25'),
        (scoring.MechanismLevel.UNCERTAIN, scoring.ExonRelevance.ALL, '0'),
        (scoring.MechanismLevel.ESTABLISHED, scoring.ExonRelevance.MOST, '0.5'),
        (scoring.MechanismLevel.LIKELY, scoring.ExonRelevance.MOST, '0.25'),
        (scoring.MechanismLevel.SUSPECTED, scoring.ExonRelevance.MOST, '0'),  # SM18 Figure 1 states 0%
        (scoring.MechanismLevel.ESTABLISHED, scoring.ExonRelevance.FEW, '0'),
    ],
)
def test_matrix_grid(ref: reference.Reference, mechanism, exon, expected: str) -> None:  # noqa: ANN001
    got = scoring.matrix_multiplier(ref, scoring.Scaling.MECHANISM_AND_EXON, mechanism, exon)
    assert got == D(expected)


def test_matrix_exon_only_ignores_mechanism(ref: reference.Reference) -> None:
    # The missense amino-acid path scales by exon relevance alone (predictors capture LoF and GoF).
    got = scoring.matrix_multiplier(ref, scoring.Scaling.EXON_ONLY, None, scoring.ExonRelevance.MOST)
    assert got == D('0.5')


def test_matrix_none_is_identity(ref: reference.Reference) -> None:
    assert scoring.matrix_multiplier(ref, scoring.Scaling.NONE, None, None) == D('1')


def test_matrix_requires_levels(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match='exon relevance required'):
        scoring.matrix_multiplier(ref, scoring.Scaling.EXON_ONLY, None, None)


# --- the SM18 §17 waiver of the exon-relevance reduction -----------------------------------------


_WAIVING_VARIANT = 'NM_000000.1:c.100G>A'


def _expert_classified(
    variant: str = _WAIVING_VARIANT,
    stars: int | None = scoring.EXPERT_PANEL_REVIEW_STARS,
    classification: str = scoring.WAIVING_CLASSIFICATION,
) -> scoring.ExonPathogenicVariant:
    return scoring.ExonPathogenicVariant(
        variant=variant,
        classification=classification,
        basis=scoring.PathogenicVariantBasis.EXPERT_CLASSIFIED,
        review_stars=stars,
    )


def _waiver() -> scoring.ExonRelevanceWaiver:
    """A minimal well-formed waiver: one expert-classified variant at the expert-panel rung."""
    return scoring.ExonRelevanceWaiver(variants=(_expert_classified(),))


@pytest.mark.parametrize('tier', list(scoring.ExonRelevance))
def test_the_waiver_defeats_whatever_reduction_the_tier_would_have_applied(
    ref: reference.Reference, tier: scoring.ExonRelevance
) -> None:
    """§17 waives the reduction; it does not argue a tier, so no tier's factor survives it."""
    reduced = scoring.matrix_multiplier(ref, scoring.Scaling.EXON_ONLY, None, tier)
    waived = scoring.matrix_multiplier(ref, scoring.Scaling.EXON_ONLY, None, _waiver())
    assert waived == D('1')
    assert waived >= reduced


def test_the_waiver_is_not_the_all_tier_in_the_trail() -> None:
    """Both leave the multiplier at 1; only one of them is a claim about transcript abundance."""
    tier_note = scoring.matrix_note(scoring.Scaling.EXON_ONLY, scoring.ExonRelevance.ALL, D('1'))
    waiver = _waiver()
    waived_note = scoring.matrix_note(scoring.Scaling.EXON_ONLY, waiver, D('1'))
    assert waived_note != tier_note
    assert waiver.variants[0].variant in waived_note
    assert '§17' in waived_note


@pytest.mark.parametrize(
    'scaling',
    [scoring.Scaling.MECHANISM_AND_EXON, scoring.Scaling.MECHANISM_ONLY, scoring.Scaling.NONE],
)
def test_the_waiver_is_refused_off_the_path_sm18_writes_it_for(
    ref: reference.Reference, scaling: scoring.Scaling
) -> None:
    # §17 is written for MIS_PRD, the one path the exon axis scales alone; elsewhere it has no scope.
    with pytest.raises(ValueError, match='no scope'):
        scoring.matrix_multiplier(ref, scaling, scoring.MechanismLevel.ESTABLISHED, _waiver())


def test_a_waiver_with_no_established_variant_is_refused() -> None:
    with pytest.raises(ValueError, match='no antecedent'):
        scoring.ExonRelevanceWaiver(variants=())


@pytest.mark.parametrize('stars', [None, scoring.EXPERT_PANEL_REVIEW_STARS - 1])
def test_an_expert_classification_below_the_expert_panel_rung_is_refused(stars: int | None) -> None:
    with pytest.raises(ValueError, match='expert panel'):
        _expert_classified(stars=stars)


def test_a_well_established_claim_naming_nothing_that_establishes_it_is_refused() -> None:
    with pytest.raises(ValueError, match='well-established'):
        scoring.ExonPathogenicVariant(
            variant=_WAIVING_VARIANT,
            classification=scoring.WAIVING_CLASSIFICATION,
            basis=scoring.PathogenicVariantBasis.WELL_ESTABLISHED,
        )


def test_a_waiving_variant_the_trail_could_not_identify_is_refused() -> None:
    with pytest.raises(ValueError, match='must be named'):
        _expert_classified(variant='  ')


@pytest.mark.parametrize('classification', ['LP', 'B', 'LB', 'VUS'])
def test_a_variant_the_exon_harbours_that_is_not_pathogenic_funds_no_waiver(classification: str) -> None:
    """Establishing *a* classification is not §17's antecedent; establishing a pathogenic one is.

    An expert panel classifies benign variants too, so a rung check that never reads the
    classification defeats the reduction on a record asserting the opposite of what §17 rests on.
    """
    with pytest.raises(ValueError, match='SM18 §17'):
        _expert_classified(classification=classification)


def test_apply_matrix_scales_positive_only() -> None:
    assert scoring.apply_matrix(D('6'), D('0.5')) == D('3.0')
    assert scoring.apply_matrix(D('-1'), D('0.5')) == D('-1')  # negatives pass through unscaled
    assert scoring.apply_matrix(D('0'), D('0.5')) == D('0')


# --- informative-variant tiers (SM19) ------------------------------------------------------------


@pytest.mark.parametrize(
    ('classifications', 'strong', 'expected'),
    [
        (('P',), False, '2'),  # first P
        (('P', 'P', 'P'), False, '4'),  # +2 first, +1 each additional
        (('LP', 'LP'), False, '2'),  # only-LP: +1 each
        (('P', 'LP'), False, '3'),  # +2 first P, +1 additional
        (('B', 'B'), False, '-3'),  # benign mirror: -2 first, -1 additional
        (('P', 'B'), False, '0'),  # +2 and -2 sum
        (('P',), True, '4'),  # same-codon strong: +4 first P
        (('P', 'P'), True, '6'),  # +4 first, +2 additional
        (('LP', 'LP'), True, '4'),  # strong only-LP: +2 each
        (('B',), True, '-4'),
    ],
)
def test_informative_points(classifications, strong: bool, expected: str) -> None:  # noqa: ANN001
    assert scoring.informative_points(classifications, strong=strong) == D(expected)


def test_informative_points_rejects_unknown_token() -> None:
    with pytest.raises(ValueError, match='unrecognised'):
        scoring.informative_points(('VUS',))


# --- path scoring: matrix on positive PRD, INF after and exempt, caps -----------------------------


def _nul_path(ref: reference.Reference, mechanism, exon, inf, *, inf_cap=None) -> scoring.PathInput:  # noqa: ANN001
    return scoring.PathInput(
        label='yellow NMD',
        parent_code='NUL_',
        prd_initial=D('6'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
        mechanism=mechanism,
        exon=exon,
        inf=inf,
        inf_cap=inf_cap,
        parent_cap=(ref.category_cap('NUL_PFD').low, ref.category_cap('NUL_PFD').high),
    )


def test_an_inf_sum_above_its_cap_reports_the_raw_and_the_adjustment(ref: reference.Reference) -> None:
    """A cap that stands in for the value it bounded leaves nothing in the trail recording the loss.

    `informative_points` returns an uncapped sum, so a caller reaches this legitimately; a line whose
    raw equals its points reads as a sum that was never reduced.
    """
    cap = ref.concept_cap('NUL_INF')
    raw = cap.high + D('4')
    path = _nul_path(
        ref, scoring.MechanismLevel.ESTABLISHED, scoring.ExonRelevance.ALL, raw, inf_cap=(cap.low, cap.high)
    )
    result = scoring.score_path(ref, path)
    line = next(c for c in result.contributions if c.label == 'NUL_INF')
    assert line.raw_points == raw
    assert line.points == cap.high
    assert 'cap' in line.note
    # The bounded value is what the column carries, so the trail still explains its own total.
    assert sum((c.points for c in result.contributions), D('0')) == result.total


def test_matrix_applies_to_positive_prd(ref: reference.Reference) -> None:
    path = _nul_path(ref, scoring.MechanismLevel.LIKELY, scoring.ExonRelevance.ALL, D('0'))
    result = scoring.score_path(ref, path)
    assert result.raw_prd == D('6')
    assert result.adjusted_prd == D('3.0')  # +6 x 0.5
    assert result.total == D('3.0')


def test_inf_added_after_matrix_and_not_reduced(ref: reference.Reference) -> None:
    # +6 initial x Suspected/All (0.25) = 1.5; +2 INF is added after and NOT scaled by the matrix.
    path = _nul_path(ref, scoring.MechanismLevel.SUSPECTED, scoring.ExonRelevance.ALL, D('2'))
    result = scoring.score_path(ref, path)
    assert result.adjusted_prd == D('1.50')
    assert result.total == D('3.50')  # 1.5 + 2.0, not (1.5 + 2.0) x 0.25


def test_fractional_carry_forward(ref: reference.Reference) -> None:
    # SM18: 3.0 x 0.25 = 0.75 may be carried forward.
    path = scoring.PathInput(
        label='p',
        parent_code='CDS_',
        prd_initial=D('3'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
        mechanism=scoring.MechanismLevel.SUSPECTED,
        exon=scoring.ExonRelevance.ALL,
    )
    assert scoring.score_path(ref, path).total == D('0.75')


def test_combine_cap_clamps_prd_plus_fxn(ref: reference.Reference) -> None:
    # MIS_PRD +4 (exon All) + MIS_FXN +4 = 8, combine-capped at +6 (SM6).
    path = scoring.PathInput(
        label='amino-acid',
        parent_code='MIS_',
        prd_initial=D('4'),
        scaling=scoring.Scaling.EXON_ONLY,
        exon=scoring.ExonRelevance.ALL,
        combine_stages=(
            scoring.CombineStage(
                label='MIS_PRD + MIS_FXN', items=(scoring.PointItem('MIS_FXN', D('4')),), cap=(D('-8'), D('6'))
            ),
        ),
    )
    assert scoring.score_path(ref, path).total == D('6')


def _staged(*stages: scoring.CombineStage) -> scoring.PathInput:
    return scoring.PathInput(label='splice', parent_code='SPL_', prd_initial=D('3'), combine_stages=stages)


def test_each_combine_stage_sees_the_previous_stages_capped_subtotal(ref: reference.Reference) -> None:
    # The trees state the splice caps as two boxes in sequence ("Add SPL_PRD + SPL_SPA, cap ..." then
    # "Add (SPL_PRD + SPL_SPA) to SPL_FXN, cap ..."), so the first ceiling has to bind before the
    # second layer is added. Summing the items under one cap instead would give 3 + 3 - 1 = 5.
    path = _staged(
        scoring.CombineStage(label='first', items=(scoring.PointItem('SPL_SPA', D('3')),), cap=(D('0'), D('4'))),
        scoring.CombineStage(label='second', items=(scoring.PointItem('SPL_FXN', D('-1')),), cap=(D('-8'), D('9'))),
    )
    assert scoring.score_path(ref, path).total == D('3')  # (3 + 3 -> 4) - 1


def test_a_stage_caps_the_running_subtotal_even_with_no_items(ref: reference.Reference) -> None:
    # A combine box on the tree applies whether or not the analyst has that assay; an absent
    # SPL_SPA must not turn its cap off.
    assert scoring.score_path(
        ref, _staged(scoring.CombineStage(label='first', items=(), cap=(D('0'), D('2'))))
    ).total == D('2')


def test_an_item_note_reaches_the_audit_trail(ref: reference.Reference) -> None:
    path = _staged(
        scoring.CombineStage(
            label='first', items=(scoring.PointItem('SPL_SPA', D('1'), note='substantial'),), cap=(D('-8'), D('9'))
        )
    )
    trail = {c.label: c.note for c in scoring.score_path(ref, path).contributions}
    assert trail['SPL_SPA'] == 'substantial'


def test_parent_cap_clamps_total(ref: reference.Reference) -> None:
    path = _nul_path(ref, scoring.MechanismLevel.ESTABLISHED, scoring.ExonRelevance.ALL, D('8'))
    # +6 x 1.0 = 6, + 8 INF = 14, parent-capped at +10.
    assert scoring.score_path(ref, path).total == D('10.0')


# --- missense vs splice max-path (SM6 Table 1) ---------------------------------------------------


def _result(label: str, total: str) -> scoring.PathResult:
    return scoring.PathResult(
        label=label,
        parent_code='X',
        raw_prd=D('0'),
        adjusted_prd=D('0'),
        multiplier=D('1'),
        total=D(total),
        contributions=(),
    )


@pytest.mark.parametrize(
    ('mis', 'spl', 'winner'),
    [
        ('3', '-2', 'MIS'),  # negative splice -> amino-acid
        ('3', '5', 'SPL'),  # both positive -> more positive
        ('5', '3', 'MIS'),
        ('4', '4', 'MIS'),  # tie -> amino-acid
        ('-3', '0', 'SPL'),  # splice not negative, more positive than amino-acid
    ],
)
def test_select_path(mis: str, spl: str, winner: str) -> None:
    selected, alternate = scoring.select_path(_result('MIS', mis), _result('SPL', spl))
    assert selected.label == winner
    assert alternate.label == ('SPL' if winner == 'MIS' else 'MIS')


# --- band mapping + VUS sub-bands ----------------------------------------------------------------


@pytest.mark.parametrize(
    ('total', 'band', 'subband'),
    [
        ('-5', 'B', None),
        ('-2', 'LB', None),
        ('0', 'VUS', 'VUS-low'),
        ('2', 'VUS', 'VUS-mid'),
        ('4', 'VUS', 'VUS-high'),
        ('5.9', 'VUS', 'VUS-high'),
        ('6', 'LP', None),
        ('10', 'P', None),
    ],
)
def test_band_for_total(ref: reference.Reference, total: str, band: str, subband: str | None) -> None:
    assert scoring.band_for_total(ref, D(total)) == (band, subband)


# --- gene-disease-validity gate cap --------------------------------------------------------------


@pytest.mark.parametrize(
    ('band', 'level', 'final', 'capped'),
    [
        ('P', gene_disease_pb2.GATE_LEVEL_DEFINITIVE, 'P', False),
        ('P', gene_disease_pb2.GATE_LEVEL_MODERATE, 'LP', True),  # Moderate caps at LP
        ('LP', gene_disease_pb2.GATE_LEVEL_MODERATE, 'LP', False),
        ('LP', gene_disease_pb2.GATE_LEVEL_LIMITED, 'VUS', True),  # Limited caps at VUS
        ('VUS', gene_disease_pb2.GATE_LEVEL_LIMITED, 'VUS', False),
        ('B', gene_disease_pb2.GATE_LEVEL_LIMITED, 'B', False),  # benign classes pass the gate
        ('P', gene_disease_pb2.GATE_LEVEL_LESS_THAN_LIMITED, 'Variant in Gene of Uncertain Significance', True),
        ('LP', gene_disease_pb2.GATE_LEVEL_DISPUTED_OR_REFUTED, 'Do not report', True),
    ],
)
def test_apply_gate(
    ref: reference.Reference, band: str, level: gene_disease_pb2.GateLevel, final: str, capped: bool
) -> None:
    outcome = scoring.apply_gate(ref, band, level)
    assert (outcome.final_class, outcome.capped) == (final, capped)


@pytest.mark.parametrize('rejected', [gene_disease_pb2.GATE_LEVEL_UNSPECIFIED, 'Supportive'])
def test_apply_gate_takes_a_curated_gate_level_and_nothing_else(ref: reference.Reference, rejected: object) -> None:
    # Two things a code-mode caller can pass: an entity carrying no level, and `Supportive` — a real
    # GenCC classification the gate is not keyed by. Both are refused rather than ranked, and the
    # refusal names the map that turns a classification into a level.
    with pytest.raises(ValueError, match=r'gene_disease_validity\.gate_level'):
        scoring.apply_gate(ref, 'P', typing.cast('gene_disease_pb2.GateLevel', rejected))


@pytest.mark.parametrize('lookalike', [True, 1.0, D('1')])
def test_apply_gate_refuses_a_value_that_merely_equals_a_gate_level(
    ref: reference.Reference, lookalike: object
) -> None:
    # Each equals GATE_LEVEL_DEFINITIVE, the most permissive level, and this library's own points
    # are Decimals, so one of them is a keystroke away.
    with pytest.raises(ValueError, match=r'gene_disease_validity\.gate_level'):
        scoring.apply_gate(ref, 'P', typing.cast('gene_disease_pb2.GateLevel', lookalike))
