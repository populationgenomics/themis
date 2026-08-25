"""Tests for the SVCv4 reference: what it states, and the inconsistencies it refuses to load."""

from __future__ import annotations

import dataclasses
import decimal
import itertools
from collections.abc import Sequence
from typing import cast

import pytest

from themis.rpc import gene_disease_pb2
from themis.svcv4 import data, reference, scoring


def _band(
    code: str, lower: str | None, upper: str | None, *, lower_inclusive: bool, upper_inclusive: bool
) -> reference.Band:
    return reference.Band(
        code=code,
        lower=None if lower is None else decimal.Decimal(lower),
        lower_inclusive=lower_inclusive,
        upper=None if upper is None else decimal.Decimal(upper),
        upper_inclusive=upper_inclusive,
    )


def _gate_row(level: gene_disease_pb2.GateLevel, allows: Sequence[str], result: str | None = None) -> reference.GateRow:
    return reference.GateRow(level=level, allows=frozenset(allows), result=result)


def _control_grid(direction: str, rows: Sequence[Sequence[str]], *, number: int = 1) -> reference.ControlCountGrid:
    """A control-count grid over `rows`, everything the checks do not read stated once."""
    return reference.ControlCountGrid(
        number=number,
        direction=direction,
        caption='Lookup table for a small experiment',
        applies_to='a test variant whose result falls in that control range',
        cells={
            (benign, pathogenic): decimal.Decimal(value)
            for benign, row in enumerate(rows)
            for pathogenic, value in enumerate(row)
        },
        media_file='image3.png',
        media_pixels='1634x800',
        legibility='clear',
    )


def _binning_grid(
    *,
    number: int = 1,
    title: str = 'AUTOSOMAL DOMINANT',
    denominators: tuple[int, ...] = (500, 1000),
    penetrances: tuple[str, ...] = ('0.8', '0.2'),
    thresholds: tuple[str, ...] = ('0.1', '0.2', '0.3', '0.4'),
) -> reference.BinningGrid:
    """A binning grid over the given axes, its cells filled row-major from `thresholds`."""
    columns = tuple(decimal.Decimal(value) for value in penetrances)
    values = iter(thresholds)
    return reference.BinningGrid(
        number=number,
        caption='Table: DAFT Lookup Table',
        title=title,
        applies_to='an MDE',
        prevalence_denominators=denominators,
        penetrances=columns,
        cells={
            (denominator, penetrance): decimal.Decimal(next(values))
            for denominator in denominators
            for penetrance in columns
        },
        marked=frozenset(),
        media_file='image4.png',
        media_pixels='1136x1038',
        legibility='clear',
    )


def test_the_class_order_runs_benign_to_pathogenic(ref: reference.Reference) -> None:
    """`apply_gate` ranks a permitted class by this order, so it is the bands' own ascending order.

    Named rather than pinned: every band is addressed, the outermost two are the unbounded ones, and
    each band starts where the one before it ends.
    """
    bands = [reference.band_for(ref.bands, code) for code in ref.class_order]
    assert len(bands) == len(ref.bands)
    assert (bands[0].lower, bands[-1].upper) == (None, None)
    assert all(left.upper == right.lower for left, right in itertools.pairwise(bands))


def test_bands_declared_pathogenic_to_benign_are_refused(ref: reference.Reference) -> None:
    """The declaration order is `class_order`, so a reversed one inverts every gate cap silently."""
    with pytest.raises(reference.ReferenceDataError, match='not ascending'):
        reference.validate_bands(tuple(reversed(ref.bands)), 'classification', full_line=True)


def test_bands_declared_over_nothing_are_refused() -> None:
    with pytest.raises(reference.ReferenceDataError, match='no bands'):
        reference.validate_bands((), 'classification', full_line=True)


