"""The per-observation cells price from the reference tables, and refuse what they cannot price."""

from __future__ import annotations

import decimal
import typing
from collections.abc import Iterable

import pytest

from themis.svcv4 import observations, reference
from themis.svcv4.models import svcv4_pb2


def addressed_grids(ref: reference.Reference) -> tuple[tuple[str, reference.ObservationGrid], ...]:
    """Each per-observation grid, under the id prefix the expansion addresses its rows below."""
    tables = ref.per_observation
    return (
        ('CLN_UAF.ad.', tables.unaffected),
        ('CLN_AFF.ad.', tables.affected_monoallelic),
        ('CLN_AFF.arxl.', tables.affected_biallelic),
        ('CLN_DNV.', tables.de_novo),
    )


def addressed_row_tables(ref: reference.Reference) -> tuple[tuple[str, tuple[reference.ObservationRow, ...]], ...]:
    """Each per-observation table the framework states as a flat list of rows, under its id prefix."""
    tables = ref.per_observation
    return (
        ('POP_HMZ.', (tables.homozygous.dominant, tables.homozygous.other)),
        ('CLN_CCS.', tables.case_control),
        ('LOC_PHE.yield.', tables.diagnostic_yield),
        ('LOC_SEG.', tables.cosegregation),
        ('LOC_SEG.non_segregation.', tables.non_segregation),
    )


def addresses(cells: Iterable[str], prefix: str, row: str) -> bool:
    """Whether any cell id addresses this row of the grid under `prefix`.

    The match is delimited: a row's fragment is followed by the end of the id or by the separator its
    columns hang off. Plain containment would pass on another table's id, or on this table's row
    whose name another row's name is a prefix of.
    """
    addressed = prefix + row
    return any(cell_id.startswith(addressed) and cell_id[len(addressed) :][:1] in ('', '.', '_') for cell_id in cells)


def test_every_row_of_every_per_observation_table_is_priced_or_reported_unvalued(ref: reference.Reference) -> None:
    """A row of the reference that no cell id addresses is an observation nothing can record.

    Every kind of row: the ones priced per column, the ones a table prices once for the whole row,
    and the flat tables. A row the framework declines to value is addressed too, by `unvalued_cells`
    — the determination is recordable and only its number is missing, so dropping it would take the
    determination away rather than leave it unscored. Non-empty on both sides rules out a vacuous
    pass.
    """
    addressed = observations.cell_points(ref).keys() | observations.unvalued_cells(ref)
    rows = [(prefix, row.cell) for prefix, grid in addressed_grids(ref) for row in (*grid.rows, *grid.collapsed_rows)]
    rows += [(prefix, row.cell) for prefix, table in addressed_row_tables(ref) for row in table]
    assert addressed
    assert rows
    assert [(prefix, row) for prefix, row in rows if not addresses(addressed, prefix, row)] == []


def test_a_row_the_framework_declines_to_value_is_reported_rather_than_priced(ref: reference.Reference) -> None:
    # SM4 Figure 1 routes an odds ratio near or below 1.0 to a statistician. Pricing it at zero would
    # read as a determination worth nothing rather than one the framework gives no number for.
    assert 'CLN_CCS.or_near_or_below_1' in observations.unvalued_cells(ref)
    assert 'CLN_CCS.or_near_or_below_1' not in observations.cell_points(ref)
    with pytest.raises(observations.UnknownCellError):
        observations.points_for(ref, 'CLN_CCS.or_near_or_below_1')


def test_the_independent_families_draw_the_path_code_split(ref: reference.Reference) -> None:
    # The split decides which codes this module may price at all, and it is the reference's own.
    assert observations.is_path_code(ref, 'MIS_PRD')
    assert not observations.is_path_code(ref, 'CLN_AFF')
    assert not observations.is_path_code(ref, 'LOC_SEG')
    assert [cell_id for cell_id in observations.cell_points(ref) if observations.is_path_code(ref, cell_id)] == []


def test_values_come_from_the_reference_tables(ref: reference.Reference) -> None:
    # One value from each table a transcription slip would change.
    assert observations.points_for(ref, 'POP_FRQ.bin.ge_15x') == decimal.Decimal('-6.0')
    assert observations.points_for(ref, 'CLN_UAF.ad.near_100') == decimal.Decimal('-4.0')
    assert observations.points_for(ref, 'CLN_AFF.ad.specific_full') == decimal.Decimal('1.0')
    assert observations.points_for(
        ref, 'CLN_AFF.arxl.consistent_full_lt_0_0001.trans_plp_confirmed'
    ) == decimal.Decimal('3.0')
    assert observations.points_for(ref, 'CLN_DNV.specific.confirmed') == decimal.Decimal('7.0')
    assert observations.points_for(ref, 'LOC_PHE.yield.ge_82') == decimal.Decimal('4.0')
    assert observations.points_for(ref, 'LOC_SEG.ar.hom_or_chet_affected') == decimal.Decimal('2.0')


