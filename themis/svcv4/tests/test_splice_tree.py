"""Tests for the splice decision-tree cells: the bounds the reference states only as a union.

Most assertions are invariants over the whole table: that the assay vocabulary a cell offers cannot
produce a value the same cell forbids, that the trees' caps are belt-and-braces over that domain
rather than silently reshaping a derived value, and that the fixed colours do not vary by flow.

Two groups pin transcribed numbers instead, against the usual rule. The assay awards and the three
resolved conflicts are hand-transcribed from the decision trees with no upstream check, so an
editing slip in them *is* the error mode and no invariant reaches it — a mistyped 0.5 sits happily
inside every bound the rest of the file asserts. They are not the change-detector the policy warns
about either: the tables change only on a corpus revision, and a test that fails then is a test
doing its job, since the new numbers have to be re-read off the tree anyway.
"""

from __future__ import annotations

import dataclasses
import decimal
import itertools

import pytest

from themis.svcv4 import provenance, reference, scoring, splice_tree
from themis.svcv4.tests import responses

D = decimal.Decimal

# Every matrix multiplier the mechanism x exon grid can produce (SM18).
_MULTIPLIERS = (D('0'), D('0.25'), D('0.5'), D('1'))

_CELLS = tuple(itertools.product(splice_tree.SpliceFlow, splice_tree.SpliceColour))


def _vocabulary(cell: splice_tree.SpliceCell) -> tuple[splice_tree.Proportion | splice_tree.AssayStrength, ...]:
    """The readings the cell's own assay rule accepts."""
    if isinstance(cell.assay, splice_tree.StrengthAssay):
        return tuple(splice_tree.AssayStrength)
    return tuple(splice_tree.Proportion)


def _reachable_prd(cell: splice_tree.SpliceCell) -> tuple[decimal.Decimal, ...]:
    """Both bounds of the cell's PRD range, plus every tier the trees award in between.

    The bounds are seeded rather than filtered in, so a cell whose range falls between the candidate
    tiers still probes its extremes instead of yielding nothing and passing every loop vacuously.
    """
    candidates = (D('-1'), D('-0.5'), D('0'), D('0.5'), D('1'), D('2'), D('3'), D('4'), D('6'))
    inside = {t for t in candidates if cell.prd.low <= t <= cell.prd.high}
    return tuple(sorted(inside | {cell.prd.low, cell.prd.high}))