@pytest.mark.parametrize(
    ('points', 'expected'),
    [
        (decimal.Decimal('-5'), 'B'),
        (decimal.Decimal('-4.0'), 'B'),  # B is `<= -4.0`, boundary inclusive
        (decimal.Decimal('-3.9'), 'LB'),
        (decimal.Decimal('-1.0'), 'LB'),  # LB is `> -4.0 to <= -1.0`, upper inclusive
        (decimal.Decimal('-0.9'), 'VUS'),
        (decimal.Decimal('5.9'), 'VUS'),
        (decimal.Decimal('6.0'), 'LP'),  # LP is `>= +6.0`, boundary inclusive
        (decimal.Decimal('9.9'), 'LP'),
        (decimal.Decimal('10.0'), 'P'),  # P is `>= +10.0`
    ],
)
def test_band_boundaries(ref: reference.Reference, points: decimal.Decimal, expected: str) -> None:
    band = next(b for b in ref.bands if b.contains(points))
    assert band.code == expected


def test_bands_partition_the_line(ref: reference.Reference) -> None:
    # Every band boundary lands in exactly one band (the partition validated at load).
    for value in ('-100', '-4', '-1', '0', '2', '4', '6', '10', '100'):
        matches = [b.code for b in ref.bands if b.contains(decimal.Decimal(value))]
        assert len(matches) == 1


def test_the_printed_range_and_the_band_read_from_it_agree() -> None:
    """Both are authored, so the standard's phrasing and our reading of it are held together at import.

    Non-empty rules out a vacuous pass; the disagreement itself is what the class refuses to build.
    """
    assert [entry.printed_range for entry in data.classification.CLASSES]


def test_a_printed_range_the_band_does_not_span_is_refused() -> None:
    with pytest.raises(reference.ReferenceDataError, match='the standard prints'):
        data.classification.ClassificationClass(
            band=_band('LP', '6.0', '10.0', lower_inclusive=True, upper_inclusive=False),
            printed_range='>= +6.0 to <= +10.0',
            label='Likely Pathogenic',
            posterior_prob_pathogenic='> 90% to < 99%',
        )


def test_bands_that_leave_a_gap_are_refused() -> None:
    """A total in the gap lands in no band, which `band_for_total` cannot report as a class."""
    bands = (
        _band('B', None, '-4.0', lower_inclusive=False, upper_inclusive=True),
        _band('LB', '-3.0', '-1.0', lower_inclusive=False, upper_inclusive=True),
        _band('VUS', '-1.0', None, lower_inclusive=False, upper_inclusive=False),
    )
    with pytest.raises(reference.ReferenceDataError, match='not contiguous'):
        reference.validate_bands(bands, 'classification', full_line=True)


def test_bands_that_stop_short_of_the_line_are_refused() -> None:
    bands = (
        _band('B', '-100', '-4.0', lower_inclusive=True, upper_inclusive=True),
        _band('LB', '-4.0', None, lower_inclusive=False, upper_inclusive=False),
    )
    with pytest.raises(reference.ReferenceDataError, match='full point line'):
        reference.validate_bands(bands, 'classification', full_line=True)


def test_gate_levels_present(ref: reference.Reference) -> None:
    assert ref.gate[gene_disease_pb2.GATE_LEVEL_DEFINITIVE].allows == frozenset({'P', 'LP', 'VUS', 'LB', 'B'})
    assert 'LP' not in ref.gate[gene_disease_pb2.GATE_LEVEL_LIMITED].allows
    assert ref.gate[gene_disease_pb2.GATE_LEVEL_LESS_THAN_LIMITED].result == 'Variant in Gene of Uncertain Significance'
    assert ref.gate[gene_disease_pb2.GATE_LEVEL_DISPUTED_OR_REFUTED].result == 'Do not report'


def test_the_gate_carries_every_curated_level(ref: reference.Reference) -> None:
    """A level the contract can carry but the gate does not is a gene the library cannot score.

    `DescribeGene` states one per curated entity, so a missing row would raise mid-score on
    whichever gene a curator happened to file at that level — a level added to the contract without
    one here being the way that happens. The converse, a gate carrying something the contract does
    not, is what assembly refuses.
    """
    curated = set(gene_disease_pb2.GateLevel.values()) - {gene_disease_pb2.GATE_LEVEL_UNSPECIFIED}
    assert curated <= set(ref.gate)


