"""Tests for the GenCC mechanism-framework data file: shipped beside the package, its tables well-formed and priced."""

from __future__ import annotations

import pathlib
import re

import pytest

import themis.svcv4
from themis.svcv4 import scoring

_DATA_FILE = pathlib.Path(themis.svcv4.__file__).parent / 'data' / 'gencc-lof-mechanism-framework.md'
_DELIMITER_CELL = re.compile(r':?-+:?')
_NUMBER = re.compile(r'\d+(?:\.\d+)?')

Table = list[list[str]]


def _tables(text: str) -> list[Table]:
    """Every GFM table in `text`, as its rows of stripped cells — the header first, the delimiter row dropped."""
    tables: list[Table] = []
    current: Table = []
    for line in text.splitlines():
        if line.startswith('|'):
            cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
            if not all(_DELIMITER_CELL.fullmatch(cell) for cell in cells):
                current.append(cells)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def _column(table: Table, name: str) -> list[str]:
    """The body cells under the header `name`."""
    index = table[0].index(name)
    return [row[index] for row in table[1:]]


@pytest.fixture(scope='module')
def tables() -> list[Table]:
    return _tables(_DATA_FILE.read_text('utf-8'))


def test_the_file_is_shipped_beside_the_package_and_holds_tables(tables: list[Table]) -> None:
    assert tables


def test_every_row_has_the_width_of_its_header(tables: list[Table]) -> None:
    for table in tables:
        assert all(len(row) == len(table[0]) for row in table), table[0]


def test_every_criterion_is_priced(tables: list[Table]) -> None:
    criteria = [table for table in tables if 'Criterion' in table[0]]
    assert criteria
    for table in criteria:
        for criterion, points in zip(_column(table, 'Criterion'), _column(table, 'Points'), strict=True):
            assert criterion
            assert _NUMBER.search(points), criterion


def test_every_confidence_term_is_a_mechanism_level_the_library_scores(tables: list[Table]) -> None:
    bands = [table for table in tables if 'Confidence term' in table[0]]
    assert len(bands) == 1
    terms = set(_column(bands[0], 'Confidence term'))
    assert terms
    assert terms <= {level.value for level in scoring.MechanismLevel}