def _scored(cell: splice_tree.SpliceCell) -> list[tuple[decimal.Decimal, decimal.Decimal]]:
    """Every (adjusted PRD, derived SPA) pair the cell admits; RECONSIDER readings are not scores."""
    multipliers = _MULTIPLIERS if cell.scaling is not scoring.Scaling.NONE else (D('1'),)
    pairs = []
    for tier, multiplier, judgement in itertools.product(_reachable_prd(cell), multipliers, _vocabulary(cell)):
        adjusted = scoring.apply_matrix(tier, multiplier)
        try:
            pairs.append((adjusted, splice_tree.spa_points(cell, judgement, adjusted)))
        except splice_tree.ReconsiderEvidenceError:
            continue
    return pairs


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_every_flow_colour_has_a_cell(flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour) -> None:
    # The table must be total: a flow reaching a colour with no cell would have no bounds at all.
    cell = splice_tree.cell_for(flow, colour)
    assert cell.prd.low <= cell.prd.high
    assert _vocabulary(cell)


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_derived_spa_stays_inside_the_cells_own_range(
    flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # A cell cannot offer a reading that produces a value the same cell forbids. This is what fixes
    # the proportional base to the *positive* part of the adjusted PRD: scaling the negative
    # alternate-in-frame-start tier puts SPL_SPA outside its declared range on both flows.
    cell = splice_tree.cell_for(flow, colour)
    for adjusted, spa in _scored(cell):
        assert cell.spa.low <= spa <= cell.spa.high, (adjusted, spa)


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_every_reading_lands_inside_the_bounds_a_raw_value_is_held_to(
    flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # The hatch spans the readings rather than enumerating them, so a value between two labels
    # passes; what must never happen is the hatch rejecting a value one of the labels awards.
    cell = splice_tree.cell_for(flow, colour)
    multipliers = _MULTIPLIERS if cell.scaling is not scoring.Scaling.NONE else (D('1'),)
    for tier, multiplier, judgement in itertools.product(_reachable_prd(cell), multipliers, _vocabulary(cell)):
        adjusted = scoring.apply_matrix(tier, multiplier)
        bounds = splice_tree.spa_bounds(cell, adjusted)
        assert cell.spa.low <= bounds.low <= bounds.high <= cell.spa.high, (tier, multiplier)
        try:
            spa = splice_tree.spa_points(cell, judgement, adjusted)
        except splice_tree.ReconsiderEvidenceError:
            continue
        assert bounds.low <= spa <= bounds.high, (tier, multiplier, judgement)


@pytest.mark.parametrize('flow', list(splice_tree.SpliceFlow))
def test_the_matrix_tightens_what_a_scaled_assay_can_award(flow: splice_tree.SpliceFlow) -> None:
    # SPL_SPA is a proportion of the *adjusted* tier, so a discounting matrix must shrink the
    # interval a raw value is held to; otherwise the pre-matrix tier stays passable as an SPA value.
    cell = splice_tree.cell_for(flow, splice_tree.SpliceColour.YELLOW)
    full = splice_tree.spa_bounds(cell, cell.prd.high)
    halved = splice_tree.spa_bounds(cell, cell.prd.high / 2)
    assert full.low <= halved.low <= halved.high <= full.high
    assert (full.low, full.high) != (halved.low, halved.high)


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_prd_plus_spa_cap_never_binds_on_a_derived_assay(
    flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # The trees' combine cap is belt-and-braces over the cell's own input domain. A cell where it
    # bound would mean the bounds and the cap disagree — a transcription error, not a clamp to use.
    cell = splice_tree.cell_for(flow, colour)
    for adjusted, spa in _scored(cell):
        assert cell.prd_plus_spa.low <= adjusted + spa <= cell.prd_plus_spa.high, (adjusted, spa)


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_prd_plus_spa_cap_contains_the_bare_prd(flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour) -> None:
    # The first combine layer applies whether or not an assay exists, so an absent SPL_SPA must not
    # let the cap reshape the tier the path just awarded.
    cell = splice_tree.cell_for(flow, colour)
    multipliers = _MULTIPLIERS if cell.scaling is not scoring.Scaling.NONE else (D('1'),)
    for tier, multiplier in itertools.product(_reachable_prd(cell), multipliers):
        adjusted = scoring.apply_matrix(tier, multiplier)
        assert cell.prd_plus_spa.low <= adjusted <= cell.prd_plus_spa.high, (tier, multiplier)


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_second_combine_layer_contains_the_first(
    flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # Same argument one layer up: with no SPL_FXN the (PRD+SPA)+FXN cap must be a no-op.
    cell = splice_tree.cell_for(flow, colour)
    assert cell.plus_fxn.low <= cell.prd_plus_spa.low
    assert cell.prd_plus_spa.high <= cell.plus_fxn.high


@pytest.mark.parametrize('flow', list(splice_tree.SpliceFlow))
def test_the_splice_unlikely_cell_awards_nothing_positive(flow: splice_tree.SpliceFlow) -> None:
    # Violet is a benign-only path: its tier is fixed negative, its assay only subtracts, and its
    # protein assay is bounded at 0 for discordance risk. (The cell is; the assembled path is not
    # yet — SPL_INF is still on the reference union, which no bound here covers.)
    cell = splice_tree.cell_for(flow, splice_tree.SpliceColour.VIOLET)
    assert cell.prd.high < 0
    assert cell.spa.high <= 0
    assert cell.prd_plus_spa.high <= 0
    assert cell.fxn.high <= 0
    assert cell.plus_fxn.high <= 0


@pytest.mark.parametrize('colour', [splice_tree.SpliceColour.BLUE, splice_tree.SpliceColour.VIOLET])
def test_the_fixed_colours_do_not_vary_by_flow(colour: splice_tree.SpliceColour) -> None:
    # Only the scaled colours carry the flow's asymmetry: the fixed ones score the assay itself
    # rather than a proportion of a flow-specific tier, and all three trees state them alike. A
    # bound that differs by flow here is a transcription slip, not a framework distinction.
    canonical = splice_tree.cell_for(splice_tree.SpliceFlow.CANONICAL, colour)
    predicted = splice_tree.cell_for(splice_tree.SpliceFlow.PREDICTED, colour)
    assert canonical == predicted


@pytest.mark.parametrize('colour', [splice_tree.SpliceColour.YELLOW, splice_tree.SpliceColour.ORANGE])
def test_the_assay_runs_opposite_ways_on_the_two_flows(colour: splice_tree.SpliceColour) -> None:
    # SM11's assay can only walk a positional tier back; SM6/SM12's is the predicted flow's only
    # route to the +6 ceiling. Both directions asserted: neither flow may award the other's sign.
    canonical = splice_tree.cell_for(splice_tree.SpliceFlow.CANONICAL, colour)
    predicted = splice_tree.cell_for(splice_tree.SpliceFlow.PREDICTED, colour)
    assert [spa for _, spa in _scored(canonical) if spa > 0] == []
    assert [spa for _, spa in _scored(predicted) if spa < 0] == []
    assert any(spa < 0 for _, spa in _scored(canonical))  # not vacuously non-positive
    assert any(spa > 0 for _, spa in _scored(predicted))


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_a_negative_tier_is_never_scaled_by_the_assay(
    flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # SPL_SPA is a proportion of the *positive* adjusted PRD; below zero there is nothing to take a
    # proportion of, so a scaled cell must award nothing rather than amplify a benign tier.
    cell = splice_tree.cell_for(flow, colour)
    if not isinstance(cell.assay, splice_tree.ScaledAssay) or cell.prd.low >= 0:
        pytest.skip('cell has no negative tier on a scaled assay')
    for judgement in splice_tree.Proportion:
        assert splice_tree.spa_points(cell, judgement, cell.prd.low) == 0


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_a_reading_outside_the_cells_vocabulary_fails_loud(
    flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # Blue scores the assay's own strength, every other colour a proportion of transcripts; passing
    # one where the other belongs is a category error, not a value to coerce.
    cell = splice_tree.cell_for(flow, colour)
    foreign = (
        splice_tree.Proportion.SUBSTANTIAL
        if isinstance(cell.assay, splice_tree.StrengthAssay)
        else splice_tree.AssayStrength.CLEAR_DISRUPTION
    )
    with pytest.raises(ValueError, match='this path scores'):
        splice_tree.spa_points(cell, foreign, D('0'))


def test_an_assay_rule_that_leaves_a_reading_unscored_fails_loud() -> None:
    # Every rule in the table is hand-written, so a reading dropped while editing one would
    # otherwise surface as a KeyError at score time on whichever variant happens to hit it.
    with pytest.raises(ValueError, match='INCOMPLETE'):
        splice_tree.ScaledAssay(factors={splice_tree.Proportion.NEAR_TO_COMPLETE: D('1')})
    with pytest.raises(ValueError, match='SUBSTANTIAL'):
        splice_tree.FixedAssay(points={}, reconsider=frozenset({splice_tree.Proportion.INCOMPLETE}))
    with pytest.raises(ValueError, match='UNCONVINCING'):
        splice_tree.StrengthAssay(points={splice_tree.AssayStrength.CLEAR_DISRUPTION: D('2')})


@pytest.mark.parametrize('flow', list(splice_tree.SpliceFlow))
def test_a_near_complete_aberrant_product_reroutes_a_splice_unlikely_path(flow: splice_tree.SpliceFlow) -> None:
    # On violet the assay contradicting the prediction is not a score at either end of the range;
    # the routing was wrong and the analyst has to re-choose the path.
    cell = splice_tree.cell_for(flow, splice_tree.SpliceColour.VIOLET)
    with pytest.raises(splice_tree.ReconsiderEvidenceError):
        splice_tree.spa_points(cell, splice_tree.Proportion.NEAR_TO_COMPLETE, D('-1'))
    assert splice_tree.spa_points(cell, splice_tree.Proportion.INCOMPLETE, D('-1')) < 0


# --- transcription checks: the numbers no invariant above can reach ------------------------------


def _award(flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour, tier: str) -> dict[str, str]:
    """Each reading's award at `tier`, keyed by reading name; a re-routing reading maps to itself."""
    cell = splice_tree.cell_for(flow, colour)
    awards = {}
    for judgement in _vocabulary(cell):
        try:
            awards[judgement.name] = str(splice_tree.spa_points(cell, judgement, D(tier)))
        except splice_tree.ReconsiderEvidenceError:
            awards[judgement.name] = 'RECONSIDER'
    return awards


def test_the_scaled_assay_awards_match_the_trees() -> None:
    # SM11 §1c: near-to-complete 0%, substantial -25% of the positive PRD, incomplete -100%.
    # SM6 yellow step 2 / SM12 §1c: near-to-complete x1.0, substantial x0.5, incomplete +0.
    canonical = _award(splice_tree.SpliceFlow.CANONICAL, splice_tree.SpliceColour.YELLOW, '4')
    predicted = _award(splice_tree.SpliceFlow.PREDICTED, splice_tree.SpliceColour.YELLOW, '4')
    assert canonical == {'NEAR_TO_COMPLETE': '0', 'SUBSTANTIAL': '-1.00', 'INCOMPLETE': '-4'}
    assert predicted == {'NEAR_TO_COMPLETE': '4', 'SUBSTANTIAL': '2.0', 'INCOMPLETE': '0'}


def test_the_fixed_assay_awards_match_the_trees() -> None:
    # SM11 §4b / §5b, matched by SM12 §4b / §5b and SM6 blue and violet step 2.
    assert _award(splice_tree.SpliceFlow.CANONICAL, splice_tree.SpliceColour.BLUE, '0') == {
        'CLEAR_DISRUPTION': '2',
        'SOME_DISRUPTION': '1',
        'UNCONVINCING': '0',
        'SOME_NO_EFFECT': '-1',
        'CONVINCING_NO_EFFECT': '-2',
    }
    assert _award(splice_tree.SpliceFlow.CANONICAL, splice_tree.SpliceColour.VIOLET, '-1') == {
        'NEAR_TO_COMPLETE': 'RECONSIDER',
        'SUBSTANTIAL': '0',
        'INCOMPLETE': '-2',
    }


def test_the_resolved_conflicts_hold_their_resolution() -> None:
    # The three deviations the module docstring argues for, each against at least one tree, so each
    # is the bound a future reader is likeliest to "correct" back. Orange floor: -1.0, not SM11
    # §2c / SM6's 0.0. Violet ceiling: 0.0, not SM6's generic +9.0. Blue ceiling: +8.0 on both
    # flows, not SM12 §4c / SM6's +9.0.
    for flow in splice_tree.SpliceFlow:
        assert splice_tree.cell_for(flow, splice_tree.SpliceColour.ORANGE).prd_plus_spa.low == D('-1')
        assert splice_tree.cell_for(flow, splice_tree.SpliceColour.VIOLET).plus_fxn.high == D('0')
        assert splice_tree.cell_for(flow, splice_tree.SpliceColour.BLUE).plus_fxn.high == D('8')


def _union(ref: reference.Reference, field: str) -> reference.CapRange:
    """The reference range a `SpliceCell` bound of that name must sit inside.

    Keyed by field name rather than spelled out per call site, so a range added to `SpliceCell`
    without a reference counterpart fails the totality check below instead of going unenforced.
    """
    code = {'prd': 'SPL_PRD', 'spa': 'SPL_SPA', 'fxn': 'SPL_FXN'}.get(field)
    if code is not None:
        spec = ref.code(code)
        return reference.CapRange(low=spec.low, high=spec.high)
    return ref.concept_cap({'prd_plus_spa': 'SPL_PRD_SPA', 'plus_fxn': 'SPL_FXN', 'inf': 'SPL_INF'}[field])


def _range_fields() -> tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(splice_tree.SpliceCell) if f.type == 'reference.CapRange')


def test_every_range_on_the_cell_has_a_reference_counterpart(ref: reference.Reference) -> None:
    # A range the cell carries but nothing above maps is one the trees bound and the library leaves
    # unchecked — the class of hole this module exists to close. Adding a field has to fail here
    # rather than ship unenforced, so the lookup is over the dataclass, not a hand-kept list.
    assert _range_fields()  # non-vacuous: the cell does carry ranges
    for field in _range_fields():
        assert _union(ref, field).low <= _union(ref, field).high, field


@pytest.mark.parametrize(('flow', 'colour'), _CELLS)
def test_the_cells_are_at_least_as_tight_as_the_reference_union(
    ref: reference.Reference, flow: splice_tree.SpliceFlow, colour: splice_tree.SpliceColour
) -> None:
    # The whole point of the module: every cell bound sits inside the union the reference declares,
    # so the cells only ever reject values the reference already admits.
    cell = splice_tree.cell_for(flow, colour)
    for field in _range_fields():
        bound, union = getattr(cell, field), _union(ref, field)
        assert union.low <= bound.low <= bound.high <= union.high, (field, bound, union)


def test_some_cell_is_strictly_tighter_than_the_union(ref: reference.Reference) -> None:
    # The converse of the containment above: were every cell equal to the union, the module would be
    # dead weight and the +6-on-a-predicted-path hole would still be open.
    prd = ref.code('SPL_PRD')
    assert any(splice_tree.cell_for(f, c).prd.high < prd.high for f, c in _CELLS)
    assert any(splice_tree.cell_for(f, c).prd.low > prd.low for f, c in _CELLS)


def test_the_prd_plus_spa_cap_is_the_union_over_the_colours(ref: reference.Reference) -> None:
    """No supplement states this combine's cap once: each colour's is stated where its path is.

    SM6 §48 and §69 cap it at 0.0 to +6.0 on the yellow paths, SM12 §42 at -1.0 to +6.0 on orange,
    SM6 §98 at -2.0 to +2.0 on blue, and SM6 §119 / SM11 §94 / SM12 §109 at -3.0 to 0 on violet. The
    reference carries their union, so a cell edited past what any of them states fails here rather
    than passing a containment check against a bound wide enough to admit it.
    """
    prd_plus_spa = [splice_tree.cell_for(flow, colour).prd_plus_spa for flow, colour in _CELLS]
    assert ref.concept_cap('SPL_PRD_SPA') == reference.CapRange(
        low=min(cap.low for cap in prd_plus_spa), high=max(cap.high for cap in prd_plus_spa)
    )


def test_both_predictors_pairs_are_read_off_the_response() -> None:
    deltas = splice_tree.deltas_from_prediction(responses.splice_deltas_response())
    assert deltas.spliceai_loss == D('0.87')
    assert deltas.pangolin_loss == D('0.79')
    assert deltas.spliceai == D('0.87')  # the stronger effect of either kind
    assert provenance.Release('Broad SpliceAI', 'SpliceAI 1.3.1') in deltas.releases


@pytest.mark.parametrize(
    ('score', 'expected'),
    [
        ('0.87', splice_tree.EntryTier.LIKELY),
        ('0.21', splice_tree.EntryTier.LIKELY),
        ('0.2', splice_tree.EntryTier.UNCERTAIN),  # both bounds of the indeterminate band are in it
        ('0.15', splice_tree.EntryTier.UNCERTAIN),
        ('0.1', splice_tree.EntryTier.UNCERTAIN),
        ('0.09', splice_tree.EntryTier.UNLIKELY),
        ('0.0', splice_tree.EntryTier.UNLIKELY),
    ],
)
def test_the_score_bins_onto_the_trichotomy_the_flows_enter_on(score: str, expected: splice_tree.EntryTier) -> None:
    deltas = splice_tree.deltas_from_prediction(responses.splice_deltas_response(spliceai=(0.0, float(score))))
    assert splice_tree.entry_tier(deltas) is expected


def test_the_tiers_are_monotone_over_the_score_line() -> None:
    """A stronger prediction never bins weaker, and every tier is reachable: the bins partition it."""
    order = [splice_tree.EntryTier.UNLIKELY, splice_tree.EntryTier.UNCERTAIN, splice_tree.EntryTier.LIKELY]
    tiers = [
        splice_tree.entry_tier(
            splice_tree.deltas_from_prediction(responses.splice_deltas_response(spliceai=(0.0, hundredth / 100)))
        )
        for hundredth in range(101)
    ]
    assert [order.index(tier) for tier in tiers] == sorted(order.index(tier) for tier in tiers)
    assert set(tiers) == set(splice_tree.EntryTier)


def test_a_position_only_pangolin_scored_has_no_calibrated_tier() -> None:
    deltas = splice_tree.deltas_from_prediction(responses.splice_deltas_response(spliceai=None))
    assert deltas.spliceai is None
    with pytest.raises(ValueError, match='stated for SpliceAI'):
        splice_tree.entry_tier(deltas)


def test_the_predictors_are_compared_gain_against_gain() -> None:
    # A loss one predictor calls likely and the other unlikely is a discordance a single maximum
    # over each predictor's own pair would hide.
    concordant = splice_tree.deltas_from_prediction(responses.splice_deltas_response())
    discordant = splice_tree.deltas_from_prediction(
        responses.splice_deltas_response(spliceai=(0.02, 0.87), pangolin=(0.02, 0.03))
    )
    assert concordant.concordant is True
    assert discordant.concordant is False
    assert 'discordant' in discordant.derivation


def test_one_predictor_scoring_nothing_is_no_second_opinion_rather_than_a_disagreement() -> None:
    deltas = splice_tree.deltas_from_prediction(responses.splice_deltas_response(pangolin=None))
    assert deltas.concordant is None


def test_a_predictor_stating_one_delta_of_its_pair_is_refused() -> None:
    response = responses.splice_deltas_response()
    response.ClearField('spliceai_loss')
    with pytest.raises(ValueError, match='one delta of its pair'):
        splice_tree.deltas_from_prediction(response)


def test_a_position_neither_predictor_scored_is_refused() -> None:
    # That answer is the rpc's NOT_FOUND — the position is unscorable — not an empty response.
    with pytest.raises(ValueError, match='neither predictor scored'):
        splice_tree.deltas_from_prediction(responses.splice_deltas_response(spliceai=None, pangolin=None))
