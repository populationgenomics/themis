"""Tests for DAFT computation, POP_FRQ binning, and POP_HMZ."""

from __future__ import annotations

import decimal
import typing
from collections.abc import Callable

import pytest

from themis.evidence.models import evidence_pb2
from themis.rpc import clinvar_pb2, gnomad_pb2
from themis.svcv4 import clinvar_classification, frequency, provenance, reference
from themis.svcv4.tests import responses

D = decimal.Decimal


def test_daft_monoallelic_matches_fbn1_worked_output() -> None:
    # SM3's FBN1 example publishes DAFT 0.000118 for {1/5000, locus 1.0, penetrance 0.85}. The
    # formula reproduces that output only with allelic heterogeneity 1.0 (see module docstring: the
    # example's stated 0.10 is inconsistent with its output).
    daft = frequency.daft_calculator(
        frequency.Inheritance.MONOALLELIC,
        prevalence_denominator=5000,
        genetic_heterogeneity=D('1.0'),
        allelic_heterogeneity=D('1.0'),
        penetrance=D('0.85'),
    )
    assert abs(daft.value - D('0.000118')) < D('0.000001')


def test_daft_allelic_heterogeneity_scales_numerator() -> None:
    full = frequency.daft_calculator(
        frequency.Inheritance.MONOALLELIC,
        prevalence_denominator=5000,
        genetic_heterogeneity=D('1.0'),
        allelic_heterogeneity=D('1.0'),
        penetrance=D('0.85'),
    )
    tenth = frequency.daft_calculator(
        frequency.Inheritance.MONOALLELIC,
        prevalence_denominator=5000,
        genetic_heterogeneity=D('1.0'),
        allelic_heterogeneity=D('0.10'),
        penetrance=D('0.85'),
    )
    assert tenth.value == full.value * D('0.10')


def test_daft_biallelic_uses_hardy_weinberg_sqrt() -> None:
    daft = frequency.daft_calculator(
        frequency.Inheritance.BIALLELIC,
        prevalence_denominator=40000,
        genetic_heterogeneity=D('1.0'),
        allelic_heterogeneity=D('0.10'),
        penetrance=D('0.90'),
    )
    prevalence = D('1') / D('40000')
    expected = D('0.10') * (prevalence * D('1.0') / D('0.90')).sqrt()
    assert daft.value == expected


def test_daft_xlinked_rejected() -> None:
    with pytest.raises(ValueError, match='binning method'):
        frequency.daft_calculator(
            frequency.Inheritance.XLINKED,
            prevalence_denominator=5000,
            genetic_heterogeneity=D('1.0'),
            allelic_heterogeneity=D('1.0'),
            penetrance=D('0.85'),
        )


def test_daft_rejects_out_of_range_penetrance() -> None:
    with pytest.raises(ValueError, match='penetrance'):
        frequency.daft_calculator(
            frequency.Inheritance.MONOALLELIC,
            prevalence_denominator=5000,
            genetic_heterogeneity=D('1.0'),
            allelic_heterogeneity=D('1.0'),
            penetrance=D('1.5'),
        )


# --- the joint FAF and its per-callset QC gate ----------------------------------------------------

_PASSING_EXOME = frequency.Callset(allele_count=31, filters=(), flags=())
_FAILING_EXOME = frequency.Callset(allele_count=31, filters=('AS_VQSR',), flags=())


def _passing(value: str) -> frequency.Faf:
    return frequency.joint_faf(D(value), exome=_PASSING_EXOME, genome=None)


def test_joint_faf_takes_the_joint_value_when_only_genomes_call_the_variant() -> None:
    # The exome-only FAF is 0 for a genome-only variant; the joint value is what the comparison uses.
    faf = frequency.joint_faf(D('0.0013'), exome=None, genome=frequency.Callset(allele_count=34, filters=(), flags=()))
    assert faf.value == D('0.0013')
    assert faf.support is frequency.FafSupport.PASSING


def test_joint_faf_is_filter_failing_when_a_contributing_callset_failed_qc() -> None:
    # gnomAD computes the joint FAF over the failing calls too, and joint.filters cannot report it.
    faf = frequency.joint_faf(
        D('0.00131'), exome=_FAILING_EXOME, genome=frequency.Callset(allele_count=2, filters=(), flags=())
    )
    assert faf.support is frequency.FafSupport.FILTER_FAILING
    assert not faf.scoreable


def test_joint_faf_ignores_the_filters_of_a_callset_carrying_no_call() -> None:
    # An AC0-filtered callset contributes no numerator, so its filters say nothing about the FAF.
    faf = frequency.joint_faf(
        D('0.0004'), exome=frequency.Callset(allele_count=0, filters=('AC0',), flags=()), genome=_PASSING_EXOME
    )
    assert faf.support is frequency.FafSupport.PASSING


def test_joint_faf_distinguishes_absence_from_a_zero_value() -> None:
    assert frequency.absent_faf().support is frequency.FafSupport.ABSENT
    assert frequency.absent_faf().value == D('0')
    # A site gnomAD holds but calls no allele at is absent too — AC0 in every callset carrying it.
    called_nowhere = frequency.joint_faf(None, exome=frequency.Callset(0, ('AC0',), ()), genome=None)
    assert called_nowhere.support is frequency.FafSupport.ABSENT
    # gnomAD reporting no Grpmax FAF for a called variant is a 0 for the comparison, not an absence.
    uncomputed = frequency.joint_faf(None, exome=frequency.Callset(allele_count=1, filters=(), flags=()), genome=None)
    assert uncomputed.support is frequency.FafSupport.PASSING
    assert uncomputed.value == D('0')


def test_joint_faf_refuses_a_call_with_no_frequency_block() -> None:
    # Absence has its own constructor, so passing neither block is a caller who lost the payload.
    with pytest.raises(ValueError, match='absent_faf'):
        frequency.joint_faf(None, exome=None, genome=None)


def test_joint_faf_rejects_a_frequency_over_no_called_allele() -> None:
    with pytest.raises(ValueError, match='no called allele'):
        frequency.joint_faf(D('0.001'), exome=frequency.Callset(allele_count=0, filters=(), flags=()), genome=None)