def test_matrix_factors(ref: reference.Reference) -> None:
    assert ref.mechanism_factors['Established'] == decimal.Decimal('1.0')
    assert ref.mechanism_factors['Suspected'] == decimal.Decimal('0.25')
    assert ref.mechanism_factors['Uncertain'] == decimal.Decimal('0')
    assert ref.exon_factors['Most'] == decimal.Decimal('0.5')


def test_code_ranges(ref: reference.Reference) -> None:
    assert (ref.code('MIS_INF').low, ref.code('MIS_INF').high) == (decimal.Decimal('-8.0'), decimal.Decimal('8.0'))
    assert ref.code('POP_FRQ').high == decimal.Decimal('0')


@pytest.mark.parametrize(
    ('code', 'low', 'high'),
    [
        ('CLN_CCS', '-8.0', '8.0'),  # SM4 states +4.0 and a benign direction but no magnitude; the calculator's
        ('CLN_DNV', '0.0', 'Infinity'),  # SM4 caps no CLN code across probands; the unbounded side says so
        ('POP_HMZ', '-Infinity', '0.0'),  # SM3 states per-occurrence tariffs and no code-level bound
        ('CDS_PRD', '-1.0', '6.0'),  # SM8 para 32 / SM10 para 32; the calculator floors it at -4.0
        ('SPL_SPA', '-6.0', '3.0'),  # the union of the per-colour ranges, not the SPL_PRD + SPL_SPA combine cap
        ('NUL_PRD', '0.0', '6.0'),  # the subgenic ceilings; the whole-gene +10 is the NUL_PFD category cap's
        ('LOC_SEG', '-4.0', '4.0'),  # SM5 para 33's non-segregation floor, which the calculator clamps away
    ],
)
def test_contested_code_ranges(ref: reference.Reference, code: str, low: str, high: str) -> None:
    """Each range resolves a disagreement between sources; the row comment names the ground its reading rests on."""
    spec = ref.code(code)
    assert (spec.low, spec.high) == (decimal.Decimal(low), decimal.Decimal(high))


def test_oddspath_scale(ref: reference.Reference) -> None:
    by_strength = {level.strength: level for level in ref.oddspath}
    assert by_strength['Pathogenic-Strong'].points == decimal.Decimal('4')
    assert by_strength['Pathogenic-Strong'].odds == decimal.Decimal('18.7')
    # The literal, not a recomputation: the scalar is divided out at a fixed precision, and a
    # recomputation here would be rounded by whatever context this process happens to be in.
    assert str(by_strength['Benign-Strong'].odds) == '0.05347593582887700534759358289'
    assert by_strength['Indeterminate'].odds is None


def test_frequency_bins(ref: reference.Reference) -> None:
    assert [b.min_multiple for b in ref.frequency_bins] == [decimal.Decimal(m) for m in ('0', '1.5', '5', '15')]
    assert [b.points for b in ref.frequency_bins] == [decimal.Decimal(p) for p in ('0.0', '-1.0', '-3.0', '-6.0')]


def test_frequency_bins_out_of_order_are_refused(ref: reference.Reference) -> None:
    """The bins are walked in order, so a swapped pair maps a ratio onto the other bin's points."""
    reordered = (ref.frequency_bins[0], ref.frequency_bins[2], ref.frequency_bins[1], ref.frequency_bins[3])
    with pytest.raises(reference.ReferenceDataError, match='strictly ascending'):
        reference.validate_frequency_bins(reordered)


def test_frequency_bins_that_leave_the_lowest_ratios_unscored_are_refused(ref: reference.Reference) -> None:
    with pytest.raises(reference.ReferenceDataError, match='starts at 0'):
        reference.validate_frequency_bins((*ref.frequency_bins[1:], ref.frequency_bins[0]))


