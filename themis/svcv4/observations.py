"""Address and price the framework's per-observation rows.

`scoring` computes a decision-tree path from tiers. The clinical and locus codes are not that shape:
SM3 Table 7, SM4 Tables 1-5, SM5's yield bins and segregation tables state a value *per observed
individual*, and a code's contribution is that value times how many individuals fall in the row. The
library has never modelled those rows, so everything that needs them re-derives them -- the agent by
hand in prose, and any projection of a curator's worksheet separately.

This module gives each row a stable **cell id** and reads its value out of the same reference
`scoring` reads, so one revision of the framework data moves every consumer at once. The
ids are the vocabulary a curation worksheet stores and a run states, which is what lets the two be
compared row by row rather than only as totals.

An id is a code, then the row's own fragments, which the reference carries beside each value. Where
a code states one table over a narrower axis than the observations it prices, the expansion is here
rather than in the data: CLN_UAF's two collapsed columns fan out over zygosity, CLN_ALT's three
values are stated once and read on both the variant and the gene axis, and LOC_PHE's step-1 gate has
cells that score nothing.

Only the independent codes are here. The variant-type path codes (`MIS_`, `NUL_`, `CDS_`, `SPL_`)
are priced by the path a `builders` call selects, from a tier plus the mechanism and exon axes; a
cell id is the wrong key for them and `builders` is where they belong.
"""

from __future__ import annotations

import dataclasses
import decimal
from collections.abc import Iterator, Mapping

from themis.svcv4 import reference


class UnknownCellError(Exception):
    """A cell id no per-observation table defines."""


@dataclasses.dataclass(frozen=True)
class _UnaffectedExpansion:
    """How SM4 Table 5's two collapsed columns fan out over the zygosities a cell id addresses.

    The framework states the table over the columns; a cell id addresses the zygosity, so the mapping
    between them is the library's reading and lives here rather than in the reference data.

    Attributes:
        dominant_column: The column a dominant MDE's unaffected individual reads, there being no
            second allele for its class to be stated over.
        zygosities: The zygosities each column prices, and under `None` the ones a band priced
            without a column prices for.
    """

    dominant_column: str
    zygosities: Mapping[str | None, tuple[str, ...]]


_UNAFFECTED = _UnaffectedExpansion(
    dominant_column='as_p',
    zygosities={
        'as_p': ('hom_hemi', 'trans_p'),
        'as_lp': ('trans_lp',),
        None: ('hom_hemi', 'trans_p', 'trans_lp'),
    },
)


def _priced(grid: reference.ObservationGrid) -> Iterator[tuple[str, str | None, decimal.Decimal]]:
    """Every cell a grid prices: the row's fragment, the column's where the cell has one, and the value.

    A row priced per column is addressed by both fragments; a collapsed row is addressed by its own
    alone, which is what `None` says. Walking the two kinds together is what keeps a collapsed row
    from being priced by the table and addressed by nothing.

    Args:
        grid: A per-observation table.

    Yields:
        (row fragment, column fragment or None, points).
    """
    for row in grid.rows:
        for column, points in zip(grid.columns, row.points, strict=True):
            yield row.cell, column.cell, points
    for row in grid.collapsed_rows:
        yield row.cell, None, row.points