def test_joint_faf_carries_the_flags_of_the_callsets_that_hold_the_variant() -> None:
    # `lcr` is a caveat the SVCv4 reference names for POP_FRQ, not a QC failure: the FAF still
    # scores, and a flag the library drops is one the report cannot mention.
    faf = frequency.joint_faf(
        D('0.0004'),
        exome=frequency.Callset(allele_count=4, filters=(), flags=('lcr',)),
        genome=frequency.Callset(allele_count=0, filters=('AC0',), flags=('segdup',)),
    )
    assert faf.support is frequency.FafSupport.PASSING
    assert faf.flags == ('lcr',)


def test_joint_faf_rejects_a_negative_frequency() -> None:
    with pytest.raises(ValueError, match='FAF must be non-negative'):
        frequency.joint_faf(D('-0.001'), exome=_PASSING_EXOME, genome=None)


def test_a_callset_cannot_carry_a_negative_allele_count() -> None:
    with pytest.raises(ValueError, match='allele count'):
        frequency.Callset(allele_count=-1, filters=(), flags=())


# --- pathogenic-variants method (the default) ----------------------------------------------------


def _plp(faf: frequency.Faf, *, classification: str = 'Pathogenic', stars: int = 1) -> frequency.ClassifiedVariant:
    return frequency.ClassifiedVariant(classification=classification, review_stars=stars, faf=faf)


# SM3's precondition, read from the library so a change to it does not silently make these pools
# undersized rather than exercising what they are testing.
_MIN_KNOWN = frequency.MIN_PATHOGENIC_VARIANTS


_TEN_VARIANTS = [
    _plp(_passing(f))
    for f in ('0.0009', '0.0008', '0.0005', '0.0004', '0.0003', '0.0002', '0.0001', '0.0001', '0.0', '0.0')
]


def _daft_value(variants: list[frequency.ClassifiedVariant], *, floor: int = 1, truncated: bool = False) -> D:
    return frequency.daft_from_pathogenic_variants(variants, review_status_floor=floor, pool_truncated=truncated).value


def test_daft_pathogenic_variants_takes_highest() -> None:
    assert _daft_value(_TEN_VARIANTS) == D('0.0009')


def test_daft_pathogenic_variants_excludes_founder_outlier() -> None:
    # A 0.02 founder spike is >5x the next-highest (0.0009) -> peeled; DAFT is the consistent max.
    assert _daft_value([_plp(_passing('0.02')), *_TEN_VARIANTS[:9]]) == D('0.0009')


@pytest.mark.parametrize(
    ('observed', 'expected'),
    [
        # One variant seen among absent ones is not a founder outlier: there is nothing to be
        # significantly higher *than*, and the gene's only frequency is the whole evidence.
        (('0.0005',), '0.0005'),
        # A spread over orders of magnitude is the ordinary shape of a gene's P/LP frequencies:
        # peeling down it would leave the DAFT three orders of magnitude below the gene's maximum.
        (('0.004', '0.0006', '0.00008', '0.00001', '0.000001'), '0.0006'),
        # Only a top >5x an *observed* neighbour is the spike SM3 describes.
        (('0.02', '0.001', '0.0008'), '0.001'),
    ],
)
def test_daft_pathogenic_variants_peels_at_most_one_observed_outlier(observed: tuple[str, ...], expected: str) -> None:
    absent = [_plp(frequency.absent_faf())] * (_MIN_KNOWN - len(observed))
    assert _daft_value([*(_plp(_passing(value)) for value in observed), *absent]) == D(expected)


def test_daft_pathogenic_variants_keeps_within_5x() -> None:
    # A top exactly 5x the next (0.005 = 5 x 0.001) is not ">5x", so it is kept.
    assert _daft_value([_plp(_passing('0.005')), *([_plp(_passing('0.001'))] * 9)]) == D('0.005')


def test_daft_pathogenic_variants_ignores_a_filter_failing_variant() -> None:
    # 0.003 is 3.3x the next-highest, so the founder-outlier peel keeps it: only the QC gate can
    # drop it.
    inflated = frequency.joint_faf(D('0.003'), exome=_FAILING_EXOME, genome=None)
    assert _daft_value([_plp(inflated), *_TEN_VARIANTS]) == D('0.0009')


# A spike the classification gate is the ONLY thing that can drop: above the pool's own maximum
# (0.0009, so admitting it moves the answer) but within 5x of it, so the founder-outlier peel — which
# would mask the gate entirely — leaves it alone.
_GATED_SPIKE = '0.002'


@pytest.mark.parametrize(
    'classification',
    [
        'Conflicting classifications of pathogenicity',  # contains the substring, is not P/LP
        'Conflicting interpretations of pathogenicity',  # ClinVar's pre-2024 spelling of the same
        'Uncertain significance',
        'Likely benign',
    ],
)
def test_daft_pathogenic_variants_counts_only_pathogenic_records(classification: str) -> None:
    # The non-P/LP record carries the gene's highest FAF; counting it would anchor the threshold on
    # a variant nobody classified as pathogenic.
    spike = _plp(_passing(_GATED_SPIKE), classification=classification)
    assert _daft_value([spike, *_TEN_VARIANTS]) == D('0.0009')


@pytest.mark.parametrize(
    'classification',
    [
        'Pathogenic, low penetrance',
        'Likely pathogenic, low penetrance',
        'Established risk allele',
        'Likely risk allele',
        'Pathogenic/Pathogenic, low penetrance',  # a qualifier on any one term disqualifies it
        'Pathogenic; risk factor',
        'Pathogenic/Likely pathogenic; risk factor',  # SERPINA1 PI*Z, ~1% in Europeans
        'Likely pathogenic; drug response',
    ],
)
def test_daft_pathogenic_variants_drops_a_qualified_or_tailed_record(classification: str) -> None:
    """The narrowing SM3 needs and SM19 does not, applied here rather than in the shared pool.

    A reduced-penetrance allele — or one a submitter separately calls a risk factor — reaches a
    frequency a fully-penetrant pathogenic variant could not, so anchoring the threshold on it
    raises the very number it is deriving. The record stays in the pool the `*_INF` rules read,
    which is what the `is_pathogenic` assertion pins.
    """
    spike = _plp(_passing(_GATED_SPIKE), classification=classification)
    assert clinvar_classification.is_pathogenic(classification)
    assert _daft_value([spike, *_TEN_VARIANTS]) == D('0.0009')


