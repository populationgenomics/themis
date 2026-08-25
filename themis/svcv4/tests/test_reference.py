"""Tests for loading and validating the SVCv4 reference."""

from __future__ import annotations

import decimal
import json
import pathlib
from collections.abc import Callable

import pytest

from themis.rpc import gene_disease_pb2
from themis.svcv4 import reference, scoring


def _mutated_reference(tmp_path: pathlib.Path, mutate: Callable[[dict], None]) -> pathlib.Path:
    """Write a copy of the packaged reference with `mutate` applied, returning its path."""
    data = json.loads(reference._DEFAULT_DATA.read_text())
    mutate(data)
    target = tmp_path / 'mutated.json'
    target.write_text(json.dumps(data))
    return target


def _gate_row(data: dict, level: str) -> dict:
    """The gate row for `level`, raising if the reference no longer carries one under that name."""
    for row in data['gene_disease_validity_gate']['levels']:
        if row['level'] == level:
            return row
    raise KeyError(f'the reference has no gate row named {level}')


def test_class_order_is_benign_to_pathogenic(ref: reference.Reference) -> None:
    assert ref.class_order == ('B', 'LB', 'VUS', 'LP', 'P')


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
    not, is what the loader refuses.
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
        ('CLN_DNV', '0.0', 'Infinity'),  # SM4 caps no CLN code across probands; the sum sentinel says so
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


def test_oddspath_parsed(ref: reference.Reference) -> None:
    by_strength = {level.strength: level for level in ref.oddspath}
    assert by_strength['Pathogenic-Strong'].points == decimal.Decimal('4')
    assert by_strength['Pathogenic-Strong'].odds == decimal.Decimal('18.7')
    assert by_strength['Indeterminate'].odds is None


def test_frequency_bins(ref: reference.Reference) -> None:
    assert [b.min_multiple for b in ref.frequency_bins] == [decimal.Decimal(m) for m in ('0', '1.5', '5', '15')]
    assert [b.points for b in ref.frequency_bins] == [decimal.Decimal(p) for p in ('0.0', '-1.0', '-3.0', '-6.0')]


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


def test_a_cap_side_no_supplement_bounds_loads_unbounded(ref: reference.Reference) -> None:
    """`"sum"` is the file's one spelling for an unstated bound, on a cap as on a per-code range.

    It loads as an infinity, so `clamp` against it is a no-op rather than a silent trim. SM4 sums
    the clinical codes per observation under no stated bound; SM3 states no floor on the POP sum,
    only that both its codes are benign-only.
    """
    cln = ref.concept_cap('CLN')
    assert (cln.low, cln.high) == (decimal.Decimal('-Infinity'), decimal.Decimal('Infinity'))
    pop = ref.concept_cap('POP')
    assert (pop.low, pop.high) == (decimal.Decimal('-Infinity'), decimal.Decimal('0.0'))


def test_a_bound_spelled_some_other_way_fails_loud(tmp_path: pathlib.Path) -> None:
    """An unrecognised string would otherwise read as an unbounded side no clamp ever bites on."""

    def corrupt(data: dict) -> None:
        data['cap_hierarchy']['concept_caps']['CLN'] = ['NE', 'NE']

    with pytest.raises(reference.ReferenceDataError, match="unbounded side is spelled 'sum'"):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


def test_unknown_concept_and_category_raise(ref: reference.Reference) -> None:
    with pytest.raises(reference.ReferenceDataError):
        ref.concept_cap('NOPE')
    with pytest.raises(reference.ReferenceDataError):
        ref.category_cap('NOPE_PFD')


def test_cap_hierarchy_unknown_code_fails_loud(tmp_path: pathlib.Path) -> None:
    def corrupt(data: dict) -> None:
        data['cap_hierarchy']['concept_to_codes']['MIS'] = ['MIS_PRD', 'NOT_A_CODE']

    with pytest.raises(reference.ReferenceDataError, match='unknown code'):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


def test_a_concept_grouping_a_code_of_another_family_fails_loud(tmp_path: pathlib.Path) -> None:
    """The grouping is what `independent_families` reads the split off, so it has to hold."""

    def corrupt(data: dict) -> None:
        data['cap_hierarchy']['concept_to_codes']['MIS'] = ['MIS_PRD', 'POP_FRQ']

    with pytest.raises(reference.ReferenceDataError, match='whose family is'):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


