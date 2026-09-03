"""Two readings of SM3's six DAFT lookup tables, held against each other.

`apps/web/daft-tables.json` is the worksheet's reading, transcribed from the ClinGen Pilot
Calculator's rendering of the tables and exported by `bun run daft`. `themis/svcv4/data/population.py`
is the library's, read independently off SM3's own images. Neither is checkable against the other by
inspection — 144 cells each — and a misread in either reaches a curator or a tally silently.

Where they part, the difference is a fact about the framework rather than a typo, and there are six
such cells: SM3 prints them as `0.05` with a `*` it defines nowhere, and the calculator prints them
uncapped. Those six are pinned here, so a seventh divergence fails.
"""

from __future__ import annotations

import decimal
import json
import pathlib
from typing import TypedDict

import pytest

from themis.svcv4 import data, reference

_EXPORT = pathlib.Path(__file__).resolve().parents[3] / 'apps' / 'web' / 'daft-tables.json'

# The cells the two readings disagree on, as `title|prevalence denominator|penetrance`. Every one is a
# value SM3 capped at 0.05 and marked with a `*`; the assertions below hold that identity both ways.
_CAPPED_BY_SM3 = frozenset(
    {
        'AUTOSOMAL RECESSIVE|500|0.5',
        'AUTOSOMAL RECESSIVE|500|0.2',
        'AUTOSOMAL RECESSIVE|1000|0.2',
        'X-LINKED RECESSIVE - FEMALE (sex-specific prevalence)|500|0.5',
        'X-LINKED RECESSIVE - FEMALE (sex-specific prevalence)|500|0.2',
        'X-LINKED RECESSIVE - FEMALE (sex-specific prevalence)|1000|0.2',
    }
)


class ExportedRow(TypedDict):
    """One row of one table, as `apps/web/scripts/daft.ts` writes it.

    `thresholds` is keyed by the penetrance column's printed label, so a reordered column fails the
    join rather than comparing against its neighbour.
    """

    prevalence: str
    thresholds: dict[str, str]


class ExportedTable(TypedDict):
    """One table, as `apps/web/scripts/daft.ts` writes it."""

    title: str
    rows: list[ExportedRow]


def _penetrance(label: str) -> decimal.Decimal:
    """The fraction a penetrance column's printed label stands for, `80%` reading as `0.8`.

    Raises:
        ValueError: If the label is not a percentage, which would otherwise compare as a missing
            column.
    """
    if not label.endswith('%'):
        raise ValueError(f'{label!r} is not a penetrance column label of the form N%')
    return decimal.Decimal(label.removesuffix('%')) / 100


def _denominator(label: str) -> int:
    """The X in a prevalence of `1/X`, as the calculator prints it.

    The calculator's male table writes `1/1000` where the other five write `1/1,000`, so the
    separators are stripped rather than required.

    Raises:
        ValueError: If the label is not a prevalence, which would otherwise compare as a missing row.
    """
    ratio, _, denominator = label.partition('/')
    if ratio != '1' or not denominator:
        raise ValueError(f'{label!r} is not a prevalence of the form 1/X')
    return int(denominator.replace(',', ''))


@pytest.fixture(scope='module')
def ref() -> reference.Reference:
    return data.load_reference()


@pytest.fixture(scope='module')
def exported() -> list[ExportedTable]:
    if not _EXPORT.exists():
        raise AssertionError(f'{_EXPORT} is missing; regenerate it with `bun run daft` in apps/web')
    tables: list[ExportedTable] = json.loads(_EXPORT.read_text('utf-8'))['tables']
    # Non-empty rules out a vacuous pass if the generator ever stops walking the tables.
    assert tables, 'the export lists no table'
    return tables


def test_both_readings_name_the_same_tables(ref: reference.Reference, exported: list[ExportedTable]) -> None:
    """A table one side names and the other does not is a lookup one of them cannot serve."""
    assert sorted(table['title'] for table in exported) == sorted(ref.binning_grids)


def test_both_readings_agree_on_every_cell_but_the_ones_sm3_capped(
    ref: reference.Reference, exported: list[ExportedTable]
) -> None:
    """Every threshold agrees, or is one of the six the pinned set names.

    Over the full grid rather than a sample: a one-digit slip in any of the 144 cells moves a POP_FRQ
    bin, and the other reading is the only thing that can see it. Both axes are compared too, so a
    row or column the two sides address differently fails here rather than comparing as absent.
    """
    divergent: list[str] = []
    compared = 0
    for table in exported:
        grid = ref.binning_grid(table['title'])
        assert [_denominator(row['prevalence']) for row in table['rows']] == list(grid.prevalence_denominators)
        for row in table['rows']:
            denominator = _denominator(row['prevalence'])
            assert [_penetrance(label) for label in row['thresholds']] == list(grid.penetrances)
            for label, threshold in row['thresholds'].items():
                compared += 1
                if decimal.Decimal(threshold) != grid.cells[denominator, _penetrance(label)]:
                    divergent.append(f'{table["title"]}|{denominator}|{_penetrance(label)}')
    # 6 tables x 8 prevalences x 3 penetrances.
    assert compared == 144
    assert set(divergent) == _CAPPED_BY_SM3


def test_sm3_marks_exactly_the_cells_the_two_readings_disagree_on(ref: reference.Reference) -> None:
    """The `*` and the divergence are one fact; if they part, one of the two readings has moved."""
    marked = {
        f'{title}|{denominator}|{penetrance}'
        for title, grid in ref.binning_grids.items()
        for denominator, penetrance in grid.marked
    }
    assert marked == _CAPPED_BY_SM3