@pytest.mark.parametrize('classification', ['Pathogenic', 'Likely pathogenic', 'Pathogenic/Likely pathogenic'])
def test_daft_pathogenic_variants_counts_each_plp_description(classification: str) -> None:
    assert _daft_value([_plp(_passing('0.002'), classification=classification), *_TEN_VARIANTS]) == D('0.002')


def test_daft_pathogenic_variants_applies_the_callers_review_status_floor() -> None:
    unreviewed = _plp(_passing('0.002'), stars=0)
    assert _daft_value([unreviewed, *_TEN_VARIANTS], floor=0) == D('0.002')
    assert _daft_value([unreviewed, *_TEN_VARIANTS], floor=1) == D('0.0009')


def test_the_frozen_floor_is_criteria_provided() -> None:
    """The policy is "ClinVar criteria provided", which is the 1-star rung and no other.

    Written as the statuses rather than as the number: a floor derived from the constant would agree
    with any value it took, and the constant is what a caller passes to mean this policy.
    """
    floor = frequency.KNOWN_PATHOGENIC_REVIEW_STATUS_FLOOR
    unreviewed = _plp(_passing('0.002'), stars=0)  # "no assertion criteria provided"
    single_submitter = _plp(_passing('0.002'), stars=1)  # "criteria provided, single submitter"
    assert frequency.known_pathogenic([unreviewed], review_status_floor=floor) == []
    assert frequency.known_pathogenic([single_submitter], review_status_floor=floor) == [single_submitter]


@pytest.mark.parametrize('floor', range(frequency.MAX_REVIEW_STARS + 1))
def test_a_conflicting_record_is_excluded_by_its_classification_at_every_floor(floor: int) -> None:
    """ClinVar rates a conflicting record 1 star, so no floor on the scale can be what excludes it.

    The classification gate is: "Conflicting classifications of pathogenicity" is not a pathogenic
    assertion, and a substring test on "athogenic" that admitted it inflated a measured DAFT ninefold.
    """
    conflicting = _plp(_passing('0.5'), classification='Conflicting classifications of pathogenicity', stars=1)
    assert not frequency.known_pathogenic([conflicting], review_status_floor=floor)


def test_daft_pathogenic_variants_rejects_a_floor_outside_clinvars_star_range() -> None:
    with pytest.raises(ValueError, match='0-4 stars'):
        _daft_value(_TEN_VARIANTS, floor=5)


@pytest.mark.parametrize(
    'disqualified',
    [
        _plp(_passing('0.001'), classification='Conflicting classifications of pathogenicity'),
        _plp(_passing('0.001'), stars=0),
        # A readable FAF is as much a precondition as the classification: a maximum over nine
        # frequencies is not the threshold SM3 defines, whatever the tenth record says.
        _plp(frequency.joint_faf(D('0.001'), exome=_FAILING_EXOME, genome=None)),
    ],
)
def test_daft_pathogenic_variants_needs_ten_variants_that_actually_contribute(
    disqualified: frequency.ClassifiedVariant,
) -> None:
    with pytest.raises(ValueError, match='>= 10 known P/LP variants'):
        _daft_value([disqualified, *_TEN_VARIANTS[:9]])


def test_daft_pathogenic_variants_all_zero_raises() -> None:
    with pytest.raises(ValueError, match='no threshold derivable'):
        _daft_value([_plp(_passing('0'))] * 10)


def test_daft_pathogenic_variants_records_a_truncated_pool() -> None:
    # A maximum over a prefix is a lower bound; a caller has to be able to branch on that, not read
    # it out of prose.
    complete = frequency.daft_from_pathogenic_variants(_TEN_VARIANTS, review_status_floor=1, pool_truncated=False)
    prefix = frequency.daft_from_pathogenic_variants(_TEN_VARIANTS, review_status_floor=1, pool_truncated=True)
    assert prefix.value == complete.value
    assert prefix.lower_bound
    assert not complete.lower_bound


def test_a_frequency_nobody_established_is_neither_absent_nor_scoreable() -> None:
    # The pool is fetched against a rate-limited upstream, so "not looked up" is an ordinary state;
    # filing it as absent would assert something about gnomAD nobody checked.
    unknown = frequency.unknown_faf()
    assert unknown.support is frequency.FafSupport.UNKNOWN
    assert not unknown.scoreable
    with pytest.raises(ValueError, match='>= 10 known P/LP variants'):
        _daft_value([_plp(unknown), *_TEN_VARIANTS[:9]])


def test_pop_frq_refuses_a_variant_whose_frequency_was_never_established(ref: reference.Reference) -> None:
    # Scoring 0 would read as "no benign evidence" — a finding, from a lookup nobody made.
    with pytest.raises(ValueError, match='needs a frequency'):
        frequency.pop_frq(ref, frequency.unknown_faf(), _threshold('0.0001'))


# --- the binning method (SM3 Tables 1-6) ----------------------------------------------------------


def test_every_binning_table_the_enum_names_is_carried(ref: reference.Reference) -> None:
    """The enum is the caller's vocabulary and the reference is the data; neither validates itself."""
    assert {table.value for table in frequency.BinningTable} == set(ref.binning_grids)


def test_an_xlinked_entity_reaches_a_binned_daft_and_pop_frq_points(ref: reference.Reference) -> None:
    # The route `daft_calculator` refuses: X-linked, so the tables are the only source of a threshold.
    daft = frequency.binned_daft(
        ref,
        frequency.BinningTable.X_LINKED_DOMINANT_COMBINED,
        prevalence_denominator=10_000,
        penetrance=D('0.80'),
    )
    assert daft.method is frequency.DaftMethod.BINNING
    assert daft.value == D('0.000083300')  # Table 6, the 1/10,000 row x the 80% column
    assert frequency.pop_frq(ref, _passing(str(daft.value * 20)), daft).points == D('-6.0')