def test_a_code_without_a_family_fails_loud(tmp_path: pathlib.Path) -> None:
    """A code with no family falls on neither side of the split, so it is a load error."""

    def corrupt(data: dict) -> None:
        del data['evidence_codes']['LOC_SEG']['family']

    with pytest.raises(reference.ReferenceDataError, match=r'evidence_codes\.LOC_SEG\.family'):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


def test_the_families_split_into_the_grouped_ones_and_the_independent_ones(ref: reference.Reference) -> None:
    """The split has two sides and no third: what a concept groups, and what the tally sums alone.

    A concept naming something no code has a family for would leave a group nothing scores, which
    the load-time checks admit and this does not.
    """
    families = {spec.family for spec in ref.codes.values()}
    assert families - ref.independent_families == set(ref.concept_to_codes)


def test_cap_hierarchy_malformed_pair_fails_loud(tmp_path: pathlib.Path) -> None:
    def corrupt(data: dict) -> None:
        data['cap_hierarchy']['category_caps']['NUL_PFD'] = [-8.0, 10.0, 99.0]

    with pytest.raises(reference.ReferenceDataError, match='low, high'):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


def test_unknown_code_raises(ref: reference.Reference) -> None:
    with pytest.raises(reference.ReferenceDataError):
        ref.code('NOPE_XYZ')


def test_missing_file_raises(tmp_path) -> None:  # noqa: ANN001
    with pytest.raises(reference.ReferenceDataError):
        reference.load_reference(tmp_path / 'absent.json')


def test_reordered_frequency_bins_fail_loud(tmp_path: pathlib.Path) -> None:
    def swap(data: dict) -> None:
        bins = data['population_frequency']['POP_FRQ']['bins']
        bins[1], bins[2] = bins[2], bins[1]  # the > 1.5x and > 5x bins reordered

    with pytest.raises(reference.ReferenceDataError, match='lower multiple'):
        reference.load_reference(_mutated_reference(tmp_path, swap))


def test_the_axis_levels_a_caller_names_are_the_ones_the_matrix_holds() -> None:
    """`scoring` and the reference file state this vocabulary separately; only agreement is usable.

    A caller selects a multiplier by the enum's value, so a divergence is a `KeyError` raised
    mid-score rather than a factor the reference is missing.
    """
    ref = reference.load_reference()
    assert {level.value for level in scoring.MechanismLevel} == set(ref.mechanism_factors)
    assert {level.value for level in scoring.ExonRelevance} == set(ref.exon_factors)


@pytest.mark.parametrize('axis', ['molecular_mechanism', 'exon_relevance'])
def test_a_renamed_matrix_axis_level_fails_at_load(tmp_path: pathlib.Path, axis: str) -> None:
    def rename(data: dict) -> None:
        levels = data['mechanism_exon_matrix'][axis]
        levels['Renamed'] = levels.pop(next(iter(levels)))

    with pytest.raises(reference.ReferenceDataError, match='expected exactly'):
        reference.load_reference(_mutated_reference(tmp_path, rename))


def test_an_omitted_cell_naming_a_level_no_axis_holds_fails_at_load(tmp_path: pathlib.Path) -> None:
    """A cell that can never match is the axis product applied where the framework scores 0."""

    def corrupt(data: dict) -> None:
        data['mechanism_exon_matrix']['omitted_cell'] = 'Probable x Most: SM18 Figure 1 states 0%'

    with pytest.raises(reference.ReferenceDataError, match='the axes do not both hold'):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


def test_a_gate_level_that_lost_its_allows_fails_at_load(tmp_path: pathlib.Path) -> None:
    """A dropped key read as [] made the level terminal.

    `apply_gate` returns a terminal level's result whatever the computed band, so that class would
    have stood for every variant in the gene.
    """

    def drop_allows(data: dict) -> None:
        del _gate_row(data, 'GATE_LEVEL_MODERATE')['allows']

    with pytest.raises(reference.ReferenceDataError, match='allows'):
        reference.load_reference(_mutated_reference(tmp_path, drop_allows))


