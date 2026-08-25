"""Tests for the per-variant-type builders: the caps/scaling/path-structure each bakes from `ref`."""

from __future__ import annotations

import dataclasses
import decimal
import itertools
import typing
from collections.abc import Callable

import pytest

from themis.evidence.models import evidence_pb2
from themis.rpc import gene_disease_pb2
from themis.svcv4 import builders, classify, duplication_tree, frequency, provenance, reference, scoring, splice_tree

D = decimal.Decimal
_ZERO = D('0')
ME = scoring.MechanismLevel.ESTABLISHED
ALL = scoring.ExonRelevance.ALL

_Builder = Callable[..., classify.ClassificationInput]

# The builder that owns each flow; `build_missense` shares the predicted flow with SM12.
_FLOW_BUILDER: dict[splice_tree.SpliceFlow, _Builder] = {
    splice_tree.SpliceFlow.CANONICAL: builders.build_canonical_splice,
    splice_tree.SpliceFlow.PREDICTED: builders.build_intronic_synonymous,
}
_CELLS = tuple(itertools.product(splice_tree.SpliceFlow, splice_tree.SpliceColour))


def _only_path(request: classify.ClassificationInput) -> scoring.PathInput:
    assert len(request.variant_type_paths) == 1
    return request.variant_type_paths[0]


def _cap(range_: reference.CapRange) -> tuple[decimal.Decimal, decimal.Decimal]:
    return range_.low, range_.high


def _evidence(
    flow: splice_tree.SpliceFlow,
    colour: splice_tree.SpliceColour,
    prd: decimal.Decimal | None = None,
    *,
    spl_spa: splice_tree.Proportion | splice_tree.AssayStrength | None = None,
    spl_fxn: decimal.Decimal | None = None,
    spl_inf: decimal.Decimal = _ZERO,
) -> builders.SpliceEvidence:
    """Evidence for one cell, carrying the mechanism/exon calls its scaling requires.

    `prd` defaults to the lowest tier the cell admits, which every cell has by construction.
    """
    cell = splice_tree.cell_for(flow, colour)
    scaled = cell.scaling is not scoring.Scaling.NONE
    return builders.SpliceEvidence(
        colour=colour,
        spl_prd=cell.prd.low if prd is None else prd,
        mechanism=ME if scaled else None,
        exon=ALL if scaled else None,
        spl_spa=spl_spa,
        spl_fxn=spl_fxn,
        spl_inf=spl_inf,
    )


def _splice_path_of(
    ref: reference.Reference, flow: splice_tree.SpliceFlow, evidence: builders.SpliceEvidence
) -> scoring.PathInput:
    """Build one splice path through the builder that owns `flow`."""
    return _only_path(_FLOW_BUILDER[flow](ref, splice=evidence, gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE))


# --- missense ------------------------------------------------------------------------------------


