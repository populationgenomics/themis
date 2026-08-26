"""Shared parser for ClinGen's KB CSV downloads (gene-validity, gene-dosage).

Both downloads share one shape: a preamble (a title line, a ``FILE CREATED: <date>``
line, a ``WEBPAGE:`` line, a ``+++`` rule), then a ``GENE SYMBOL,...`` header, then a
second ``+++`` rule before the data rows. This turns one download's text into its data
rows (each header column -> value) plus the file-creation date used as the table's
dataset version.
"""

from __future__ import annotations

import csv
import io

_HEADER_FIRST_CELL = 'GENE SYMBOL'
_FILE_CREATED_PREFIX = 'FILE CREATED:'


def _is_separator(row: list[str]) -> bool:
    """A ``+++`` rule row (its first cell is all ``+``)."""
    return bool(row) and set(row[0]) == {'+'}


def _file_created(rows: list[list[str]]) -> str:
    for row in rows:
        if row and row[0].startswith(_FILE_CREATED_PREFIX):
            return row[0][len(_FILE_CREATED_PREFIX) :].strip()
    raise ValueError('ClinGen download has no FILE CREATED line')


def _header_index(rows: list[list[str]]) -> int:
    for index, row in enumerate(rows):
        if row and row[0] == _HEADER_FIRST_CELL:
            return index
    raise ValueError(f'ClinGen download has no {_HEADER_FIRST_CELL!r} header row')


def parse(text: str) -> tuple[list[dict[str, str]], str]:
    """Parse a ClinGen KB download into its data rows and the release they came from.

    Args:
        text: The full CSV download body.

    Returns:
        A ``(rows, file_created)`` pair: ``rows`` maps each data row's header column
        to its value (preamble and ``+++`` rules dropped); ``file_created`` is the
        ``FILE CREATED`` date from the preamble, the one release the rows rest on.

    Raises:
        ValueError: If the ``FILE CREATED`` line or the ``GENE SYMBOL`` header is
            absent — an unexpected download shape, not a silent miss.
    """
    all_rows = list(csv.reader(io.StringIO(text)))
    file_created = _file_created(all_rows)
    header_index = _header_index(all_rows)
    header = all_rows[header_index]
    rows = [
        dict(zip(header, row, strict=True))
        for row in all_rows[header_index + 1 :]
        if row and row[0] and not _is_separator(row)
    ]
    return rows, file_created