def test_cap_hierarchy_loaded(ref: reference.Reference) -> None:
    assert ref.concept_cap('MIS') == reference.CapRange(decimal.Decimal('-8.0'), decimal.Decimal('6.0'))
    assert ref.concept_cap('LOC') == reference.CapRange(decimal.Decimal('0.0'), decimal.Decimal('4.0'))
    assert ref.category_cap('NUL_PFD') == reference.CapRange(decimal.Decimal('-8.0'), decimal.Decimal('10.0'))
    assert ref.category_cap('MIS_PFD') == reference.CapRange(decimal.Decimal('-8.0'), decimal.Decimal('9.0'))
    assert ref.concept_to_codes['SPL'] == ('SPL_PRD', 'SPL_FXN', 'SPL_INF', 'SPL_SPA')


@pytest.mark.parametrize('code', ['CLN_ALT', 'CLN_UAF', 'POP_HMZ'])
def test_per_observation_codes_do_not_cap_their_accumulating_side(ref: reference.Reference, code: str) -> None:
    """SM3 and SM4 weight these per observation and state no code cap, so a multi-observation total survives.

    Clamping to the largest single tariff (-1.0 / -4.0 / -1.0) would silently truncate two
    qualifying observations down to one, withholding benign evidence.
    """
    spec = ref.code(code)
    two_observations = decimal.Decimal('-8.0')
    assert scoring.clamp(two_observations, spec.low, spec.high) == two_observations


def test_a_cap_side_no_supplement_bounds_is_unbounded(ref: reference.Reference) -> None:
    """An unstated bound is an infinity, so `clamp` against it is a no-op rather than a silent trim.

    SM4 sums the clinical codes per observation under no stated bound; SM3 states no floor on the POP
    sum, only that both its codes are benign-only.
    """
    cln = ref.concept_cap('CLN')
    assert (cln.low, cln.high) == (reference.UNBOUNDED_LOW, reference.UNBOUNDED_HIGH)
    pop = ref.concept_cap('POP')
    assert (pop.low, pop.high) == (reference.UNBOUNDED_LOW, decimal.Decimal('0.0'))


def test_a_cap_whose_low_is_above_its_high_is_refused() -> None:
    """It admits nothing, so `clamp` against it would return a bound rather than a total."""
    with pytest.raises(reference.ReferenceDataError, match='low above high'):
        reference.CapRange(low=decimal.Decimal('6.0'), high=decimal.Decimal('-8.0'))


def test_unknown_concept_and_category_raise(ref: reference.Reference) -> None:
    with pytest.raises(reference.ReferenceDataError):
        ref.concept_cap('NOPE')
    with pytest.raises(reference.ReferenceDataError):
        ref.category_cap('NOPE_PFD')


def test_a_concept_grouping_an_unknown_code_is_refused(ref: reference.Reference) -> None:
    with pytest.raises(reference.ReferenceDataError, match='unknown code'):
        reference.validate_concept_to_codes({'MIS': ('MIS_PRD', 'NOT_A_CODE')}, ref.codes)


def test_a_concept_grouping_a_code_of_another_family_is_refused(ref: reference.Reference) -> None:
    """The grouping is what `independent_families` reads the split off, so it has to hold."""
    with pytest.raises(reference.ReferenceDataError, match='whose family is'):
        reference.validate_concept_to_codes({'MIS': ('MIS_PRD', 'POP_FRQ')}, ref.codes)


def test_the_families_split_into_the_grouped_ones_and_the_independent_ones(ref: reference.Reference) -> None:
    """The split has two sides and no third: what a concept groups, and what the tally sums alone."""
    families = {spec.family for spec in ref.codes.values()}
    assert families - ref.independent_families == set(ref.concept_to_codes)


def test_unknown_code_raises(ref: reference.Reference) -> None:
    with pytest.raises(reference.ReferenceDataError):
        ref.code('NOPE_XYZ')