def test_a_gate_level_permitting_nothing_and_naming_nothing_fails_at_load(tmp_path: pathlib.Path) -> None:
    """It would cap every band to the lowest class while naming no reason for doing so."""

    def empty_allows(data: dict) -> None:
        _gate_row(data, 'GATE_LEVEL_MODERATE')['allows'] = []

    with pytest.raises(reference.ReferenceDataError, match='not both and not neither'):
        reference.load_reference(_mutated_reference(tmp_path, empty_allows))


def test_a_gate_level_permitting_a_class_the_bands_do_not_define_fails_at_load(tmp_path: pathlib.Path) -> None:
    """The gate ranks its allow-set against the bands, so an unknown class caps nothing."""

    def corrupt(data: dict) -> None:
        _gate_row(data, 'GATE_LEVEL_MODERATE')['allows'] = ['LP', 'PATHOGENIC']

    with pytest.raises(reference.ReferenceDataError, match='the classification bands do not define'):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


def test_a_gate_level_both_permitting_and_terminating_fails_at_load(tmp_path: pathlib.Path) -> None:
    """Which one wins would be whichever field the reader looked at first."""

    def corrupt(data: dict) -> None:
        _gate_row(data, 'GATE_LEVEL_LIMITED')['result'] = 'Do not report'

    with pytest.raises(reference.ReferenceDataError, match='not both and not neither'):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


@pytest.mark.parametrize(
    ('named', 'message'),
    [
        ('Limited', 'no GateLevel member'),  # the standard's display spelling, which the gate is not keyed by
        ('GATE_LEVEL_UNSPECIFIED', 'gates nothing'),
    ],
)
def test_a_gate_row_naming_no_curated_level_fails_at_load(tmp_path: pathlib.Path, named: str, message: str) -> None:
    """A row's `level` is resolved against the contract enum.

    Two rows gate nothing: one naming something the enum does not carry, and the one naming what the
    enum carries to mark the absence of a level.
    """

    def corrupt(data: dict) -> None:
        _gate_row(data, 'GATE_LEVEL_LIMITED')['level'] = named

    with pytest.raises(reference.ReferenceDataError, match=message):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


def test_a_gate_level_named_twice_fails_at_load(tmp_path: pathlib.Path) -> None:
    """The gate table is keyed by level, so a second row for one would replace the first's cap."""

    def corrupt(data: dict) -> None:
        _gate_row(data, 'GATE_LEVEL_LIMITED')['level'] = 'GATE_LEVEL_MODERATE'

    with pytest.raises(reference.ReferenceDataError, match='twice'):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


def test_vus_subbands_not_partitioning_vus_band_fail_loud(tmp_path: pathlib.Path) -> None:
    def shrink(data: dict) -> None:
        # VUS-high stops at +5.0, leaving the sub-bands short of the VUS band's +6.0 upper bound.
        data['classification']['vus_subclasses'][2]['points'] = '>= +4.0 to < +5.0'

    with pytest.raises(reference.ReferenceDataError, match='do not partition'):
        reference.load_reference(_mutated_reference(tmp_path, shrink))


def test_a_pop_frq_precondition_over_an_undefined_code_fails_loud(tmp_path: pathlib.Path) -> None:
    def rename(data: dict) -> None:
        data['clinical_observations']['pop_frq_precondition']['conditioned_codes'] = ['CLN_NOPE']

    with pytest.raises(reference.ReferenceDataError, match='evidence_codes does not define'):
        reference.load_reference(_mutated_reference(tmp_path, rename))


def test_a_pop_frq_precondition_outside_the_pop_frq_range_fails_loud(tmp_path: pathlib.Path) -> None:
    """An admissible value POP_FRQ can never be assigned withdraws the conditioned code on every run."""

    def widen(data: dict) -> None:
        data['clinical_observations']['pop_frq_precondition']['admissible_pop_frq_points'] = [4.0]

    with pytest.raises(reference.ReferenceDataError, match='outside the POP_FRQ range'):
        reference.load_reference(_mutated_reference(tmp_path, widen))


def test_a_critical_residue_award_promoted_to_a_code_fails_loud(tmp_path: pathlib.Path) -> None:
    """The award is scored as a modifier on the predictive code; a code of its own has a range this ignores."""

    def promote(data: dict) -> None:
        data['critical_amino_acids']['standalone_code'] = True

    with pytest.raises(reference.ReferenceDataError, match='standalone_code'):
        reference.load_reference(_mutated_reference(tmp_path, promote))


