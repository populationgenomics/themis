"""The per-observation cells price from the reference tables, and refuse what they cannot price."""

from __future__ import annotations

import decimal
from collections.abc import Iterable

import pytest

from themis.svcv4 import observations, reference


def addressed_grids(ref: reference.Reference) -> tuple[tuple[str, reference.ObservationGrid], ...]:
    """Each per-observation grid, under the id prefix the expansion addresses its rows below."""
    tables = ref.per_observation
    return (
        ('CLN_UAF.ad.', tables.unaffected),
        ('CLN_AFF.ad.', tables.affected_monoallelic),
        ('CLN_AFF.arxl.', tables.affected_biallelic),
        ('CLN_DNV.', tables.de_novo),
    )


def addresses(cells: Iterable[str], prefix: str, row: str) -> bool:
    """Whether any cell id addresses this row of the grid under `prefix`.

    The match is delimited: a row's fragment is followed by the end of the id or by the separator its
    columns hang off. Plain containment would pass on another table's id, or on this table's row
    whose name another row's name is a prefix of.
    """
    addressed = prefix + row
    return any(cell_id.startswith(addressed) and cell_id[len(addressed) :][:1] in ('', '.', '_') for cell_id in cells)


def test_every_row_of_every_per_observation_table_is_priced(ref: reference.Reference) -> None:
    """A row the reference prices that no cell id addresses is an observation nothing can record.

    Both kinds of row: the ones priced per column, and the ones a table prices once for the whole
    row. Non-empty on both sides rules out a vacuous pass.
    """
    cells = observations.cell_points(ref)
    rows = [(prefix, row.cell) for prefix, grid in addressed_grids(ref) for row in (*grid.rows, *grid.collapsed_rows)]
    assert cells
    assert rows
    assert [(prefix, row) for prefix, row in rows if not addresses(cells, prefix, row)] == []


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