def test_a_binned_daft_rounds_prevalence_up_and_penetrance_down(ref: reference.Reference) -> None:
    """The two axes round opposite ways, so a single direction applied to both is wrong.

    SM3 §36's own worked direction: an estimated 1/2,000 takes the 1/1,000 row, and a mid-range
    penetrance the lower column.
    """
    on_the_bins = frequency.binned_daft(
        ref, frequency.BinningTable.AUTOSOMAL_DOMINANT, prevalence_denominator=1_000, penetrance=D('0.50')
    )
    between_the_bins = frequency.binned_daft(
        ref, frequency.BinningTable.AUTOSOMAL_DOMINANT, prevalence_denominator=2_000, penetrance=D('0.65')
    )
    assert between_the_bins.value == on_the_bins.value
    assert 'prevalence 1/2,000 -> the 1/1,000 bin' in between_the_bins.basis
    assert 'penetrance 0.65 -> the 50% column' in between_the_bins.basis


def test_a_rarer_disease_than_the_last_row_still_rounds_onto_it(ref: reference.Reference) -> None:
    # Rounding up is satisfiable off the rare end (1/1,000,000 is more frequent than 1/5,000,000),
    # so the axis has a floor and not a bound; only the frequent end can round onto nothing.
    floor = frequency.binned_daft(
        ref, frequency.BinningTable.AUTOSOMAL_RECESSIVE, prevalence_denominator=1_000_000, penetrance=D('0.80')
    )
    rarer = frequency.binned_daft(
        ref, frequency.BinningTable.AUTOSOMAL_RECESSIVE, prevalence_denominator=5_000_000, penetrance=D('0.80')
    )
    assert rarer.value == floor.value


@pytest.mark.parametrize(
    ('prevalence_denominator', 'penetrance', 'message'),
    [
        (200, '0.80', 'rounds up onto no row'),
        (10_000, '0.10', 'rounds down onto no column'),
    ],
)
def test_an_estimate_off_the_axes_is_refused_not_clamped(
    ref: reference.Reference, prevalence_denominator: int, penetrance: str, message: str
) -> None:
    # Clamping would answer with a cell the framework never states for the estimate given.
    with pytest.raises(ValueError, match=message):
        frequency.binned_daft(
            ref,
            frequency.BinningTable.X_LINKED_RECESSIVE_FEMALE,
            prevalence_denominator=prevalence_denominator,
            penetrance=D(penetrance),
        )


def test_a_cell_sm3_marks_says_so_in_the_basis(ref: reference.Reference) -> None:
    # The `*` is printed on three cells and defined nowhere in SM3, so it travels with the result
    # rather than being resolved into a meaning the supplement does not give it.
    marked = frequency.binned_daft(
        ref, frequency.BinningTable.AUTOSOMAL_RECESSIVE, prevalence_denominator=500, penetrance=D('0.20')
    )
    unmarked = frequency.binned_daft(
        ref, frequency.BinningTable.AUTOSOMAL_RECESSIVE, prevalence_denominator=500, penetrance=D('0.80')
    )
    assert marked.value == unmarked.value  # both 0.05000; only the marker distinguishes them
    assert '"*"' in marked.basis
    assert '"*"' not in unmarked.basis


# --- the DAFT methods are distinguishable in the result -------------------------------------------


def test_each_daft_method_names_itself(ref: reference.Reference) -> None:
    produced = (
        frequency.daft_calculator(
            frequency.Inheritance.MONOALLELIC,
            prevalence_denominator=4000,
            genetic_heterogeneity=D('0.60'),
            allelic_heterogeneity=D('0.10'),
            penetrance=D('0.80'),
        ),
        frequency.daft_from_pathogenic_variants(_TEN_VARIANTS, review_status_floor=1, pool_truncated=False),
        frequency.curated_daft(D('0.0001'), source='ClinGen Hearing Loss VCEP v3'),
        frequency.binned_daft(
            ref, frequency.BinningTable.X_LINKED_MALE, prevalence_denominator=10_000, penetrance=D('0.50')
        ),
    )
    # A method added to the enum without a constructor of its own fails here.
    assert {daft.method for daft in produced} == set(frequency.DaftMethod)


def test_a_curated_daft_must_name_its_source() -> None:
    with pytest.raises(ValueError, match='must name its source'):
        frequency.curated_daft(D('0.0001'), source='  ')


def test_a_daft_cannot_exist_without_a_positive_value_and_a_basis() -> None:
    # The invariant is the type's, not each constructor's: `pop_frq` never has to re-check it.
    with pytest.raises(ValueError, match='DAFT must be positive'):
        frequency.Daft(value=D('0'), method=frequency.DaftMethod.CURATED, lower_bound=False, basis='a VCEP')
    with pytest.raises(ValueError, match='must state what it was derived from'):
        frequency.Daft(value=D('0.001'), method=frequency.DaftMethod.CURATED, lower_bound=False, basis=' ')


@pytest.mark.parametrize('support', [frequency.FafSupport.ABSENT, frequency.FafSupport.UNKNOWN])
def test_a_faf_with_no_frequency_cannot_carry_one(support: frequency.FafSupport) -> None:
    with pytest.raises(ValueError, match='carries no frequency'):
        frequency.Faf(value=D('0.1'), support=support, flags=())


def test_a_faf_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match='non-negative'):
        frequency.Faf(value=D('-0.1'), support=frequency.FafSupport.PASSING, flags=())


def _threshold(value: str) -> frequency.Daft:
    return frequency.curated_daft(D(value), source='a worked example')


# 13x the 0.0001 threshold the tests below use — a -3.0 were the calls behind it clean — over an
# exome callset that failed variant QC.
_UNSCOREABLE = frequency.joint_faf(D('0.00131'), exome=_FAILING_EXOME, genome=None)
_PASSING_FAF = _passing('0.00001')


@pytest.mark.parametrize(
    ('faf', 'expected'),
    [
        ('0', '0.0'),  # absent -> no benignity evidence
        ('0.0001', '0.0'),  # < 1.5x DAFT
        ('0.0002', '-1.0'),  # ~1.69x
        ('0.001', '-3.0'),  # ~8.47x
        ('0.002', '-6.0'),  # ~16.9x
    ],
)
def test_pop_frq_binning(ref: reference.Reference, faf: str, expected: str) -> None:
    assert frequency.pop_frq(ref, _passing(faf), _threshold('0.000118')).points == D(expected)