def test_the_axis_levels_a_caller_names_are_the_ones_the_matrix_holds() -> None:
    """`scoring` and the reference data state this vocabulary separately; only agreement is usable.

    A caller selects a multiplier by the enum's value, so a divergence is a `KeyError` raised
    mid-score rather than a factor the reference is missing.
    """
    ref = data.load_reference()
    assert {level.value for level in scoring.MechanismLevel} == set(ref.mechanism_factors)
    assert {level.value for level in scoring.ExonRelevance} == set(ref.exon_factors)


@pytest.mark.parametrize(
    ('axis', 'levels'),
    [('MOLECULAR_MECHANISM', reference.MECHANISM_LEVELS), ('EXON_RELEVANCE', reference.EXON_LEVELS)],
)
def test_a_renamed_matrix_axis_level_is_refused(axis: str, levels: frozenset[str]) -> None:
    renamed = dict.fromkeys(sorted(levels)[1:], decimal.Decimal('0.0')) | {'Renamed': decimal.Decimal('1.0')}
    with pytest.raises(reference.ReferenceDataError, match='expected exactly'):
        reference.validate_factor_axis(renamed, levels, axis)


def test_an_omitted_cell_naming_a_level_no_axis_holds_is_refused() -> None:
    """A cell that can never match is the axis product applied where the framework scores 0."""
    with pytest.raises(reference.ReferenceDataError, match='the axes do not both hold'):
        reference.validate_omitted_cell(('Probable', 'Most'), reference.MECHANISM_LEVELS, reference.EXON_LEVELS)


def test_a_gate_level_permitting_nothing_and_naming_nothing_is_refused() -> None:
    """It would cap every band to the lowest class while naming no reason for doing so."""
    with pytest.raises(reference.ReferenceDataError, match='not both and not neither'):
        _gate_row(gene_disease_pb2.GATE_LEVEL_MODERATE, [])


def test_a_gate_level_both_permitting_and_terminating_is_refused() -> None:
    """Which one wins would be whichever field the reader looked at first."""
    with pytest.raises(reference.ReferenceDataError, match='not both and not neither'):
        _gate_row(gene_disease_pb2.GATE_LEVEL_LIMITED, ['VUS', 'LB', 'B'], 'Do not report')


def test_a_gate_level_permitting_a_class_the_bands_do_not_define_is_refused(ref: reference.Reference) -> None:
    """The gate ranks its allow-set against the bands, so an unknown class caps nothing."""
    rows = [_gate_row(gene_disease_pb2.GATE_LEVEL_MODERATE, ['LP', 'PATHOGENIC'])]
    with pytest.raises(reference.ReferenceDataError, match='the classification bands do not define'):
        reference.assemble_gate(rows, ref.class_order)


def test_a_gate_row_naming_the_absence_of_a_level_is_refused(ref: reference.Reference) -> None:
    """`GATE_LEVEL_UNSPECIFIED` is what the contract carries for a gene with no curated level."""
    rows = [_gate_row(gene_disease_pb2.GATE_LEVEL_UNSPECIFIED, ['VUS', 'LB', 'B'])]
    with pytest.raises(reference.ReferenceDataError, match='gates nothing'):
        reference.assemble_gate(rows, ref.class_order)


def test_a_gate_level_named_twice_is_refused(ref: reference.Reference) -> None:
    """The gate table is keyed by level, so a second row for one would replace the first's cap."""
    rows = [
        _gate_row(gene_disease_pb2.GATE_LEVEL_MODERATE, ['LP', 'VUS', 'LB', 'B']),
        _gate_row(gene_disease_pb2.GATE_LEVEL_MODERATE, ['VUS', 'LB', 'B']),
    ]
    with pytest.raises(reference.ReferenceDataError, match='twice'):
        reference.assemble_gate(rows, ref.class_order)


def test_vus_subbands_not_partitioning_the_vus_band_are_refused(ref: reference.Reference) -> None:
    parent = reference.band_for(ref.bands, 'VUS')
    # VUS-high stops at +5.0, leaving the sub-bands short of the VUS band's +6.0 upper bound.
    shrunk = (*ref.vus_subbands[:-1], _band('VUS-high', '4.0', '5.0', lower_inclusive=True, upper_inclusive=False))
    with pytest.raises(reference.ReferenceDataError, match='do not partition'):
        reference.validate_subbands(shrunk, parent, 'classification.vus_subclasses')