def test_missense_amino_acid_bakes_mis_caps(ref: reference.Reference) -> None:
    request = builders.build_missense(
        ref,
        amino_acid=builders.AminoAcidEvidence(mis_prd=D('2'), exon=ALL, mis_fxn=D('4'), mis_inf=D('2')),
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    path = _only_path(request)
    assert path.parent_code == 'MIS_'
    assert path.scaling is scoring.Scaling.EXON_ONLY  # predictor captures LoF/GoF; mechanism not applied
    assert [s.cap for s in path.combine_stages] == [_cap(ref.concept_cap('MIS'))]  # MIS_PRD + MIS_FXN combine
    assert path.parent_cap == _cap(ref.category_cap('MIS_PFD'))


def test_missense_adds_splice_path_for_max_path(ref: reference.Reference) -> None:
    request = builders.build_missense(
        ref,
        amino_acid=builders.AminoAcidEvidence(mis_prd=D('0'), exon=ALL),
        splice=builders.SpliceEvidence(colour=splice_tree.SpliceColour.YELLOW, spl_prd=D('3'), mechanism=ME, exon=ALL),
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    assert [p.parent_code for p in request.variant_type_paths] == ['MIS_', 'SPL_']
    assert request.variant_type_paths[1].parent_cap == _cap(ref.category_cap('SPL_PFD'))


def test_missense_inf_above_its_cap_reaches_the_trail_as_the_raw(ref: reference.Reference) -> None:
    """The sum the caller passed is what the trail must show, with the cap as the reduction against it."""
    request = builders.build_missense(
        ref,
        amino_acid=builders.AminoAcidEvidence(mis_prd=D('0'), exon=ALL, mis_inf=D('20')),
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    result = classify.classify(ref, request)
    assert result.selected_path is not None
    line = next(c for c in result.selected_path.contributions if c.label == 'MIS_INF')
    assert line.raw_points == D('20')
    assert line.points == ref.concept_cap('MIS_INF').high


def test_missense_out_of_range_prd_fails_loud(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match='MIS_PRD'):
        builders.build_missense(
            ref,
            amino_acid=builders.AminoAcidEvidence(mis_prd=D('7'), exon=ALL),
            gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        )


def _missense(ref: reference.Reference, amino_acid: builders.AminoAcidEvidence) -> classify.Classification:
    return classify.classify(
        ref, builders.build_missense(ref, amino_acid=amino_acid, gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE)
    )


def _award_line(result: classify.Classification) -> scoring.Contribution:
    assert result.selected_path is not None
    return next(c for c in result.selected_path.contributions if c.label == 'critical residue')


@pytest.mark.parametrize('tier', [t for t in scoring.ExonRelevance if t is not scoring.ExonRelevance.ALL])
def test_the_sm18_waiver_defeats_the_reduction_the_tier_would_have_applied(
    ref: reference.Reference, tier: scoring.ExonRelevance
) -> None:
    """End to end on MIS_PRD: the tier that would have cut the tier does not, and the trail says why.

    Parametrised over the reducing tiers, since a waiver that only matched `All`'s arithmetic would
    be indistinguishable from the tier the workaround it replaces used to assert.
    """
    waiver = scoring.ExonRelevanceWaiver(
        variants=(
            scoring.ExonPathogenicVariant(
                variant='NM_000000.1:c.100G>A',
                classification=scoring.WAIVING_CLASSIFICATION,
                basis=scoring.PathogenicVariantBasis.EXPERT_CLASSIFIED,
                review_stars=scoring.EXPERT_PANEL_REVIEW_STARS,
            ),
        )
    )
    prd = ref.code('MIS_PRD').high
    reduced = _missense(ref, builders.AminoAcidEvidence(mis_prd=prd, exon=tier))
    waived = _missense(ref, builders.AminoAcidEvidence(mis_prd=prd, exon=waiver))

    assert waived.total > reduced.total
    assert waived.total == prd
    assert waived.selected_path is not None
    line = next(c for c in waived.selected_path.contributions if c.label == 'MIS_PRD')
    assert '§17' in line.note


@pytest.mark.parametrize('exon', list(scoring.ExonRelevance))
def test_a_critical_residue_award_is_scaled_by_the_exon_axis(
    ref: reference.Reference, exon: scoring.ExonRelevance
) -> None:
    """The award is predictive evidence, so whatever the matrix does to the tier it does to this.

    Anything else lets a residue in an exon the matrix has zeroed carry the award on its own.
    """
    evidence = builders.AminoAcidEvidence(
        mis_prd=ref.code('MIS_PRD').high, exon=exon, critical_residue=ref.critical_residue_max
    )
    result = _missense(ref, evidence)
    assert result.selected_path is not None
    multiplier = result.selected_path.multiplier
    assert _award_line(result).points == ref.critical_residue_max * multiplier
    assert result.total == (ref.code('MIS_PRD').high + ref.critical_residue_max) * multiplier


def test_a_critical_residue_award_and_its_tier_stay_under_the_predictive_concept_cap(
    ref: reference.Reference,
) -> None:
    """SM7 awards on the predictive code, so the family concept cap is what bounds the pair."""
    evidence = builders.AminoAcidEvidence(
        mis_prd=ref.code('MIS_PRD').high,
        exon=ALL,
        mis_fxn=ref.code('MIS_FXN').high,
        critical_residue=ref.critical_residue_max,
    )
    result = _missense(ref, evidence)
    assert result.selected_path is not None
    assert result.selected_path.total == ref.concept_cap('MIS').high
    assert _award_line(result).points == ref.critical_residue_max  # counted, then bounded with the rest


def test_a_critical_residue_award_above_its_maximum_fails_loud(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match='critical-residue award'):
        _missense(
            ref,
            builders.AminoAcidEvidence(
                mis_prd=ref.code('MIS_PRD').high, exon=ALL, critical_residue=ref.critical_residue_max + 1
            ),
        )


@pytest.mark.parametrize('mis_prd', ['0', '-4'])
def test_a_critical_residue_award_on_a_non_positive_tier_fails_loud(ref: reference.Reference, mis_prd: str) -> None:
    """SM7 awards "on top of the points awarded" by the predictor; at or below zero there are none."""
    with pytest.raises(ValueError, match='positive predictor tier'):
        _missense(
            ref,
            builders.AminoAcidEvidence(mis_prd=D(mis_prd), exon=ALL, critical_residue=ref.critical_residue_max),
        )


def test_a_critical_residue_award_is_withheld_once_prd_and_inf_reach_their_maximum(
    ref: reference.Reference,
) -> None:
    """SM7 conditions the award on the predictive-plus-informative maximum not already being reached.

    The quantity is PRD + INF: a functional assay is not in the condition SM7 states, so it must
    neither trip the refusal nor rescue an award the condition withholds.
    """
    ceiling = ref.category_cap('MIS_PFD').high
    inf = ceiling - ref.code('MIS_PRD').high
    at_the_maximum = builders.AminoAcidEvidence(
        mis_prd=ref.code('MIS_PRD').high, exon=ALL, mis_inf=inf, critical_residue=ref.critical_residue_max
    )
    with pytest.raises(ValueError, match='without it'):
        _missense(ref, at_the_maximum)
    # FXN is outside the condition, so it cannot change either verdict.
    with pytest.raises(ValueError, match='without it'):
        _missense(ref, dataclasses.replace(at_the_maximum, mis_fxn=ref.code('MIS_FXN').low))
    below = dataclasses.replace(at_the_maximum, mis_inf=inf - 1)
    assert _award_line(_missense(ref, below)).points == ref.critical_residue_max
    assert _award_line(_missense(ref, dataclasses.replace(below, mis_fxn=ref.code('MIS_FXN').high))).points


# --- NUL (nonsense / frameshift / exon-deletion) -------------------------------------------------


@pytest.mark.parametrize('build', [builders.build_nonsense, builders.build_frameshift])
def test_null_builders_bake_nul_caps(ref: reference.Reference, build: _Builder) -> None:
    request = build(
        ref,
        null=builders.NullEvidence(nul_prd=D('6'), mechanism=ME, exon=ALL, nul_fxn=D('2')),
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    path = _only_path(request)
    assert path.parent_code == 'NUL_'
    assert path.scaling is scoring.Scaling.MECHANISM_AND_EXON
    assert [s.cap for s in path.combine_stages] == [_cap(ref.concept_cap('NUL'))]
    assert path.parent_cap == _cap(ref.category_cap('NUL_PFD'))


def test_null_subgenic_requires_exon(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match='exon-relevance'):
        builders.build_nonsense(
            ref,
            null=builders.NullEvidence(nul_prd=D('6'), mechanism=ME, exon=None),
            gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        )


def test_exon_deletion_whole_gene_is_mechanism_only(ref: reference.Reference) -> None:
    request = builders.build_exon_deletion(
        ref,
        null=builders.NullEvidence(nul_prd=D('10'), mechanism=ME, exon=None),
        whole_gene=True,
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    path = _only_path(request)
    assert path.scaling is scoring.Scaling.MECHANISM_ONLY
    assert path.exon is None
    assert path.prd_initial == D('10')  # +10 exceeds NUL_PRD code cap [0,6]; admitted by NUL_PFD bound


def test_exon_deletion_whole_gene_rejects_exon(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match='mechanism only'):
        builders.build_exon_deletion(
            ref,
            null=builders.NullEvidence(nul_prd=D('10'), mechanism=ME, exon=ALL),
            whole_gene=True,
            gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        )


# --- SPL (canonical splice / intronic-synonymous) ------------------------------------------------


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_every_splice_cell_builds_and_bakes_its_own_caps(
    ref: reference.Reference, flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    cell = splice_tree.cell_for(flow, colour)
    path = _splice_path_of(ref, flow, _evidence(flow, colour))
    assert path.parent_code == 'SPL_'
    assert path.parent_cap == _cap(ref.category_cap('SPL_PFD'))
    # A splice path always carries both combine layers, in the order the trees state them: the
    # colour's PRD+SPA cap first, then its (PRD+SPA)+FXN cap over the result.
    assert [s.cap for s in path.combine_stages] == [_cap(cell.prd_plus_spa), _cap(cell.plus_fxn)]


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_a_tier_outside_the_cell_is_rejected_in_both_directions(
    ref: reference.Reference, flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # The bound is the cell's, not the reference union's: a tier one step outside either end of the
    # colour's own range is a routing error, whatever any other colour or flow admits.
    cell = splice_tree.cell_for(flow, colour)
    for outside in (cell.prd.low - 1, cell.prd.high + 1):
        with pytest.raises(ValueError, match='SPL_PRD initial'):
            _splice_path_of(ref, flow, _evidence(flow, colour, outside))


def test_the_canonical_nmd_tier_is_refused_on_a_predicted_path(ref: reference.Reference) -> None:
    # The +6.0 NMD tier follows from the wild-type GT/AG position, so only SM11 can award it; SM6
    # and SM12 reach the same colour through an in-silico prediction and start at +3.0. Taking the
    # canonical tier on a predicted path is the VUS-versus-LP error the union range cannot catch.
    nmd = builders.SpliceEvidence(colour=splice_tree.SpliceColour.YELLOW, spl_prd=D('6'), mechanism=ME, exon=ALL)
    assert _splice_path_of(ref, splice_tree.SpliceFlow.CANONICAL, nmd).prd_initial == D('6')
    with pytest.raises(ValueError, match='SPL_PRD initial 6'):
        _splice_path_of(ref, splice_tree.SpliceFlow.PREDICTED, nmd)
    with pytest.raises(ValueError, match='SPL_PRD initial 6'):
        builders.build_missense(
            ref,
            amino_acid=builders.AminoAcidEvidence(mis_prd=D('0'), exon=ALL),
            splice=nmd,
            gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        )


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_the_matrix_axes_must_match_the_cells_scaling(
    ref: reference.Reference, flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # The blue and violet paths state "MM x ER NOT considered", so a mechanism call on one of them
    # is a contradiction to surface, not an input to ignore; the scaled colours need both calls.
    cell = splice_tree.cell_for(flow, colour)
    # Supply an axis where the cell forbids one; withhold both where it requires them. The two
    # branches carry distinct messages, so a cell raising the other one fails here rather than
    # passing on a shared substring.
    fixed = cell.scaling is scoring.Scaling.NONE
    contradiction = builders.SpliceEvidence(
        colour=colour, spl_prd=cell.prd.low, mechanism=ME if fixed else None, exon=None
    )
    expected = 'does not apply the mechanism x exon matrix' if fixed else 'both calls are required'
    with pytest.raises(ValueError, match=expected):
        _splice_path_of(ref, flow, contradiction)


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_a_splice_path_carries_both_assays(
    ref: reference.Reference, flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # An RNA assay plus a protein assay is a consistent evidence set; the two-layer combine scores
    # both rather than refusing the input or dropping one.
    cell = splice_tree.cell_for(flow, colour)
    reading = (
        splice_tree.AssayStrength.SOME_DISRUPTION
        if isinstance(cell.assay, splice_tree.StrengthAssay)
        else splice_tree.Proportion.INCOMPLETE
    )
    path = _splice_path_of(ref, flow, _evidence(flow, colour, spl_spa=reading, spl_fxn=D('-1')))
    assert [i.name for stage in path.combine_stages for i in stage.items] == ['SPL_SPA', 'SPL_FXN']


def test_the_assay_reading_reaches_the_audit_trail(ref: reference.Reference) -> None:
    # The proportion label is the judgement the analyst made; the points alone do not record it.
    flow, colour = splice_tree.SpliceFlow.PREDICTED, splice_tree.SpliceColour.YELLOW
    path = _splice_path_of(ref, flow, _evidence(flow, colour, spl_spa=splice_tree.Proportion.SUBSTANTIAL))
    spa = next(i for stage in path.combine_stages for i in stage.items if i.name == 'SPL_SPA')
    assert spa.note == splice_tree.Proportion.SUBSTANTIAL.value


def test_spa_is_a_proportion_of_the_adjusted_prd_not_the_initial_tier(ref: reference.Reference) -> None:
    # The builder applies the matrix and the caller does not; deriving SPL_SPA from the tier the
    # caller passed would double-count the unreduced points the matrix already discounted.
    request = builders.build_intronic_synonymous(
        ref,
        splice=builders.SpliceEvidence(
            colour=splice_tree.SpliceColour.YELLOW,
            spl_prd=D('3'),
            mechanism=scoring.MechanismLevel.LIKELY,  # x0.5 -> adjusted 1.5
            exon=ALL,
            spl_spa=splice_tree.Proportion.NEAR_TO_COMPLETE,  # 100% of the adjusted value
        ),
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    path = _only_path(request)
    spa = next(i for stage in path.combine_stages for i in stage.items if i.name == 'SPL_SPA')
    assert spa.points == scoring.score_path(ref, path).adjusted_prd
    assert spa.points < path.prd_initial


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_informative_variants_outside_the_cells_inf_range_fail_loud(
    ref: reference.Reference, flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # A splice-unlikely path's informative-variant module scores B/LB only, so a positive SPL_INF
    # there means a P/LP variant was counted where its tree routes one to a re-evaluation. Checked
    # rather than clamped: clamping to 0 scores the path as if the analyst had found nothing.
    cell = splice_tree.cell_for(flow, colour)
    for outside in (cell.inf.low - 1, cell.inf.high + 1):
        with pytest.raises(ValueError, match='SPL_INF value'):
            _splice_path_of(ref, flow, _evidence(flow, colour, spl_inf=outside))


@pytest.mark.parametrize('flow', list(splice_tree.SpliceFlow))
def test_a_splice_unlikely_path_cannot_reach_a_pathogenic_band(
    ref: reference.Reference, flow: splice_tree.SpliceFlow
) -> None:
    # The end-to-end consequence: informative variants were the last input that could carry a path
    # the analyst judged not to splice into a pathogenic band, since they are added after the matrix
    # and escape both combine caps.
    cell = splice_tree.cell_for(flow, splice_tree.SpliceColour.VIOLET)
    request = _FLOW_BUILDER[flow](
        ref,
        splice=_evidence(flow, splice_tree.SpliceColour.VIOLET, spl_inf=cell.inf.high),
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    assert classify.classify(ref, request).total <= 0


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_a_raw_spa_value_is_bounded_by_the_cell_in_both_directions(
    ref: reference.Reference, flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # The escape hatch is for a caller holding a measured number, not a way past the cell: a
    # canonical assay cannot add points, nor a predicted one subtract them, however it is supplied.
    cell = splice_tree.cell_for(flow, colour)
    evidence = _evidence(flow, colour)
    bounds = splice_tree.spa_bounds(cell, scoring.apply_matrix(evidence.spl_prd, D('1')))
    for outside in (bounds.low - 1, bounds.high + 1):
        with pytest.raises(ValueError, match='SPL_SPA'):
            _splice_path_of(ref, flow, dataclasses.replace(evidence, spl_spa_points=outside))


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_a_protein_assay_outside_the_cells_fxn_range_fails_loud(
    ref: reference.Reference, flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # A splice-unlikely path bounds SPL_FXN itself at 0.0 for discordance risk, tighter than the
    # generic module every other colour uses; letting the combine cap absorb a positive assay
    # instead would score the path as if the assay read 0 rather than surfacing the discordance.
    cell = splice_tree.cell_for(flow, colour)
    for outside in (cell.fxn.low - 1, cell.fxn.high + 1):
        with pytest.raises(ValueError, match='SPL_FXN value'):
            _splice_path_of(ref, flow, _evidence(flow, colour, spl_fxn=outside))


def test_the_second_combine_layer_binds(ref: reference.Reference) -> None:
    # The (PRD+SPA)+FXN ceiling is the layer #250 added; with SPA at its own ceiling and a strong
    # protein assay the subtotal exceeds it, so the cap has to clamp rather than ride through.
    flow, colour = splice_tree.SpliceFlow.PREDICTED, splice_tree.SpliceColour.YELLOW
    cell = splice_tree.cell_for(flow, colour)
    path = _splice_path_of(
        ref,
        flow,
        _evidence(flow, colour, spl_spa=splice_tree.Proportion.NEAR_TO_COMPLETE, spl_fxn=cell.fxn.high),
    )
    assert scoring.score_path(ref, path).total == cell.plus_fxn.high


def test_a_raw_spa_taken_off_the_initial_tier_is_refused(ref: reference.Reference) -> None:
    # The misreading the escape hatch must not readmit: the initial tier is passable as SPL_SPA
    # only where the matrix did not discount it, and taking the proportion off the pre-matrix tier
    # is what carries a splice path across the LP boundary.
    evidence = builders.SpliceEvidence(
        colour=splice_tree.SpliceColour.YELLOW,
        spl_prd=D('3'),
        mechanism=scoring.MechanismLevel.LIKELY,  # x0.5 -> adjusted 1.5, so SPL_SPA tops out at 1.5
        exon=ALL,
        spl_spa_points=D('3'),
    )
    with pytest.raises(ValueError, match='SPL_SPA 3 outside'):
        _splice_path_of(ref, splice_tree.SpliceFlow.PREDICTED, evidence)
    accepted = dataclasses.replace(evidence, spl_spa_points=D('1.5'))
    assert _splice_path_of(ref, splice_tree.SpliceFlow.PREDICTED, accepted).combine_stages[0].items[0].points == D(
        '1.5'
    )


def test_the_two_spa_forms_are_mutually_exclusive(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match='not both'):
        builders.build_canonical_splice(
            ref,
            splice=builders.SpliceEvidence(
                colour=splice_tree.SpliceColour.YELLOW,
                spl_prd=D('6'),
                mechanism=ME,
                exon=ALL,
                spl_spa=splice_tree.Proportion.INCOMPLETE,
                spl_spa_points=D('-1'),
            ),
            gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        )


def test_a_contradicted_violet_path_is_not_scored(ref: reference.Reference) -> None:
    with pytest.raises(splice_tree.ReconsiderEvidenceError):
        builders.build_canonical_splice(
            ref,
            splice=builders.SpliceEvidence(
                colour=splice_tree.SpliceColour.VIOLET,
                spl_prd=D('-1'),
                spl_spa=splice_tree.Proportion.NEAR_TO_COMPLETE,
            ),
            gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        )


# --- CDS (in-frame indel / start-lost / stop-lost) -----------------------------------------------


@pytest.mark.parametrize(
    'build',
    [builders.build_inframe_indel, builders.build_start_lost, builders.build_stop_lost],
)
def test_coding_builders_bake_cds_caps(ref: reference.Reference, build: _Builder) -> None:
    request = build(
        ref,
        coding=builders.CodingEvidence(cds_prd=D('6'), mechanism=ME, exon=ALL, cds_fxn=D('2')),
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    path = _only_path(request)
    assert path.parent_code == 'CDS_'
    assert path.scaling is scoring.Scaling.MECHANISM_AND_EXON
    assert [s.cap for s in path.combine_stages] == [_cap(ref.concept_cap('CDS'))]
    assert path.parent_cap == _cap(ref.category_cap('CDS_PFD'))


def test_coding_fxn_out_of_range_fails_loud(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match='CDS_FXN'):
        builders.build_inframe_indel(
            ref,
            coding=builders.CodingEvidence(cds_prd=D('4'), mechanism=ME, exon=ALL, cds_fxn=D('20')),
            gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        )


# --- single-/multi-exon duplication (SM14): the path's own bounds, not the family's ---------------

_DUPLICATION_PATHS = list(duplication_tree.DuplicationPath)


def _duplication_evidence(
    path: duplication_tree.DuplicationPath,
    *,
    prd: decimal.Decimal | None = None,
    fxn: decimal.Decimal | None = None,
    inf: decimal.Decimal = _ZERO,
) -> builders.DuplicationEvidence:
    """Evidence for one SM14 path, carrying the mechanism/exon calls its scaling requires.

    `prd` defaults to the highest tier the cell admits, which every cell has by construction.
    """
    cell = duplication_tree.cell_for(path)
    scaled = cell.scaling is not scoring.Scaling.NONE
    return builders.DuplicationEvidence(
        path=path,
        prd=cell.prd.high if prd is None else prd,
        mechanism=ME if scaled else None,
        exon=ALL if scaled else None,
        fxn=fxn,
        inf=inf,
    )


def _duplication_path_of(ref: reference.Reference, evidence: builders.DuplicationEvidence) -> scoring.PathInput:
    return _only_path(
        builders.build_exon_duplication(ref, duplication=evidence, gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE)
    )


@pytest.mark.parametrize('path', _DUPLICATION_PATHS, ids=lambda p: p.name)
def test_no_duplication_path_scores_outside_its_own_parent_cap(
    ref: reference.Reference, path: duplication_tree.DuplicationPath
) -> None:
    """The strongest evidence a path admits still lands inside the parent cap SM14 gives that path."""
    cell = duplication_tree.cell_for(path)
    evidence = _duplication_evidence(
        path,
        fxn=None if cell.functional is None else cell.functional.fxn.high,
        inf=_ZERO if cell.benign_informative_only else cell.inf.high,
    )
    result = scoring.score_path(ref, _duplication_path_of(ref, evidence))
    assert cell.parent.low <= result.total <= cell.parent.high


@pytest.mark.parametrize('path', _DUPLICATION_PATHS, ids=lambda p: p.name)
def test_a_duplication_paths_cap_binds_exactly_where_its_own_bound_is_reached(
    ref: reference.Reference, path: duplication_tree.DuplicationPath
) -> None:
    """The cap fires on a sum above the path's bound and stays silent on one below it.

    Run against the family's category cap, which is what the builder would otherwise carry: on the
    not-tandem paths the two disagree, and the sum in between is the one a family cap scores wrong.
    """
    cell = duplication_tree.cell_for(path)
    family = ref.category_cap(f'{cell.family}_PFD')
    inf = _ZERO if cell.benign_informative_only else cell.inf.high
    result = scoring.score_path(ref, _duplication_path_of(ref, _duplication_evidence(path, inf=inf)))
    uncapped = scoring.apply_matrix(cell.prd.high, result.multiplier) + inf
    fired = [line for line in result.contributions if line.label == 'parent cap']

    assert bool(fired) == (uncapped > cell.parent.high)
    assert result.total == min(uncapped, cell.parent.high)
    if cell.parent.high < family.high:
        # The point of the cell: this path's ceiling is one the family's cap never reaches down to.
        assert result.total < family.high


@pytest.mark.parametrize(
    'path', [p for p in _DUPLICATION_PATHS if duplication_tree.cell_for(p).functional is None], ids=lambda p: p.name
)
def test_functional_points_on_a_path_that_scores_fxn_na_fail_loud(
    ref: reference.Reference, path: duplication_tree.DuplicationPath
) -> None:
    with pytest.raises(ValueError, match='NA'):
        _duplication_path_of(ref, _duplication_evidence(path, fxn=_ZERO))


@pytest.mark.parametrize(
    'path',
    [p for p in _DUPLICATION_PATHS if duplication_tree.cell_for(p).benign_informative_only],
    ids=lambda p: p.name,
)
def test_a_pathogenic_informative_variant_on_a_benign_only_module_fails_loud(
    ref: reference.Reference, path: duplication_tree.DuplicationPath
) -> None:
    # Clamping it to 0.0 would score the path the tree has just routed away from.
    with pytest.raises(ValueError, match='reconsider'):
        _duplication_path_of(ref, _duplication_evidence(path, inf=D('2')))


@pytest.mark.parametrize('path', _DUPLICATION_PATHS, ids=lambda p: p.name)
def test_a_duplication_tier_the_path_does_not_admit_fails_loud(
    ref: reference.Reference, path: duplication_tree.DuplicationPath
) -> None:
    cell = duplication_tree.cell_for(path)
    with pytest.raises(ValueError, match='_PRD initial'):
        _duplication_path_of(ref, _duplication_evidence(path, prd=cell.prd.high + D('1')))


@pytest.mark.parametrize('path', _DUPLICATION_PATHS, ids=lambda p: p.name)
def test_the_matrix_axes_a_duplication_path_states_are_required_and_no_others(
    ref: reference.Reference, path: duplication_tree.DuplicationPath
) -> None:
    cell = duplication_tree.cell_for(path)
    scaled = cell.scaling is not scoring.Scaling.NONE
    stated = _duplication_evidence(path)
    contradicting = dataclasses.replace(stated, mechanism=None if scaled else ME, exon=None if scaled else ALL)
    with pytest.raises(ValueError, match='matrix'):
        _duplication_path_of(ref, contradicting)
    assert _duplication_path_of(ref, stated).scaling is cell.scaling


# --- the preconditions classify enforces on the built input --------------------------------------


def test_mechanism_precondition_enforced_by_classify(ref: reference.Reference) -> None:
    request = builders.build_nonsense(
        ref,
        null=builders.NullEvidence(nul_prd=D('6'), mechanism=ME, exon=ALL),
        gate_level=gene_disease_pb2.GATE_LEVEL_LIMITED,
    )
    with pytest.raises(ValueError, match='below Moderate'):
        classify.classify(ref, request)


def test_a_path_code_routed_through_independent_codes_is_refused(ref: reference.Reference) -> None:
    """`independent_codes` takes a bare mapping, so this is the way a path's code escapes its caps.

    The same +8 of MIS_INF that the parent cap cuts to 9 on the amino-acid path — LP — would sum to
    12 beside it, which is P.
    """
    on_the_path = classify.classify(
        ref,
        builders.build_missense(
            ref,
            amino_acid=builders.AminoAcidEvidence(mis_prd=D('4'), exon=ALL, mis_inf=D('8')),
            gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        ),
    )
    assert on_the_path.total == ref.category_cap('MIS_PFD').high
    assert on_the_path.final_class == 'LP'
    with pytest.raises(ValueError, match='variant-type path'):
        classify.classify(
            ref,
            builders.build_missense(
                ref,
                amino_acid=builders.AminoAcidEvidence(mis_prd=D('4'), exon=ALL),
                independent_codes={'MIS_INF': D('8')},
                gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
            ),
        )


def test_the_mapping_form_cannot_file_a_code_the_framework_did_not_determine(ref: reference.Reference) -> None:
    """The mapping carries points, and a not-determined finding has none to hand it.

    The route matters because it is the one that loses the finding's support on the way in: a caller
    reaching for `.points` on an unscoreable POP_FRQ has a None, and filing it as the 0.0 a rare
    variant scores is exactly what the mapping must not do.
    """
    faf = frequency.joint_faf(
        D('0.00131'), exome=frequency.Callset(allele_count=31, filters=('AS_VQSR',), flags=()), genome=None
    )
    undetermined = frequency.pop_frq(ref, faf, frequency.curated_daft(D('0.0001'), source='a VCEP'))
    with pytest.raises(ValueError, match='POP_FRQ was not determined'):
        builders.build_nonsense(
            ref,
            null=builders.NullEvidence(nul_prd=D('6'), mechanism=ME, exon=ALL),
            independent_codes={'POP_FRQ': undetermined.points},  # type: ignore[dict-item] — the code-mode case
            gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        )


def test_the_builders_place_a_path_on_every_family_the_reference_does_not_call_independent(
    ref: reference.Reference,
) -> None:
    """The two sides of the split, checked against each other.

    `classify` reads the admissible independent families off the reference; what makes that the
    right split is that the builders score every other family on a path. A family reaching the
    reference with neither a path here nor a place in the tally would sit outside both.
    """
    requests = [
        builders.build_missense(
            ref,
            amino_acid=builders.AminoAcidEvidence(mis_prd=D('0'), exon=ALL),
            splice=_evidence(splice_tree.SpliceFlow.PREDICTED, splice_tree.SpliceColour.YELLOW),
            gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        ),
        builders.build_nonsense(
            ref,
            null=builders.NullEvidence(nul_prd=D('6'), mechanism=ME, exon=ALL),
            gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        ),
        builders.build_inframe_indel(
            ref,
            coding=builders.CodingEvidence(cds_prd=D('2'), mechanism=ME, exon=ALL),
            gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        ),
    ]
    placed = {path.parent_code.removesuffix('_') for request in requests for path in request.variant_type_paths}
    assert placed == {spec.family for spec in ref.codes.values()} - ref.independent_families


_ROUTED = evidence_pb2.CONSEQUENCE_NONSENSE
_NULL = builders.NullEvidence(nul_prd=D('6'), mechanism=ME, exon=ALL)
_CODING = builders.CodingEvidence(cds_prd=D('2'), mechanism=ME, exon=ALL)
_AMINO_ACID = builders.AminoAcidEvidence(mis_prd=D('3'), exon=ALL)
_DUPLICATION = _duplication_evidence(duplication_tree.DuplicationPath.YELLOW)


def _routed(
    ref: reference.Reference, consequence: evidence_pb2.Consequence, evidence: builders.VariantEvidence
) -> classify.ClassificationInput:
    return builders.for_consequence(ref, consequence, evidence, gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE)


def test_the_consequence_selects_the_workflow_its_supplement_states(ref: reference.Reference) -> None:
    routed = _routed(ref, evidence_pb2.CONSEQUENCE_MISSENSE, builders.MissensePaths(amino_acid=_AMINO_ACID))
    built = builders.build_missense(ref, amino_acid=_AMINO_ACID, gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE)
    assert routed == built


@pytest.mark.parametrize(
    'consequence',
    [
        evidence_pb2.CONSEQUENCE_NONSENSE,
        evidence_pb2.CONSEQUENCE_FRAMESHIFT,
        evidence_pb2.CONSEQUENCE_EXON_DELETION,
        evidence_pb2.CONSEQUENCE_START_LOST,
        evidence_pb2.CONSEQUENCE_STOP_LOST,
    ],
)
def test_the_evidence_family_selects_the_arm_of_a_lof_tree(
    ref: reference.Reference, consequence: evidence_pb2.Consequence
) -> None:
    # Each of these trees forks on a judgement the consequence does not carry — NMD, or a rescuing
    # start or stop — and the family the caller supplies is how that call arrives.
    assert _only_path(_routed(ref, consequence, _NULL)).parent_code == 'NUL_'
    assert _only_path(_routed(ref, consequence, _CODING)).parent_code == 'CDS_'


def test_a_canonical_splice_variant_takes_the_flow_whose_tier_starts_at_six(ref: reference.Reference) -> None:
    # The canonical flow's NMD tier is +6.0 and the predicted flow's is +3.0, so a +6.0 tier routed
    # onto the wrong flow is refused rather than scored two bands high.
    yellow = builders.SpliceEvidence(colour=splice_tree.SpliceColour.YELLOW, spl_prd=D('6'), mechanism=ME, exon=ALL)
    assert _only_path(_routed(ref, evidence_pb2.CONSEQUENCE_CANONICAL_SPLICE, yellow)).prd_initial == D('6')
    with pytest.raises(ValueError, match='predicted yellow path'):
        _routed(ref, evidence_pb2.CONSEQUENCE_INTRONIC, yellow)


@pytest.mark.parametrize(
    ('consequence', 'evidence'),
    [
        (evidence_pb2.CONSEQUENCE_MISSENSE, _NULL),
        (evidence_pb2.CONSEQUENCE_INFRAME_INDEL, _NULL),
        (evidence_pb2.CONSEQUENCE_EXON_DUPLICATION, _CODING),
        (evidence_pb2.CONSEQUENCE_CANONICAL_SPLICE, _CODING),
        (evidence_pb2.CONSEQUENCE_NONSENSE, _DUPLICATION),
    ],
)
def test_an_inadmissible_pair_names_both_sides(
    ref: reference.Reference, consequence: evidence_pb2.Consequence, evidence: builders.VariantEvidence
) -> None:
    with pytest.raises(ValueError, match=evidence_pb2.Consequence.Name(consequence)) as refused:
        _routed(ref, consequence, evidence)
    assert type(evidence).__name__ in str(refused.value)


def test_a_non_coding_variant_reaches_no_released_workflow(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match='SM17'):
        _routed(ref, evidence_pb2.CONSEQUENCE_NON_CODING, _NULL)


def test_an_unresolved_consequence_is_refused(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match=r'Variant\.Normalize'):
        _routed(ref, evidence_pb2.CONSEQUENCE_UNSPECIFIED, _NULL)


def test_a_consequence_composed_as_something_other_than_the_enum_is_refused(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match=r'must be an evidence_pb2\.Consequence'):
        _routed(ref, True, _NULL)  # type: ignore[arg-type] — the code-mode case


def test_every_consequence_the_contract_carries_routes_or_names_why_not(ref: reference.Reference) -> None:
    """No consequence falls through: a member added to the contract reaches a tree or a refusal."""
    families = (
        builders.MissensePaths(amino_acid=_AMINO_ACID),
        _NULL,
        _CODING,
        builders.SpliceEvidence(colour=splice_tree.SpliceColour.BLUE, spl_prd=_ZERO),
        _DUPLICATION,
    )
    for value in evidence_pb2.Consequence.values():
        consequence = typing.cast('evidence_pb2.Consequence', value)
        routed = [family for family in families if _routes(ref, consequence, family)]
        if consequence in builders._ADMITTED:
            assert routed, evidence_pb2.Consequence.Name(value)
        else:
            assert not routed, evidence_pb2.Consequence.Name(value)


def _routes(
    ref: reference.Reference, consequence: evidence_pb2.Consequence, evidence: builders.VariantEvidence
) -> bool:
    try:
        _routed(ref, consequence, evidence)
    except ValueError:
        return False
    return True


def test_the_routed_call_scores_the_variant_and_carries_its_releases(ref: reference.Reference) -> None:
    release = provenance.Release('Ensembl VEP REST', 'Ensembl 113')
    result = builders.classify_variant(
        ref,
        consequence=evidence_pb2.CONSEQUENCE_MISSENSE,
        evidence=builders.MissensePaths(amino_acid=dataclasses.replace(_AMINO_ACID, releases=(release,))),
        independent_codes={'POP_FRQ': _ZERO},
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    assert result.total == D('3')
    assert result.releases == (release,)


@pytest.mark.parametrize(
    ('consequence', 'evidence', 'build'),
    [
        (evidence_pb2.CONSEQUENCE_NONSENSE, _NULL, builders.build_nonsense),
        (evidence_pb2.CONSEQUENCE_FRAMESHIFT, _NULL, builders.build_frameshift),
        (evidence_pb2.CONSEQUENCE_EXON_DELETION, _NULL, builders.build_exon_deletion),
        (evidence_pb2.CONSEQUENCE_INFRAME_INDEL, _CODING, builders.build_inframe_indel),
        (evidence_pb2.CONSEQUENCE_START_LOST, _CODING, builders.build_start_lost),
        (evidence_pb2.CONSEQUENCE_STOP_LOST, _CODING, builders.build_stop_lost),
    ],
)
def test_a_routed_path_is_the_one_its_named_builder_assembles(
    ref: reference.Reference,
    consequence: evidence_pb2.Consequence,
    evidence: builders.VariantEvidence,
    build: _Builder,
) -> None:
    """The routing and the named builders are one assembly, so neither can drift from the other."""
    keyword = 'null' if isinstance(evidence, builders.NullEvidence) else 'coding'
    named = build(ref, gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE, **{keyword: evidence})
    assert _routed(ref, consequence, evidence) == named