def test_pop_frq_boundary_multiples(ref: reference.Reference) -> None:
    daft = _threshold('0.0001')
    assert frequency.pop_frq(ref, _passing('0.00015'), daft).points == D('-1.0')  # 1.5x inclusive lower edge
    assert frequency.pop_frq(ref, _passing('0.0015'), daft).points == D('-6.0')  # 15x


def test_pop_frq_carries_the_threshold_method_it_scored_against(ref: reference.Reference) -> None:
    # Which method produced the DAFT is part of the finding: the same points mean different things
    # under a VCEP threshold and under ten gnomAD frequencies.
    daft = frequency.daft_from_pathogenic_variants(_TEN_VARIANTS, review_status_floor=1, pool_truncated=False)
    scored = frequency.pop_frq(ref, _passing('0.002'), daft)
    assert scored.daft.method is frequency.DaftMethod.PATHOGENIC_VARIANTS
    assert scored.multiple == _passing('0.002').value / daft.value


def test_an_unscoreable_faf_determines_no_pop_frq(ref: reference.Reference) -> None:
    """A figure the framework may not read is not the 0.0 a rare variant earns; it is no score at all."""
    scored = frequency.pop_frq(ref, _UNSCOREABLE, _threshold('0.0001'))
    assert scored.points is None
    assert scored.multiple is None
    assert scored.faf.support is frequency.FafSupport.FILTER_FAILING
    assert 'filter_failing' in scored.derivation


def test_a_rare_faf_and_an_unscoreable_one_are_told_apart_by_support_not_by_points(
    ref: reference.Reference,
) -> None:
    """The zero POP_FRQ has to mean one thing: SM4 conditions the clinical codes on that assignment.

    A variant rarer than its threshold and one absent from gnomAD are both determinations of 0.0.
    The unscoreable one is 13x its threshold and only its support says so.
    """
    daft = _threshold('0.0001')
    rare = frequency.pop_frq(ref, _passing('0.00001'), daft)
    absent = frequency.pop_frq(ref, frequency.absent_faf(), daft)
    unscoreable = frequency.pop_frq(ref, _UNSCOREABLE, daft)
    assert (rare.points, absent.points, unscoreable.points) == (D('0.0'), D('0.0'), None)
    assert (rare.faf.support, absent.faf.support) == (frequency.FafSupport.PASSING, frequency.FafSupport.ABSENT)
    assert unscoreable.faf.support is frequency.FafSupport.FILTER_FAILING


@pytest.mark.parametrize(
    ('points', 'faf', 'multiple', 'expected'),
    [
        # Points off a FAF nothing may be compared against, and none off one that may.
        (D('0.0'), _UNSCOREABLE, None, 'carries points iff'),
        (None, _PASSING_FAF, None, 'carries points iff'),
        # The multiple is what selected the bin, so it stands or falls with the points.
        (D('0.0'), _PASSING_FAF, None, 'carries the multiple'),
        (None, _UNSCOREABLE, D('13.1'), 'carries the multiple'),
        # A frequency nobody established is no POP_FRQ finding, not even a not-determined one.
        (None, frequency.unknown_faf(), None, 'needs a frequency'),
    ],
)
def test_a_pop_frq_its_faf_contradicts_is_refused(
    points: decimal.Decimal | None, faf: frequency.Faf, multiple: decimal.Decimal | None, expected: str
) -> None:
    # The expected substring names which invariant fired: every message says "POP_FRQ", so matching
    # that alone would pass with the three collapsed into one.
    with pytest.raises(ValueError, match=expected):
        frequency.PopFrq(points=points, faf=faf, daft=_threshold('0.0001'), multiple=multiple)


@pytest.mark.parametrize(
    ('points', 'expected'),
    [
        (D('NaN'), 'must be finite'),  # a NaN score, not the InvalidOperation it becomes downstream
        (D('Infinity'), 'must be finite'),
        (D('1.0'), 'benignity only'),  # never toward pathogenicity
        (D('-1.0') * 0, 'negative zero'),  # a benign score that rounded away, in a curator's tally
    ],
)
def test_a_pop_frq_whose_points_are_not_a_benign_score_is_refused(points: decimal.Decimal, expected: str) -> None:
    # A scoreable FAF and a multiple beside it, so only the value the points carry is left to refuse.
    with pytest.raises(ValueError, match=expected):
        frequency.PopFrq(points=points, faf=_PASSING_FAF, daft=_threshold('0.0001'), multiple=D('13.1'))


def _gated_hmz(ref: reference.Reference, inheritance, observations: int) -> frequency.PopHmz:  # noqa: ANN001
    """`pop_hmz` with SM3 Table 7's penetrance/severity gate answered yes on both limbs."""
    return frequency.pop_hmz(
        ref, inheritance, observations, penetrance_near_100pct=True, affected_not_expected_in_databases=True
    )


@pytest.mark.parametrize(
    ('inheritance', 'observations', 'expected'),
    [
        (frequency.HmzInheritance.AD, 3, '-2.0'),  # (3-1) x -1.0
        (frequency.HmzInheritance.AD, 2, '-1.0'),
        (frequency.HmzInheritance.AR, 3, '-1.0'),  # (3-1) x -0.5
        (frequency.HmzInheritance.XLINKED, 5, '-2.0'),  # (5-1) x -0.5
    ],
)
def test_pop_hmz(ref: reference.Reference, inheritance, observations: int, expected: str) -> None:  # noqa: ANN001
    scored = _gated_hmz(ref, inheritance, observations)
    assert scored.support is frequency.HmzSupport.SCORED
    assert scored.points == D(expected)


@pytest.mark.parametrize('observations', [0, 1])
def test_pop_hmz_below_the_floor_is_not_determined(ref: reference.Reference, observations: int) -> None:
    finding = _gated_hmz(ref, frequency.HmzInheritance.AD, observations)
    assert finding.support is frequency.HmzSupport.BELOW_FLOOR
    assert finding.points is None
    assert finding.observations == observations