def test_a_pop_frq_precondition_over_an_undefined_code_is_refused(ref: reference.Reference) -> None:
    precondition = dataclasses.replace(ref.clinical_pop_frq_precondition, conditioned_codes=frozenset({'CLN_NOPE'}))
    with pytest.raises(reference.ReferenceDataError, match='no code for'):
        reference.validate_pop_frq_precondition(precondition, ref.codes)


def test_a_pop_frq_precondition_outside_the_pop_frq_range_is_refused(ref: reference.Reference) -> None:
    """An admissible value POP_FRQ can never be assigned withdraws the conditioned code on every run."""
    precondition = dataclasses.replace(
        ref.clinical_pop_frq_precondition, admissible_points=frozenset({decimal.Decimal('4.0')})
    )
    with pytest.raises(reference.ReferenceDataError, match='outside the POP_FRQ range'):
        reference.validate_pop_frq_precondition(precondition, ref.codes)


@pytest.mark.parametrize('empty', ['conditioned_codes', 'admissible_points'])
def test_a_pop_frq_precondition_over_nothing_is_refused(ref: reference.Reference, empty: str) -> None:
    """A gate over no code, or admitting no value, fires on every classification or on none."""
    with pytest.raises(reference.ReferenceDataError, match='at least one'):
        dataclasses.replace(ref.clinical_pop_frq_precondition, **{empty: frozenset()})


def test_a_critical_residue_award_promoted_to_a_code_is_refused() -> None:
    """The award is scored as a modifier on the predictive code; a code of its own has a range this ignores."""
    with pytest.raises(reference.ReferenceDataError, match='standalone code'):
        reference.validate_critical_residue_award(standalone_code=True, max_points=decimal.Decimal('2.0'))


@pytest.mark.parametrize('maximum', ['0', '-2.0'])
def test_a_non_positive_critical_residue_maximum_is_refused(maximum: str) -> None:
    with pytest.raises(reference.ReferenceDataError, match='must be positive'):
        reference.validate_critical_residue_award(standalone_code=False, max_points=decimal.Decimal(maximum))


@pytest.mark.parametrize(
    ('revision', 'message'),
    [
        ('', 'non-empty'),
        ('   ', 'non-empty'),
        ('main', '40-character'),
        ('4e7050d', '40-character'),
        ('4e7050dc79f12ea80e81ce03e65013f0271ba8e', '40-character'),  # 39 characters
        ('Z' * 40, '40-character'),
    ],
    ids=['empty', 'blank', 'branch-name', 'short-id', 'one-short', 'not-hex'],
)
def test_a_citation_pin_that_identifies_no_commit_is_refused(revision: str, message: str) -> None:
    """Every SM citation is a line number; only a full commit id settles which line."""
    with pytest.raises(reference.ReferenceDataError, match=message):
        reference.CitedDocuments(repository='populationgenomics/SVCv4-info', revision=revision, note='n')


def test_a_citation_pin_without_a_repository_is_refused() -> None:
    with pytest.raises(reference.ReferenceDataError, match='non-empty'):
        reference.CitedDocuments(repository='', revision='a' * 40, note='n')


@pytest.mark.parametrize('field', ['what', 'verified_against', 'citation_form'])
def test_a_reference_that_does_not_state_what_it_is_is_refused(field: str) -> None:
    """The statement travels with the transcription, so no copy of it circulates without one."""
    stated = {'what': 'w', 'verified_against': 'v', 'citation_form': 'c'} | {field: ''}
    with pytest.raises(reference.ReferenceDataError, match='non-empty'):
        reference.TranscriptionProvenance(**stated)


def test_a_control_count_cell_scoring_against_its_tables_direction_is_refused() -> None:
    """The transposition SM20's own prose invites: §22 cites its two tables by the wrong numbers."""
    with pytest.raises(reference.ReferenceDataError, match='one direction only'):
        _control_grid('pathogenic', (('0.0', '0.0'), ('0.0', '-3.0')))