def test_a_non_positive_critical_residue_maximum_fails_loud(tmp_path: pathlib.Path) -> None:
    def zero(data: dict) -> None:
        data['critical_amino_acids']['max_points'] = 0

    with pytest.raises(reference.ReferenceDataError, match='must be positive'):
        reference.load_reference(_mutated_reference(tmp_path, zero))


@pytest.mark.parametrize(
    ('pin', 'message'),
    [
        (None, 'cited_documents'),
        ({'repository': 'r', 'note': 'n'}, 'revision'),
        ({'repository': 'r', 'revision': '', 'note': 'n'}, 'non-empty'),
        ({'repository': 'r', 'revision': 'main', 'note': 'n'}, '40-character'),
        ({'repository': 'r', 'revision': '4e7050d', 'note': 'n'}, '40-character'),
        ({'repository': ['r'], 'revision': 'a' * 40, 'note': 'n'}, 'non-empty'),
    ],
    ids=['absent', 'no-revision', 'empty-revision', 'branch-name', 'short-id', 'not-a-string'],
)
def test_a_citation_pin_that_identifies_no_commit_fails_at_load(
    tmp_path: pathlib.Path, pin: dict | None, message: str
) -> None:
    """Every SM citation in the file is a line number; only a full commit id settles which line."""

    def replace(data: dict) -> None:
        if pin is None:
            del data['meta']['cited_documents']
        else:
            data['meta']['cited_documents'] = pin

    with pytest.raises(reference.ReferenceDataError, match=message):
        reference.load_reference(_mutated_reference(tmp_path, replace))


@pytest.mark.parametrize(
    ('block', 'message'),
    [
        (None, 'provenance'),
        ({'what': 'w', 'verified_against': 'v'}, 'citation_form'),
        ({'what': 'w', 'verified_against': '', 'citation_form': 'c'}, 'non-empty'),
        ({'what': 'w', 'verified_against': ['v'], 'citation_form': 'c'}, 'non-empty'),
    ],
    ids=['absent', 'missing-key', 'empty-string', 'not-a-string'],
)
def test_a_reference_that_does_not_state_what_it_is_fails_at_load(
    tmp_path: pathlib.Path, block: dict | None, message: str
) -> None:
    """The statement travels with the file, so no copy of it circulates without one."""

    def replace(data: dict) -> None:
        if block is None:
            del data['meta']['provenance']
        else:
            data['meta']['provenance'] = block

    with pytest.raises(reference.ReferenceDataError, match=message):
        reference.load_reference(_mutated_reference(tmp_path, replace))


def _control_cells(data: dict, table: int) -> dict:
    return data['functional_assays']['control_count_lookup']['tables'][table]['cells']


def test_a_control_count_cell_scoring_against_its_tables_direction_fails_at_load(
    tmp_path: pathlib.Path,
) -> None:
    """The transposition SM20's own prose invites: §22 cites its two tables by the wrong numbers."""

    def corrupt(data: dict) -> None:
        _control_cells(data, 0)['10'][10] = -3.0

    with pytest.raises(reference.ReferenceDataError, match='one direction only'):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


def test_a_control_count_grid_that_is_not_square_fails_at_load(tmp_path: pathlib.Path) -> None:
    # Both axes are control counts, so a row shorter than the rows states a cell nothing can reach.
    def corrupt(data: dict) -> None:
        _control_cells(data, 0)['10'] = [0.0, 0.0]

    with pytest.raises(reference.ReferenceDataError, match='the grid is square'):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


def test_control_count_rows_that_are_not_counts_from_zero_fail_at_load(tmp_path: pathlib.Path) -> None:
    def corrupt(data: dict) -> None:
        cells = _control_cells(data, 0)
        cells['eleven'] = cells.pop('10')

    with pytest.raises(reference.ReferenceDataError, match='control counts from 0 up'):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))


def test_a_control_range_the_library_selects_on_fails_at_load_if_renamed(tmp_path: pathlib.Path) -> None:
    # `functional.fxn_from_controls` selects a grid by this name, so a renamed one is a KeyError
    # raised mid-score rather than at load.
    def corrupt(data: dict) -> None:
        data['functional_assays']['control_count_lookup']['tables'][1]['direction'] = 'neutral'

    with pytest.raises(reference.ReferenceDataError, match='expected one of'):
        reference.load_reference(_mutated_reference(tmp_path, corrupt))