@pytest.mark.parametrize(('penetrance', 'severity'), [(False, True), (True, False), (False, False)])
def test_pop_hmz_under_an_unmet_precondition_is_not_determined(
    ref: reference.Reference, penetrance: bool, severity: bool
) -> None:
    """Every row of SM3 Table 7 is conditioned on the gate, so a count alone determines nothing.

    A count far past the floor against a phenotype mild enough to appear in a population database is
    the case SM3 works through: it earns no benignity, where a count-only rule scores it -44.0.
    """
    finding = frequency.pop_hmz(
        ref,
        frequency.HmzInheritance.AD,
        89,
        penetrance_near_100pct=penetrance,
        affected_not_expected_in_databases=severity,
    )
    assert finding.support is frequency.HmzSupport.PRECONDITION_UNMET
    assert finding.points is None


def test_pop_hmz_requires_the_precondition_to_be_answered(ref: reference.Reference) -> None:
    """A gate with a default is a gate the caller can pass without having considered it."""
    with pytest.raises(TypeError, match=r'penetrance_near_100pct|affected_not_expected_in_databases'):
        frequency.pop_hmz(ref, frequency.HmzInheritance.AD, 3)  # type: ignore[call-arg] — the omission under test


def test_unknown_hmz_establishes_no_count() -> None:
    assert frequency.unknown_hmz().support is frequency.HmzSupport.UNKNOWN
    assert frequency.unknown_hmz().observations is None


def test_pop_hmz_rejects_a_negative_count(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match='non-negative'):
        _gated_hmz(ref, frequency.HmzInheritance.AD, -1)


def test_no_pop_hmz_finding_carries_a_signed_zero() -> None:
    # `Decimal('-1.0') * 0` is `Decimal('-0.0')`, which prints into a curator's tally as a benign
    # score that rounded away; no construction may hand a caller one.
    with pytest.raises(ValueError, match='negative zero'):
        frequency.PopHmz(points=D('-1.0') * 0, support=frequency.HmzSupport.SCORED, observations=2)


@pytest.mark.parametrize(
    ('points', 'support', 'observations', 'expected'),
    [
        (None, frequency.HmzSupport.SCORED, 2, 'carries points iff'),  # scored, yet nothing to score with
        (D('-1.0'), frequency.HmzSupport.BELOW_FLOOR, 1, 'carries points iff'),  # points under no determination
        (D('-1.0'), frequency.HmzSupport.UNKNOWN, None, 'carries points iff'),
        (None, frequency.HmzSupport.UNKNOWN, 0, 'carries a count iff'),  # a count under a lookup nobody made
        (None, frequency.HmzSupport.BELOW_FLOOR, None, 'carries a count iff'),
        # A scored zero is a determination, so it is the state the floor cases must stay distinct
        # from — and no count under the floor may claim to be one.
        (D('0.0'), frequency.HmzSupport.SCORED, 1, 'against a floor'),
        (None, frequency.HmzSupport.BELOW_FLOOR, 2, 'against a floor'),
        (D('1.0'), frequency.HmzSupport.SCORED, 2, 'benignity only'),  # never toward pathogenicity
        (D('NaN'), frequency.HmzSupport.SCORED, 2, 'must be finite'),  # a NaN weight, not a comparison error
        (D('-Infinity'), frequency.HmzSupport.SCORED, 2, 'must be finite'),
    ],
)
def test_pop_hmz_rejects_a_finding_its_support_contradicts(
    points: decimal.Decimal | None, support: frequency.HmzSupport, observations: int | None, expected: str
) -> None:
    # The expected substring names which invariant fired: every message says "POP_HMZ", so matching
    # that alone would pass with two of the four collapsed into one.
    with pytest.raises(ValueError, match=expected):
        frequency.PopHmz(points=points, support=support, observations=observations)


def test_a_scored_zero_is_a_determination_the_floor_cases_are_not(ref: reference.Reference) -> None:
    scored_zero = frequency.PopHmz(points=D('0.0'), support=frequency.HmzSupport.SCORED, observations=2)
    assert scored_zero.points == 0
    assert _gated_hmz(ref, frequency.HmzInheritance.AR, 1) != scored_zero
    assert frequency.unknown_hmz() != scored_zero


def test_faf_is_read_at_the_joint_grpmax_path(ref: reference.Reference) -> None:
    faf = frequency.faf_from_gnomad(responses.gnomad_response())
    assert faf.value == D('8.94E-7')  # variant.joint.faf95.popmax, not either callset's own
    assert faf.support is frequency.FafSupport.PASSING
    assert faf.flags == ('lcr',)  # gnomAD's caveat flags, unioned over the callsets carrying it
    assert provenance.Release('gnomAD GraphQL', 'gnomad_r4') in faf.releases
    assert frequency.pop_frq(ref, faf, frequency.curated_daft(D('0.0001'), source='a VCEP')).points == D('0.0')


def test_a_callset_gnomad_states_as_null_is_one_it_does_not_hold_the_variant_in() -> None:
    payload = responses.gnomad_payload()
    payload['variant']['genome'] = None  # type: ignore[index] — the payload is JSON, not a message
    faf = frequency.faf_from_gnomad(responses.gnomad_response(payload))
    assert faf.support is frequency.FafSupport.PASSING
    assert faf.flags == ('lcr',)  # the genome block contributed none, rather than reading as empty


def test_a_filter_failing_callset_makes_the_joint_faf_unscoreable(ref: reference.Reference) -> None:
    # The joint FAF is computed over calls that failed variant QC, so the verdict is per callset.
    payload = responses.gnomad_payload()
    payload['variant']['exome']['filters'] = ['AS_VQSR']  # type: ignore[index] — the payload is JSON
    faf = frequency.faf_from_gnomad(responses.gnomad_response(payload))
    assert faf.support is frequency.FafSupport.FILTER_FAILING
    assert not faf.scoreable
    # The fixture variant is far rarer than this threshold, so a score would be a 0.0 — the rarity
    # SM4 conditions the clinical codes on, claimed off calls gnomAD did not stand behind.
    assert frequency.pop_frq(ref, faf, frequency.curated_daft(D('0.0001'), source='a VCEP')).points is None