def test_a_control_count_grid_that_is_not_square_is_refused() -> None:
    # Both axes are control counts, so a row shorter than the rows states a cell nothing can reach.
    with pytest.raises(reference.ReferenceDataError, match='the grid is square'):
        _control_grid('pathogenic', (('0.0', '0.0', '0.0'), ('0.0', '0.0', '0.0'), ('0.0',)))


def test_control_counts_that_do_not_start_at_zero_are_refused() -> None:
    """A grid whose lowest row is one control prices no assay that had none."""
    grid = _control_grid('pathogenic', (('0.0', '0.0'), ('0.0', '0.0')))
    with pytest.raises(reference.ReferenceDataError, match='control counts from 0 up'):
        dataclasses.replace(
            grid, cells={(benign + 1, pathogenic): points for (benign, pathogenic), points in grid.cells.items()}
        )


def test_a_control_range_the_library_selects_on_is_refused_if_renamed() -> None:
    # `functional.fxn_from_controls` selects a grid by this name, so a renamed one is a KeyError
    # raised mid-score rather than at load.
    with pytest.raises(reference.ReferenceDataError, match='is not one of'):
        _control_grid('neutral', (('0.0',),))


def test_control_count_tables_that_do_not_state_both_directions_are_refused() -> None:
    with pytest.raises(reference.ReferenceDataError, match='expected'):
        reference.assemble_control_counts((_control_grid('pathogenic', (('0.0',),)),))


def test_a_binning_axis_out_of_the_printed_order_is_refused() -> None:
    """SM3's rounding rules are read off the axis order: prevalence up a row, penetrance down a column."""
    with pytest.raises(reference.ReferenceDataError, match='strictly descending'):
        _binning_grid(penetrances=('0.2', '0.8'))
    with pytest.raises(reference.ReferenceDataError, match='strictly ascending'):
        _binning_grid(denominators=(1000, 500))


def test_a_binning_threshold_that_is_not_positive_is_refused() -> None:
    with pytest.raises(reference.ReferenceDataError, match='must be positive'):
        _binning_grid(thresholds=('0.1', '0.2', '0.3', '0'))


def test_two_binning_tables_under_one_title_are_refused() -> None:
    """A curation block names the table it read by that title, so the later one would answer for it."""
    with pytest.raises(reference.ReferenceDataError, match='titled'):
        reference.assemble_binning_grids((_binning_grid(number=1), _binning_grid(number=2)))


def test_a_binning_table_numbered_out_of_position_is_refused() -> None:
    with pytest.raises(reference.ReferenceDataError, match='sits at position'):
        reference.assemble_binning_grids((_binning_grid(number=2),))


def test_a_per_observation_row_stating_the_wrong_number_of_values_is_refused() -> None:
    """The columns are stated once for the table, so a row's values are read against them by position."""
    with pytest.raises(reference.ReferenceDataError, match='states 1 values'):
        reference.ObservationGrid(
            columns=(
                reference.ObservationColumn(cell='confirmed', heading='confirmed_parentage'),
                reference.ObservationColumn(cell='unconfirmed', heading='unconfirmed_parentage'),
            ),
            rows=(
                reference.ObservationGridRow(
                    cell='specific', description='SPECIFIC', points=reference.printed_decimals('7.0')
                ),
            ),
            collapsed_rows=(),
        )


def test_a_per_observation_table_addressing_one_row_twice_is_refused(ref: reference.Reference) -> None:
    """The cells are keyed by fragment, so the second row would price the first's id."""
    de_novo = ref.per_observation.de_novo
    with pytest.raises(reference.ReferenceDataError, match='same cell twice'):
        dataclasses.replace(de_novo, rows=de_novo.rows + de_novo.rows[:1])


@pytest.mark.parametrize('blank', ['code', 'family', 'concept'])
def test_a_code_that_does_not_name_itself_is_refused(ref: reference.Reference, blank: str) -> None:
    """A blank family is the one that bites: `independent_families` reads the path split off it."""
    with pytest.raises(reference.ReferenceDataError, match=f'blank {blank}'):
        dataclasses.replace(ref.code('MIS_PRD'), **{blank: '   '})