def test_an_unpriced_cell_raises_rather_than_scoring_zero(ref: reference.Reference) -> None:
    with pytest.raises(observations.UnknownCellError, match='invented'):
        observations.points_for(ref, 'CLN_AFF.ad.invented')


def test_a_path_code_is_not_addressable_here(ref: reference.Reference) -> None:
    # The variant-type path codes are priced by `builders`, from a tier plus the matrix axes. Asking
    # for one by cell id is a caller mistake, not a zero.
    with pytest.raises(observations.UnknownCellError):
        observations.points_for(ref, 'MIS_PRD.bayesdel.t3')


def test_a_total_multiplies_each_row_by_its_observation_count(ref: reference.Reference) -> None:
    # Two probands on SM4 Table 1's specific/full row and one on its consistent/full row.
    counts = {'CLN_AFF.ad.specific_full': 2, 'CLN_AFF.ad.consistent_full': 1}
    expected = observations.points_for(ref, 'CLN_AFF.ad.specific_full') * 2 + observations.points_for(
        ref, 'CLN_AFF.ad.consistent_full'
    )
    assert observations.total(ref, counts) == expected


def test_a_zero_count_contributes_nothing(ref: reference.Reference) -> None:
    assert observations.total(ref, {'CLN_DNV.specific.confirmed': 0}) == decimal.Decimal(0)


def test_a_negative_count_is_refused(ref: reference.Reference) -> None:
    with pytest.raises(ValueError, match='negative'):
        observations.total(ref, {'CLN_DNV.specific.confirmed': -1})


def test_a_total_over_an_unpriced_cell_raises(ref: reference.Reference) -> None:
    with pytest.raises(observations.UnknownCellError):
        observations.total(ref, {'LOC_SEG.invented': 1})


def test_a_total_over_two_codes_is_refused(ref: reference.Reference) -> None:
    # Summed, these would reach the tally as one line: CLN_DNV at +10.0, which its unbounded upper
    # side clamps nothing of, and no CLN_AFF for SM4's rarity precondition to hold.
    counts = {'CLN_DNV.specific.confirmed': 1, 'CLN_AFF.ad.specific_full': 3}
    with pytest.raises(ValueError, match='CLN_AFF, CLN_DNV'):
        observations.total(ref, counts)


def test_every_cell_addresses_a_code_the_reference_names(ref: reference.Reference) -> None:
    # A total reads its code off the cell ids, so an id opening with anything but a code would be
    # summed into a line the tally files under a name that bounds nothing.
    cells = observations.cell_points(ref)
    assert cells
    assert [cell_id for cell_id in cells if observations._code_of(cell_id) not in ref.codes] == []


_MONOALLELIC = observations.AffectedTable.MONOALLELIC
_BIALLELIC = observations.AffectedTable.BIALLELIC


@pytest.mark.parametrize(
    ('mode', 'karyotype', 'table'),
    [
        (svcv4_pb2.MODE_OF_INHERITANCE_X_LINKED_DOMINANT, observations.Karyotype.XX, _MONOALLELIC),
        (svcv4_pb2.MODE_OF_INHERITANCE_X_LINKED_DOMINANT, observations.Karyotype.XY, _MONOALLELIC),
        (svcv4_pb2.MODE_OF_INHERITANCE_X_LINKED_RECESSIVE, observations.Karyotype.XX, _BIALLELIC),
        (svcv4_pb2.MODE_OF_INHERITANCE_X_LINKED_RECESSIVE, observations.Karyotype.XY, _MONOALLELIC),
    ],
)
def test_an_x_linked_proband_is_counted_from_the_table_its_kind_and_karyotype_select(
    mode: svcv4_pb2.ModeOfInheritance, karyotype: observations.Karyotype, table: observations.AffectedTable
) -> None:
    # SM4 §75: one heterozygous affected woman lands in a different table under the two kinds.
    assert observations.x_linked_affected_table(mode, karyotype) is table


@pytest.mark.parametrize(
    'mode',
    [
        svcv4_pb2.MODE_OF_INHERITANCE_AUTOSOMAL_DOMINANT,
        svcv4_pb2.MODE_OF_INHERITANCE_AUTOSOMAL_RECESSIVE,
        svcv4_pb2.MODE_OF_INHERITANCE_SEMIDOMINANT,
        svcv4_pb2.MODE_OF_INHERITANCE_UNSPECIFIED,
    ],
)
def test_the_karyotype_selects_a_table_only_under_an_x_linked_mode(mode: svcv4_pb2.ModeOfInheritance) -> None:
    with pytest.raises(ValueError, match='not X-linked'):
        observations.x_linked_affected_table(mode, observations.Karyotype.XX)