def test_a_response_stating_the_whole_joint_block_as_null_is_refused() -> None:
    # The line between the two: a well-formed payload whose calls cannot be scored determines no
    # POP_FRQ, where a payload carrying no joint block at all is the upstream's shape having moved.
    payload = responses.gnomad_payload()
    payload['variant']['joint'] = None  # type: ignore[index] — the payload is JSON, not a message
    with pytest.raises(ValueError, match='no joint block'):
        frequency.faf_from_gnomad(responses.gnomad_response(payload))


@pytest.mark.parametrize(
    ('door', 'drop', 'path'),
    [
        (frequency.faf_from_gnomad, ('variant', 'joint', 'faf95'), 'variant.joint.faf95.popmax'),
        (frequency.faf_from_gnomad, ('variant', 'exome', 'filters'), 'variant.exome.filters'),
        (
            frequency.homozygotes_from_gnomad,
            ('variant', 'genome', 'homozygote_count'),
            'variant.genome.homozygote_count',
        ),
    ],
)
def test_a_path_the_payload_does_not_carry_names_itself(
    door: Callable[[gnomad_pb2.DescribeVariantResponse], object], drop: tuple[str, ...], path: str
) -> None:
    # The upstream's shape moving under a contract that still names the path: the one failure the
    # doors exist to catch, since every other reading of an absent key manufactures a score.
    payload = responses.gnomad_payload()
    block = payload
    for key in drop[:-1]:
        block = block[key]  # type: ignore[index, assignment] — the payload is JSON, not a message
    del block[drop[-1]]  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=path):
        door(responses.gnomad_response(payload))


def test_a_response_stating_no_provenance_is_refused() -> None:
    response = responses.gnomad_response()
    del response.provenance[:]
    with pytest.raises(ValueError, match='no provenance'):
        frequency.faf_from_gnomad(response)


def test_homozygote_counts_are_summed_over_the_callsets_that_passed_qc() -> None:
    counts = frequency.homozygotes_from_gnomad(responses.gnomad_response())
    assert counts.eligible == 3  # exome 1 + genome 2, both passing
    assert counts.excluded_for_qc == 0


def test_a_filter_failing_callsets_homozygotes_are_excluded_rather_than_counted() -> None:
    payload = responses.gnomad_payload()
    payload['variant']['genome']['filters'] = ['AS_VQSR']  # type: ignore[index] — the payload is JSON
    counts = frequency.homozygotes_from_gnomad(responses.gnomad_response(payload))
    assert counts.eligible == 1
    assert counts.excluded_for_qc == 2
    assert 'AS_VQSR' in counts.derivation


def test_pop_hmz_scores_the_qc_passing_occurrences_and_carries_their_account(ref: reference.Reference) -> None:
    counts = frequency.homozygotes_from_gnomad(responses.gnomad_response())
    scored = frequency.pop_hmz(
        ref,
        frequency.HmzInheritance.AR,
        counts,
        penetrance_near_100pct=True,
        affected_not_expected_in_databases=True,
    )
    # Three eligible occurrences, counted from the 2nd, at the recessive weight.
    assert scored.points == D('-1.0')
    assert scored.observations == 3
    assert 'homozygous' in scored.derivation
    assert scored.releases == counts.releases


def test_a_bare_eligible_count_stays_admissible(ref: reference.Reference) -> None:
    # Eligibility beyond QC — coverage, whether the genotype is credible — is not a filter test.
    scored = frequency.pop_hmz(
        ref, frequency.HmzInheritance.AR, 3, penetrance_near_100pct=True, affected_not_expected_in_databases=True
    )
    assert scored.points == D('-1.0')
    assert scored.counts is None
    assert scored.releases == ()


@pytest.mark.parametrize(
    ('mode', 'expected'),
    [
        (evidence_pb2.INHERITANCE_AUTOSOMAL_DOMINANT, frequency.Inheritance.MONOALLELIC),
        (evidence_pb2.INHERITANCE_AUTOSOMAL_RECESSIVE, frequency.Inheritance.BIALLELIC),
        (evidence_pb2.INHERITANCE_X_LINKED, frequency.Inheritance.XLINKED),
    ],
)
def test_a_curated_mode_reaches_the_calculators_own_partition(
    mode: evidence_pb2.Inheritance, expected: frequency.Inheritance
) -> None:
    assert frequency.daft_inheritance(mode) == expected


def test_x_linked_resolves_and_is_then_refused_by_the_calculator_itself() -> None:
    # SM3's own routing: the mode is curated, and the method it reaches is the binning tables.
    with pytest.raises(ValueError, match='binning method'):
        frequency.daft_calculator(
            frequency.daft_inheritance(evidence_pb2.INHERITANCE_X_LINKED),
            prevalence_denominator=10000,
            genetic_heterogeneity=D('1'),
            allelic_heterogeneity=D('1'),
            penetrance=D('1'),
        )


@pytest.mark.parametrize(
    ('mapping', 'mode'),
    [
        (frequency.daft_inheritance, evidence_pb2.INHERITANCE_SEMIDOMINANT),
        (frequency.daft_inheritance, evidence_pb2.INHERITANCE_MITOCHONDRIAL),
        (frequency.daft_inheritance, evidence_pb2.INHERITANCE_UNDETERMINED),
        (frequency.hmz_inheritance, evidence_pb2.INHERITANCE_Y_LINKED),
        (frequency.hmz_inheritance, evidence_pb2.INHERITANCE_UNSPECIFIED),
        (frequency.binning_tables_for, evidence_pb2.INHERITANCE_MITOCHONDRIAL),
        (frequency.binning_tables_for, evidence_pb2.INHERITANCE_UNSPECIFIED),
    ],
)
def test_a_mode_the_framework_has_no_route_for_is_refused(
    mapping: Callable[[evidence_pb2.Inheritance], object], mode: evidence_pb2.Inheritance
) -> None:
    # Naming the mode: the alternative is resolving to the nearest route, which answers the gate
    # right and the frequency arithmetic wrong.
    with pytest.raises(ValueError, match=evidence_pb2.Inheritance.Name(mode)):
        mapping(mode)