def test_a_gate_row_naming_no_gate_level_is_refused(ref: reference.Reference) -> None:
    """An untyped code-mode caller can hand these structures an integer the enum does not name."""
    rows = [_gate_row(cast('gene_disease_pb2.GateLevel', 99), ['VUS', 'LB', 'B'])]
    with pytest.raises(reference.ReferenceDataError, match='the levels one may name'):
        reference.assemble_gate(rows, ref.class_order)


def test_the_two_control_count_grids_are_sign_flipped_transposes(ref: reference.Reference) -> None:
    """SM20's read provenance claims it of two tables transcribed from separate images; this holds it."""
    pathogenic = ref.control_count_grid('pathogenic').cells
    benign = ref.control_count_grid('benign').cells
    assert pathogenic
    assert all(points == -benign[(column, row)] for (row, column), points in pathogenic.items())


def test_a_printed_frequency_ratio_the_bins_do_not_span_is_refused(ref: reference.Reference) -> None:
    """The printed interval is what a worksheet shows; the threshold beside it is what scores."""
    misprinted = dataclasses.replace(ref.frequency_bins[1], ratio='> 2x to < 5x DAFT')
    bins = (ref.frequency_bins[0], misprinted, *ref.frequency_bins[2:])
    with pytest.raises(reference.ReferenceDataError, match='SM3 prints'):
        reference.validate_frequency_bins(bins)


def test_an_axis_label_that_is_not_what_its_value_prints_as_is_refused() -> None:
    """The label is what a curation block quotes; the value beside it is what the lookup enters on."""
    with pytest.raises(reference.ReferenceDataError, match='heads the row'):
        data.population.PrevalenceBin(label='1/5,000', denominator=500)
    with pytest.raises(reference.ReferenceDataError, match='heads the column'):
        data.population.PenetranceColumn(label='80%', penetrance=decimal.Decimal('0.5'))


def test_subbands_declared_over_nothing_are_refused(ref: reference.Reference) -> None:
    with pytest.raises(reference.ReferenceDataError, match='no sub-bands'):
        reference.validate_subbands((), reference.band_for(ref.bands, 'VUS'), 'classification.vus_subclasses')


@pytest.mark.parametrize('odds_path', ['18.7', '1:2:3', 'x:1', ''])
def test_an_oddspath_ratio_that_is_not_two_numbers_is_refused(odds_path: str) -> None:
    """A ratio read as "no ratio" is a step the walk skips, scoring the weight of the step below it."""
    with pytest.raises(reference.ReferenceDataError, match='neither two numbers'):
        data.calibration._odds(odds_path)


def test_a_calibration_step_scoring_against_its_own_ratio_is_refused(ref: reference.Reference) -> None:
    """The ratio and the points state one thing twice, and the walk arrives at the points by the ratio."""
    inverted = dataclasses.replace(ref.oddspath[-1], points=decimal.Decimal('-8'))
    with pytest.raises(reference.ReferenceDataError, match='opposite ways'):
        reference.validate_oddspath((inverted,))


def test_a_calibration_step_with_no_ratio_scoring_points_is_refused(ref: reference.Reference) -> None:
    indeterminate = next(level for level in ref.oddspath if level.odds is None)
    with pytest.raises(reference.ReferenceDataError, match='no OddsPath ratio'):
        reference.validate_oddspath((dataclasses.replace(indeterminate, points=decimal.Decimal('2')),))


def test_no_keyed_table_the_reference_carries_is_a_writable_dict(ref: reference.Reference) -> None:
    """One `Reference` is shared, so a table handed out writable is one caller's edit in every tally."""
    keyed = [field.name for field in dataclasses.fields(ref) if field.type.startswith('Mapping[')]
    assert keyed
    assert [name for name in keyed if isinstance(getattr(ref, name), dict)] == []