@pytest.mark.parametrize(
    ('mode', 'tables'),
    [
        (svcv4_pb2.MODE_OF_INHERITANCE_AUTOSOMAL_DOMINANT, {_MONOALLELIC}),
        (svcv4_pb2.MODE_OF_INHERITANCE_AUTOSOMAL_RECESSIVE, {_BIALLELIC}),
        (svcv4_pb2.MODE_OF_INHERITANCE_SEMIDOMINANT, {_MONOALLELIC, _BIALLELIC}),
        (svcv4_pb2.MODE_OF_INHERITANCE_X_LINKED_DOMINANT, {_MONOALLELIC}),
        (svcv4_pb2.MODE_OF_INHERITANCE_X_LINKED_RECESSIVE, {_MONOALLELIC, _BIALLELIC}),
    ],
)
def test_each_mode_admits_the_affected_tables_sm4_names_for_it(
    mode: svcv4_pb2.ModeOfInheritance, tables: set[observations.AffectedTable]
) -> None:
    assert set(observations.affected_tables(mode)) == tables


def test_every_stated_mode_reaches_an_affected_table() -> None:
    """A member added to the enum fails here until SM4's table for it is written down."""
    for value in svcv4_pb2.ModeOfInheritance.values():
        mode = typing.cast('svcv4_pb2.ModeOfInheritance', value)
        if mode == svcv4_pb2.MODE_OF_INHERITANCE_UNSPECIFIED:
            with pytest.raises(ValueError, match='MODE_OF_INHERITANCE_UNSPECIFIED'):
                observations.affected_tables(mode)
        else:
            assert observations.affected_tables(mode)


def test_every_affected_table_addresses_priced_cells_and_names_them_back(ref: reference.Reference) -> None:
    """The table's value opens its cell ids, so a cell is read back to the table it was priced from."""
    priced = observations.cell_points(ref)
    for table in observations.AffectedTable:
        cells = [cell_id for cell_id in priced if cell_id.startswith(f'{table.value}.')]
        assert cells, table
        assert {observations.affected_table_of(cell_id) for cell_id in cells} == {table}
    with pytest.raises(observations.UnknownCellError):
        observations.affected_table_of('CLN_DNV.specific.confirmed')


@pytest.mark.parametrize('code', ['POP_HMZ', 'CLN_UAF'])
@pytest.mark.parametrize(
    ('mode', 'tables'),
    [
        (svcv4_pb2.MODE_OF_INHERITANCE_AUTOSOMAL_DOMINANT, {'ad'}),
        (svcv4_pb2.MODE_OF_INHERITANCE_AUTOSOMAL_RECESSIVE, {'arxl'}),
        (svcv4_pb2.MODE_OF_INHERITANCE_X_LINKED_DOMINANT, {'arxl'}),
        (svcv4_pb2.MODE_OF_INHERITANCE_X_LINKED_RECESSIVE, {'arxl'}),
    ],
)
def test_the_benign_observation_tables_split_dominant_from_recessive_and_x_linked(
    code: str, mode: svcv4_pb2.ModeOfInheritance, tables: set[str]
) -> None:
    assert set(observations.mode_routed_tables(mode, code)) == tables


@pytest.mark.parametrize(('code', 'table'), [('POP_HMZ', 'arxl'), ('CLN_UAF', 'ad')])
def test_a_semidominant_entity_takes_the_table_its_supplement_prints_it_under(code: str, table: str) -> None:
    """SM3 Table 7 prices it beside the recessive rows, SM4 Table 5 under the dominant column."""
    assert observations.mode_routed_tables(svcv4_pb2.MODE_OF_INHERITANCE_SEMIDOMINANT, code) == (table,)


def test_every_mode_routed_table_addresses_priced_cells(ref: reference.Reference) -> None:
    """A table a mode reaches is one the reference prices, read back from its cells."""
    priced = observations.cell_points(ref)
    for code in ('CLN_AFF', 'CLN_UAF', 'POP_HMZ'):
        for value in svcv4_pb2.ModeOfInheritance.values():
            mode = typing.cast('svcv4_pb2.ModeOfInheritance', value)
            if mode == svcv4_pb2.MODE_OF_INHERITANCE_UNSPECIFIED:
                with pytest.raises(ValueError, match='MODE_OF_INHERITANCE_UNSPECIFIED'):
                    observations.mode_routed_tables(mode, code)
                continue
            for table in observations.mode_routed_tables(mode, code):
                cells = [cell for cell in priced if cell.startswith(f'{code}.{table}.')]
                assert cells, (code, table)
                assert {observations.mode_routed_table_of(cell) for cell in cells} == {table}


def test_a_code_whose_tables_no_mode_routes_is_refused() -> None:
    assert observations.mode_routed_table_of('CLN_DNV.specific.confirmed') is None
    with pytest.raises(ValueError, match='does not split its tables'):
        observations.mode_routed_tables(svcv4_pb2.MODE_OF_INHERITANCE_AUTOSOMAL_DOMINANT, 'CLN_DNV')