def _addressed(ref: reference.Reference) -> Iterator[tuple[str, decimal.Decimal]]:
    """Every per-observation cell, as the id addressing it and what one observation in it scores.

    Raises:
        ReferenceDataError: If CLN_UAF states a column no zygosity reads.
    """
    tables = ref.per_observation

    for frequency_bin in ref.frequency_bins:
        yield f'POP_FRQ.bin.{frequency_bin.cell}', frequency_bin.points

    for weight in (tables.homozygous.dominant, tables.homozygous.other):
        yield f'POP_HMZ.{weight.cell}', weight.points

    # SM4 Table 5 collapses the zygosities into two columns: what the unaffected individual carries
    # on its own account or in trans with a P variant, and in trans with an LP one. Which zygosity
    # reads which column is the expansion below, and a band priced without a column (under-80%
    # penetrance) is that value for every one of them.
    for band, column, points in _priced(tables.unaffected):
        zygosities = _UNAFFECTED.zygosities.get(column)
        if zygosities is None:
            read = sorted(name for name in _UNAFFECTED.zygosities if name is not None)
            raise reference.ReferenceDataError(
                f'CLN_UAF states a column {column!r} no zygosity reads; the expansion reads {read}'
            )
        if column in (None, _UNAFFECTED.dominant_column):
            yield f'CLN_UAF.ad.{band}', points
        for zygosity in zygosities:
            yield f'CLN_UAF.arxl.{band}.{zygosity}', points

    # CLN_ALT is scored on two axes -- an alternate cause in this variant, and one in another gene --
    # over the same rows; the recessive row is the variant axis's alone.
    alternate = tables.alternate_cause
    for axis in ('variant', 'gene'):
        yield f'CLN_ALT.{axis}.{alternate.more_severe.cell}', alternate.more_severe.points
        yield f'CLN_ALT.{axis}.{alternate.not_more_severe.cell}', alternate.not_more_severe.points
    recessive = alternate.not_consistent_recessive
    yield f'CLN_ALT.variant.{recessive.cell}', recessive.points

    # A monoallelic proband's row and column read as one fragment, the way SM4 Table 1 names a case.
    for row, column, points in _priced(tables.affected_monoallelic):
        yield f'CLN_AFF.ad.{row if column is None else f"{row}_{column}"}', points

    for row, column, points in _priced(tables.affected_biallelic):
        yield f'CLN_AFF.arxl.{row if column is None else f"{row}.{column}"}', points

    for row, column, points in _priced(tables.de_novo):
        yield f'CLN_DNV.{row if column is None else f"{row}.{column}"}', points

    for yield_bin in tables.diagnostic_yield:
        yield f'LOC_PHE.yield.{yield_bin.cell}', yield_bin.points
    # Step 1 gates the workflow rather than scoring it.
    yield 'LOC_PHE.step1.no', decimal.Decimal(0)
    yield 'LOC_PHE.step1.yes', decimal.Decimal(0)

    for row in tables.cosegregation:
        yield f'LOC_SEG.{row.cell}', row.points


def cell_points(ref: reference.Reference) -> dict[str, decimal.Decimal]:
    """Every per-observation cell the framework prices, by id.

    Raises:
        ReferenceDataError: If two rows are addressed by one id, which would price one of them at
            the other's value, or if CLN_UAF states a column no zygosity reads.
    """
    cells: dict[str, decimal.Decimal] = {}
    for cell_id, points in _addressed(ref):
        if cell_id in cells:
            raise reference.ReferenceDataError(
                f'two per-observation rows are addressed by {cell_id!r}; one of them would be priced at the '
                "other's value"
            )
        cells[cell_id] = points
    return cells


def points_for(ref: reference.Reference, cell_id: str) -> decimal.Decimal:
    """The framework's value for one observation in this row.

    Raises:
        UnknownCellError: If no table defines the cell. Never returns zero for an unknown id: a cell
            nobody priced is a transcription this module cannot score, and scoring it as nothing
            would drop the observation from a total that still looked complete.
    """
    try:
        return cell_points(ref)[cell_id]
    except KeyError as e:
        raise UnknownCellError(f'no per-observation table prices {cell_id!r}') from e


def _code_of(cell_id: str) -> str:
    """The evidence code a cell belongs to: an id opens with its code, then addresses the row."""
    return cell_id.split('.', 1)[0]


def total(ref: reference.Reference, counts: Mapping[str, int]) -> decimal.Decimal:
    """Sum one code's observations: each cell's value times how many individuals fall in it.

    The arithmetic the clinical and locus codes reach the tally already carrying. Doing it here
    rather than in prose is what keeps a stated derivation and the number beside it from disagreeing.

    Cells of two codes are refused rather than added together, because the sum reaches the tally as
    one line under one code: the other code's observations are then bounded by a range that is not
    theirs, and the preconditions the framework states for them are checked against a code the tally
    cannot see they were filed under.

    Args:
        ref: The loaded reference.
        counts: Cell id to the number of individuals recorded in that row, every id addressing the
            same code. A negative count is refused; zero contributes nothing.

    Raises:
        UnknownCellError: If a cell id is not priced.
        ValueError: If the cells address more than one code, or a count is negative.
    """
    codes = sorted({_code_of(cell_id) for cell_id in counts})
    if len(codes) > 1:
        raise ValueError(
            f'the cells address {", ".join(codes)}; a total is the observations of one code, filed under '
            'that code — sum each code over its own cells'
        )
    priced = cell_points(ref)
    running = decimal.Decimal(0)
    for cell_id, count in counts.items():
        if count < 0:
            raise ValueError(f'{cell_id} has a negative observation count: {count}')
        try:
            running += priced[cell_id] * count
        except KeyError as e:
            raise UnknownCellError(f'no per-observation table prices {cell_id!r}') from e
    return running
