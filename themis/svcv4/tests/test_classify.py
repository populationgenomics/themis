"""Tests for the top-level classify contract, on hand-constructed combining inputs."""

from __future__ import annotations

import decimal
from collections.abc import Sequence

import pytest

from themis.rpc import gene_disease_pb2
from themis.svcv4 import classify, frequency, provenance, reference, scoring

D = decimal.Decimal


@pytest.mark.parametrize(
    ('code', 'points', 'final_class'),
    [
        ('CLN_CCS', '8', 'LP'),  # PS4 pathogenic direction; the old [0,4] cap clamped to +4 (VUS-high)
        ('CLN_CCS', '-8', 'B'),  # PS4 benign direction; the old [0,4] cap clamped to 0 (not benign)
        ('CLN_DNV', '12', 'P'),  # summed de-novo; the old [0,7] cap clamped to +7 (LP)
    ],
)
def test_corrected_caps_change_class(ref: reference.Reference, code: str, points: str, final_class: str) -> None:
    # An independent code carried through classify with a neutral variant path; the fixed per-code
    # cap now admits the full point value, flipping the class the old (wrong) cap suppressed.
    path = scoring.PathInput(label='blue', parent_code='SPL_', prd_initial=D('0'), scaling=scoring.Scaling.NONE)
    request = classify.ClassificationInput(
        variant_type_paths=[path],
        independent_codes=[classify.IndependentCode(code, D(points))],
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    assert classify.classify(ref, request).final_class == final_class


def test_lof_frameshift_nmd_path(ref: reference.Reference) -> None:
    # HNF4A-style: NMD frameshift, LoF Established, exon in all relevant transcripts, three distinct
    # P LoF variants in the same exon (NUL_INF), absent from gnomAD (POP_FRQ 0).
    #   NUL_PRD +6 x (Established x All = 1.0) = 6.0
    #   NUL_INF three P: +2 + 1 + 1 = +4, added after the matrix
    #   NUL_ = 6 + 4 = 10 (parent cap -8..+10) -> Pathogenic
    yellow = scoring.PathInput(
        label='yellow NMD',
        parent_code='NUL_',
        prd_initial=D('6'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
        mechanism=scoring.MechanismLevel.ESTABLISHED,
        exon=scoring.ExonRelevance.ALL,
        inf=scoring.informative_points(('P', 'P', 'P')),
        parent_cap=(ref.category_cap('NUL_PFD').low, ref.category_cap('NUL_PFD').high),
    )
    request = classify.ClassificationInput(
        variant_type_paths=[yellow],
        independent_codes=[classify.IndependentCode('POP_FRQ', D('0'))],
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    result = classify.classify(ref, request)
    assert result.total == D('10')
    assert result.band == 'P'
    assert result.final_class == 'P'
    assert result.gate_capped is False


def test_missense_max_path_selects_amino_acid(ref: reference.Reference) -> None:
    # Amino-acid MIS_PRD +3 (exon All), splice path negative -> amino-acid path wins; POP_FRQ -1.
    amino_acid = scoring.PathInput(
        label='amino-acid',
        parent_code='MIS_',
        prd_initial=D('3'),
        scaling=scoring.Scaling.EXON_ONLY,
        exon=scoring.ExonRelevance.ALL,
        parent_cap=(D('-8'), D('9')),
    )
    splice = scoring.PathInput(
        label='violet unlikely',
        parent_code='SPL_',
        prd_initial=D('-1'),
        scaling=scoring.Scaling.NONE,
        parent_cap=(D('-8'), D('8')),
    )
    request = classify.ClassificationInput(
        variant_type_paths=[amino_acid, splice],
        independent_codes=[classify.IndependentCode('POP_FRQ', D('-1'))],
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    result = classify.classify(ref, request)
    assert result.selected_path is not None
    assert result.selected_path.parent_code == 'MIS_'
    assert result.alternate_path is not None
    assert result.alternate_path.parent_code == 'SPL_'
    assert result.total == D('2')  # 3 - 1
    assert (result.band, result.vus_subband) == ('VUS', 'VUS-mid')


def test_gate_caps_class(ref: reference.Reference) -> None:
    # A +10 total (Pathogenic) under Moderate validity is capped to LP.
    path = scoring.PathInput(
        label='yellow',
        parent_code='NUL_',
        prd_initial=D('6'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
        mechanism=scoring.MechanismLevel.ESTABLISHED,
        exon=scoring.ExonRelevance.ALL,
        inf=D('4'),
        parent_cap=(D('-8'), D('10')),
    )
    request = classify.ClassificationInput(
        variant_type_paths=[path], independent_codes=[], gate_level=gene_disease_pb2.GATE_LEVEL_MODERATE
    )
    result = classify.classify(ref, request)
    assert (result.band, result.final_class, result.gate_capped) == ('P', 'LP', True)


def test_loc_family_capped_at_four(ref: reference.Reference) -> None:
    path = scoring.PathInput(label='blue', parent_code='SPL_', prd_initial=D('0'), scaling=scoring.Scaling.NONE)
    request = classify.ClassificationInput(
        variant_type_paths=[path],
        independent_codes=[classify.IndependentCode('LOC_PHE', D('4')), classify.IndependentCode('LOC_SEG', D('3'))],
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    result = classify.classify(ref, request)
    assert result.total == D('4')  # 4 + 3 = 7, LOC combined capped at +4


def test_independent_code_clamped_to_range(ref: reference.Reference) -> None:
    path = scoring.PathInput(label='blue', parent_code='SPL_', prd_initial=D('0'), scaling=scoring.Scaling.NONE)
    request = classify.ClassificationInput(
        variant_type_paths=[path],
        independent_codes=[classify.IndependentCode('POP_FRQ', D('-9'))],  # below the -6.0 floor
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    result = classify.classify(ref, request)
    assert result.total == D('-6.0')


def test_mechanism_precondition_fails_loud(ref: reference.Reference) -> None:
    # Established mechanism claimed under Limited validity is a contradiction (SM18).
    path = scoring.PathInput(
        label='yellow',
        parent_code='NUL_',
        prd_initial=D('6'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
        mechanism=scoring.MechanismLevel.ESTABLISHED,
        exon=scoring.ExonRelevance.ALL,
    )
    request = classify.ClassificationInput(
        variant_type_paths=[path], independent_codes=[], gate_level=gene_disease_pb2.GATE_LEVEL_LIMITED
    )
    with pytest.raises(ValueError, match='below Moderate'):
        classify.classify(ref, request)


def _with_independent_codes(ref: reference.Reference, codes: Sequence[classify.ScoredCode]) -> classify.Classification:
    path = scoring.PathInput(label='blue', parent_code='SPL_', prd_initial=D('0'), scaling=scoring.Scaling.NONE)
    request = classify.ClassificationInput(
        variant_type_paths=[path], independent_codes=codes, gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE
    )
    return classify.classify(ref, request)


def _conditioned_code(ref: reference.Reference) -> str:
    return sorted(ref.clinical_pop_frq_precondition.conditioned_codes)[0]


def _pop_frq(ref: reference.Reference, *, releases: tuple[provenance.Release, ...]) -> frequency.PopFrq:
    """POP_FRQ over a variant absent from gnomAD: a determination, and the points are 0.0."""
    faf = frequency.Faf(value=D('0'), support=frequency.FafSupport.ABSENT, flags=(), releases=releases)
    daft = frequency.curated_daft(D('0.0001'), source='a VCEP specification')
    return frequency.pop_frq(ref, faf, daft)


def _inadmissible_pop_frq(ref: reference.Reference) -> decimal.Decimal:
    """A POP_FRQ the code can be assigned and the precondition does not admit."""
    spec = ref.code('POP_FRQ')
    admissible = ref.clinical_pop_frq_precondition.admissible_points
    candidates = [p for p in (spec.low, spec.high, D('-3')) if spec.low <= p <= spec.high and p not in admissible]
    assert candidates, 'the precondition admits the whole POP_FRQ range; it can never withdraw a code'
    return candidates[0]


def test_a_clinical_code_awarding_points_at_an_inadmissible_pop_frq_is_withdrawn(ref: reference.Reference) -> None:
    """SM4's tables are conditioned on the POP_FRQ assignment, so outside it the code is not scored."""
    with pytest.raises(ValueError, match='withdraws the code'):
        _with_independent_codes(
            ref,
            [
                classify.IndependentCode('POP_FRQ', _inadmissible_pop_frq(ref)),
                classify.IndependentCode(_conditioned_code(ref), D('2')),
            ],
        )


def test_an_out_of_range_pop_frq_cannot_clamp_itself_into_the_gate(ref: reference.Reference) -> None:
    """The tally clamps POP_FRQ to its range; the gate must read what the caller passed.

    A positive POP_FRQ is a caller slip on a benign-only code. Clamped first it becomes 0.0, which is
    the rarity the gate is looking for, so the slip would manufacture the precondition it fails.
    """
    above_range = ref.code('POP_FRQ').high + 3
    assert above_range not in ref.clinical_pop_frq_precondition.admissible_points
    with pytest.raises(ValueError, match='withdraws the code'):
        _with_independent_codes(
            ref,
            [
                classify.IndependentCode('POP_FRQ', above_range),
                classify.IndependentCode(_conditioned_code(ref), D('2')),
            ],
        )


def test_a_clinical_code_awarding_points_without_a_pop_frq_is_withdrawn(ref: reference.Reference) -> None:
    """The ordering claim: an absent POP_FRQ is not the rarity the tables need, it is an unmet gate."""
    with pytest.raises(ValueError, match='no POP_FRQ in the tally'):
        _with_independent_codes(ref, [classify.IndependentCode(_conditioned_code(ref), D('2'))])


def _undetermined_pop_frq(ref: reference.Reference) -> frequency.PopFrq:
    """POP_FRQ over a variant 13x its threshold whose gnomAD calls failed variant QC."""
    faf = frequency.joint_faf(
        D('0.00131'), exome=frequency.Callset(allele_count=31, filters=('AS_VQSR',), flags=()), genome=None
    )
    return frequency.pop_frq(ref, faf, frequency.curated_daft(D('0.0001'), source='a VCEP specification'))


def test_a_clinical_code_awarding_points_under_an_undetermined_pop_frq_is_withdrawn(ref: reference.Reference) -> None:
    """SM4 conditions the code on an assignment, and a frequency that could not be scored is none.

    The gate cannot read this off the points: an unscoreable FAF has none, and the 0.0 it would
    otherwise be filed at is the commonest variant in the tally passing as the rarest.
    """
    with pytest.raises(ValueError, match='did not determine') as refusal:
        _with_independent_codes(
            ref, [_undetermined_pop_frq(ref), classify.IndependentCode(_conditioned_code(ref), D('2'))]
        )
    message = str(refusal.value)
    assert _conditioned_code(ref) in message  # which codes the missing determination blocks
    assert 'filter_failing' in message  # and why the frequency could not be scored


def test_a_clinical_code_scores_under_a_determined_zero(ref: reference.Reference) -> None:
    """The other half of the rule: absence from gnomAD is a determination, and it is the rarity SM4 wants."""
    result = _with_independent_codes(
        ref, [_pop_frq(ref, releases=()), classify.IndependentCode(_conditioned_code(ref), D('2'))]
    )
    assert result.total == D('2')


def test_an_undetermined_pop_frq_is_left_out_of_the_tally_rather_than_filed_at_zero(
    ref: reference.Reference,
) -> None:
    """With no conditioned code in the tally the finding is still not a line: the sum would read 0.0."""
    with pytest.raises(ValueError, match=r'POP_FRQ was not determined.*filter_failing'):
        _with_independent_codes(ref, [_undetermined_pop_frq(ref)])


def test_a_conditioned_code_at_zero_is_not_gated(ref: reference.Reference) -> None:
    """SM4 conditions the award of points. A code assessed to zero awards none, and stays reportable."""
    result = _with_independent_codes(ref, [classify.IndependentCode(_conditioned_code(ref), D('0'))])
    assert result.total == D('0')


def test_a_clinical_code_scores_at_every_admissible_pop_frq(ref: reference.Reference) -> None:
    code = _conditioned_code(ref)
    for admissible in sorted(ref.clinical_pop_frq_precondition.admissible_points):
        result = _with_independent_codes(
            ref, [classify.IndependentCode('POP_FRQ', admissible), classify.IndependentCode(code, D('2'))]
        )
        assert result.total == admissible + D('2'), f'{code} did not score at POP_FRQ {admissible}'


@pytest.mark.parametrize(
    'independent_codes',
    [
        # No cap fires: the plain case.
        [('POP_FRQ', '-1')],
        # The LOC family cap fires. Both codes range to +/-4.0, so this is reachable, and the trail
        # has to explain a total 6.0 below the lines above it.
        [('LOC_PHE', '4'), ('LOC_SEG', '2')],
        # A per-code clamp and the family cap in the same trail.
        [('POP_FRQ', '-9'), ('LOC_PHE', '4'), ('LOC_SEG', '4')],
    ],
)
def test_contributions_sum_to_total(ref: reference.Reference, independent_codes: list[tuple[str, str]]) -> None:
    """The trail is the derivation, so a column that does not add up is a wrong derivation shown.

    A cap has to land as its adjustment: an absolute line on top of the lines it bounds counts them
    twice, and the reader has no way to see which reading was meant.
    """
    path = scoring.PathInput(
        label='yellow',
        parent_code='NUL_',
        prd_initial=D('6'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
        mechanism=scoring.MechanismLevel.ESTABLISHED,
        exon=scoring.ExonRelevance.ALL,
        parent_cap=(D('-8'), D('10')),
    )
    request = classify.ClassificationInput(
        variant_type_paths=[path],
        independent_codes=[classify.IndependentCode(code, D(points)) for code, points in independent_codes],
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
    )
    result = classify.classify(ref, request)
    assert sum((c.points for c in result.contributions), D('0')) == result.total


def test_a_scored_paths_trail_sums_to_its_own_total(ref: reference.Reference) -> None:
    """The same property one level down, over a path where both a stage cap and the parent cap fire."""
    path = scoring.PathInput(
        label='yellow NMD',
        parent_code='NUL_',
        prd_initial=D('6'),
        scaling=scoring.Scaling.NONE,
        combine_stages=(
            scoring.CombineStage(
                label='NUL',
                items=(scoring.PointItem('NUL_FXN', D('4')),),
                cap=(D('-8'), D('8')),  # 6 + 4 = 10, clamped to 8
            ),
        ),
        inf=D('4'),  # 8 + 4 = 12, clamped to the parent cap's 10
        parent_cap=(D('-8'), D('10')),
    )
    result = scoring.score_path(ref, path)
    assert result.total == D('10')
    assert sum((c.points for c in result.contributions), D('0')) == result.total


def test_a_path_code_filed_as_an_independent_code_is_refused(ref: reference.Reference) -> None:
    """A path's code is bounded by the caps the path holds it to; filed here it meets none of them."""
    with pytest.raises(ValueError, match='variant-type path'):
        _with_independent_codes(ref, [classify.IndependentCode('MIS_INF', D('8'))])


def test_every_evidence_code_is_either_independent_or_carried_by_a_path(ref: reference.Reference) -> None:
    """The split covers the reference, so a code family added to it cannot slip through unclassified.

    Each code is filed at zero, which the tally scores and no other precondition objects to, so the
    only thing separating the two outcomes is the family.
    """
    for name, spec in sorted(ref.codes.items()):
        codes = [classify.IndependentCode(name, D('0'))]
        if spec.family in ref.independent_families:
            assert _with_independent_codes(ref, codes).total == D('0'), name
        else:
            with pytest.raises(ValueError, match='variant-type path'):
                _with_independent_codes(ref, codes)


def test_a_code_from_every_independent_family_scores_in_the_tally(ref: reference.Reference) -> None:
    """The families the reference calls independent are the ones the tally is there to sum."""
    result = _with_independent_codes(
        ref,
        [
            classify.IndependentCode('POP_FRQ', D('0')),  # the POP_FRQ assignment SM4 conditions CLN_AFF on
            classify.IndependentCode('CLN_AFF', D('1')),
            classify.IndependentCode('LOC_SEG', D('2')),
        ],
    )
    assert result.total == D('3')


def test_a_code_the_framework_did_not_determine_cannot_be_filed(ref: reference.Reference) -> None:
    # `frequency.pop_hmz` carries no points below its floor; forwarding that None must fail at the
    # boundary rather than deep inside the clamp. The model composes this untyped, in code mode.
    not_determined = frequency.pop_hmz(
        ref, frequency.HmzInheritance.AD, 1, penetrance_near_100pct=True, affected_not_expected_in_databases=True
    )
    with pytest.raises(ValueError, match='was not determined'):
        classify.IndependentCode('POP_HMZ', not_determined.points)  # type: ignore[arg-type] — the code-mode case


@pytest.mark.parametrize('points', [0.5, '0.5', 3])
def test_a_code_filed_as_something_other_than_a_decimal_is_refused(points: object) -> None:
    # A float is silently lossy through Decimal and the string and the int are the other code-mode
    # slips; each once reached `scoring.clamp` and failed there, or worse, did not.
    with pytest.raises(ValueError, match='must be a Decimal'):
        classify.IndependentCode('POP_FRQ', points)  # type: ignore[arg-type] — the code-mode case


def _release(source: str = 'gnomAD GraphQL', version: str = 'gnomad_r4') -> provenance.Release:
    return provenance.Release(source=source, dataset_version=version)


def test_a_door_value_is_filed_as_the_code_it_scored(ref: reference.Reference) -> None:
    """What a door returns goes into the tally as it stands: it already names its code and points."""
    result = _with_independent_codes(ref, [_pop_frq(ref, releases=())])
    assert result.total == D('0')
    assert [line.label for line in result.contributions if line.label == 'POP_FRQ']


def test_a_code_filed_twice_is_refused(ref: reference.Reference) -> None:
    """Each entry is clamped on its own before the sum, so a second line doubles the code past its range.

    Two POP_FRQ at the code's floor reach the total at -12 against a stated range of [-6, 0], and the
    precondition SM4 conditions the clinical codes on reads the assignments as a set, so -6 twice
    over and -6 once are alike to it.
    """
    with pytest.raises(ValueError, match='POP_FRQ filed more than once'):
        _with_independent_codes(
            ref, [classify.IndependentCode('POP_FRQ', D('-6')), classify.IndependentCode('POP_FRQ', D('-6'))]
        )


def test_a_door_value_filed_beside_the_same_code_is_refused(ref: reference.Reference) -> None:
    """A door's value names the code it scored, so the doubling arrives through it the same way."""
    with pytest.raises(ValueError, match='POP_FRQ filed more than once'):
        _with_independent_codes(ref, [_pop_frq(ref, releases=()), classify.IndependentCode('POP_FRQ', D('-6'))])


def test_two_distinct_codes_each_take_their_own_line(ref: reference.Reference) -> None:
    """What the check refuses is the repeat, not a second POP code."""
    result = _with_independent_codes(
        ref, [classify.IndependentCode('POP_FRQ', D('-6')), classify.IndependentCode('POP_HMZ', D('-2'))]
    )
    assert result.total == D('-8')


def test_a_codes_derivation_reaches_the_trail(ref: reference.Reference) -> None:
    # The basis is what a reviewer checks the points against; a bare total makes them reconstruct it.
    result = _with_independent_codes(ref, [_pop_frq(ref, releases=())])
    line = next(line for line in result.contributions if line.label == 'POP_FRQ')
    assert 'curated VCEP DAFT' in line.basis


def test_the_releases_behind_every_scored_value_reach_the_result(ref: reference.Reference) -> None:
    """A total is reproducible only against the releases its retrievals were made at."""
    frequency_release, path_release = _release(), _release('Ensembl VEP REST', 'VEP 116')
    path = scoring.PathInput(label='blue', parent_code='SPL_', prd_initial=D('0'), scaling=scoring.Scaling.NONE)
    request = classify.ClassificationInput(
        variant_type_paths=[path],
        independent_codes=[_pop_frq(ref, releases=(frequency_release,))],
        gate_level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE,
        releases=(path_release,),
    )
    assert classify.classify(ref, request).releases == (path_release, frequency_release)


def test_a_not_determined_code_is_left_out_of_the_tally_rather_than_filed_at_zero(ref: reference.Reference) -> None:
    # A POP_HMZ under SM3's floor: the framework determined nothing, which a 0.0 line would misreport
    # as an assessed code that contributed nothing.
    not_determined = frequency.pop_hmz(
        ref, frequency.HmzInheritance.AD, 1, penetrance_near_100pct=True, affected_not_expected_in_databases=True
    )
    with pytest.raises(ValueError, match=r'was not determined.*below SM3'):
        _with_independent_codes(ref, [not_determined])