@pytest.mark.parametrize(
    'mapping', [frequency.daft_inheritance, frequency.hmz_inheritance, frequency.binning_tables_for]
)
def test_a_mode_composed_as_something_other_than_the_enum_is_refused(
    mapping: Callable[[evidence_pb2.Inheritance], object],
) -> None:
    # A bool hashes equal to AUTOSOMAL_DOMINANT, so membership alone would admit it.
    with pytest.raises(ValueError, match=r'must be an evidence_pb2\.Inheritance'):
        mapping(True)  # type: ignore[arg-type] — the code-mode case


def test_every_binning_table_the_reference_carries_is_reachable_from_some_mode(ref: reference.Reference) -> None:
    """A table no mode admits is one no caller can reach, and SM3 prints six for a reason."""
    reachable = {table for mode in frequency._BINNING_TABLES for table in frequency.binning_tables_for(mode)}
    assert {table.value for table in reachable} == set(ref.binning_grids)


def test_the_hmz_rows_the_reference_weighs_are_the_ones_a_mode_reaches(ref: reference.Reference) -> None:
    """Every mode `hmz_inheritance` resolves has a weight, so a resolution cannot fail at score time."""
    for mode in frequency._HMZ_INHERITANCE:
        scored = frequency.pop_hmz(
            ref,
            frequency.hmz_inheritance(mode),
            2,
            penetrance_near_100pct=True,
            affected_not_expected_in_databases=True,
        )
        assert scored.points is not None
        assert scored.points < 0


def _refusal(mapping: Callable[[evidence_pb2.Inheritance], object], mode: evidence_pb2.Inheritance) -> str:
    """The message the mapping refused `mode` with, or the empty string where it routed it."""
    try:
        mapping(mode)
    except ValueError as refused:
        return str(refused)
    return ''


@pytest.mark.parametrize(
    'mapping', [frequency.daft_inheritance, frequency.hmz_inheritance, frequency.binning_tables_for]
)
def test_every_mode_the_contract_curates_is_mapped_or_named_as_unroutable(
    mapping: Callable[[evidence_pb2.Inheritance], object],
) -> None:
    """No mode falls through: a member added to the contract reaches a route or a refusal naming it."""
    for value in evidence_pb2.Inheritance.values():
        mode = typing.cast('evidence_pb2.Inheritance', value)
        refusal = _refusal(mapping, mode)
        assert not refusal or evidence_pb2.Inheritance.Name(value) in refusal


def _pool_faf(value: str) -> frequency.Faf:
    return frequency.Faf(
        value=D(value),
        support=frequency.FafSupport.PASSING,
        flags=(),
        releases=(provenance.Release('gnomAD GraphQL', 'gnomad_r4'),),
    )


def _pool(count: int = 12, *, classification: str = 'Pathogenic', stars: int = 1) -> list[clinvar_pb2.ClinVarRecord]:
    return [
        responses.clinvar_record(f'VCV{index:09d}', classification=classification, review_stars=stars)
        for index in range(count)
    ]


def test_the_pool_daft_takes_its_floor_from_the_request_that_fetched_it() -> None:
    # The floor rides in the search term, so the pool IS the 2-star set; re-stating it here would
    # let a basis claim a floor the search never carried.
    records = _pool(stars=2)
    fafs = {record.clinvar_id: _pool_faf('0.00001') for record in records}
    daft = frequency.daft_from_clinvar_pool(
        responses.clinvar_describe_request(review_status_floor=2),
        responses.clinvar_describe_response(records),
        fafs,
    )
    assert daft.method is frequency.DaftMethod.PATHOGENIC_VARIANTS
    assert daft.value == D('0.00001')
    assert '>= 2 stars' in daft.basis
    assert daft.lower_bound is False
    assert provenance.Release('NCBI ClinVar', 'ClinVar 2026-08-16') in daft.releases


def test_a_truncated_pool_bounds_the_threshold_from_below() -> None:
    records = _pool()
    fafs = {record.clinvar_id: _pool_faf('0.00001') for record in records}
    daft = frequency.daft_from_clinvar_pool(
        responses.clinvar_describe_request(review_status_floor=1),
        responses.clinvar_describe_response(records, pool_truncated=True),
        fafs,
    )
    assert daft.lower_bound is True


def test_a_record_with_no_frequency_counts_toward_neither_the_pool_nor_the_maximum() -> None:
    # The frequency of a record nobody looked up is unknown, which is not an absence.
    records = _pool()
    fafs = {record.clinvar_id: _pool_faf('0.00001') for record in records[:9]}
    with pytest.raises(ValueError, match='readable FAF'):
        frequency.daft_from_clinvar_pool(
            responses.clinvar_describe_request(review_status_floor=1),
            responses.clinvar_describe_response(records),
            fafs,
        )


def test_frequencies_keyed_on_anything_but_the_record_accession_are_refused() -> None:
    # Keyed on the HGVS instead, every record reads as one whose frequency was never established.
    records = _pool()
    with pytest.raises(ValueError, match='name no record of the pool'):
        frequency.daft_from_clinvar_pool(
            responses.clinvar_describe_request(review_status_floor=1),
            responses.clinvar_describe_response(records),
            {'NM_000123.4:c.100A>G': _pool_faf('0.00001')},
        )


def test_a_callset_gnomad_states_as_null_contributes_no_count() -> None:
    payload = responses.gnomad_payload()
    payload['variant']['genome'] = None  # type: ignore[index] — the payload is JSON, not a message
    counts = frequency.homozygotes_from_gnomad(responses.gnomad_response(payload))
    assert [callset.callset for callset in counts.callsets] == ['exome']
    assert counts.eligible == 1  # the exome's own, not a zero standing in for the genome's


def test_a_variant_in_neither_callset_has_no_count_to_read() -> None:
    payload = responses.gnomad_payload()
    payload['variant']['exome'] = None  # type: ignore[index] — the payload is JSON, not a message
    payload['variant']['genome'] = None  # type: ignore[index]
    with pytest.raises(ValueError, match='neither callset'):
        frequency.homozygotes_from_gnomad(responses.gnomad_response(payload))
