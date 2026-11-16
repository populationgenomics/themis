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


def _file_created(rows: list[list[str]], download: str) -> str:
    for row in rows:
        if row and row[0].startswith(_FILE_CREATED_PREFIX):
            return row[0][len(_FILE_CREATED_PREFIX) :].strip()
    raise ValueError(f'ClinGen {download} download has no FILE CREATED line')


def _header_index(rows: list[list[str]], download: str) -> int:
    for index, row in enumerate(rows):
        if row and row[0] == _HEADER_FIRST_CELL:
            return index
    raise ValueError(f'ClinGen {download} download has no {_HEADER_FIRST_CELL!r} header row')


def parse(text: str, *, download: str) -> tuple[list[dict[str, str]], str]:
    """Parse a ClinGen KB download into its data rows and the release they came from.

    Args:
        text: The full CSV download body.
        download: Which download the text is (``gene-validity``, ``gene-dosage``), named by
            every error raised.

    Returns:
        A ``(rows, file_created)`` pair: ``rows`` maps each data row's header column
        to its value (preamble and ``+++`` rules dropped); ``file_created`` is the
        ``FILE CREATED`` date from the preamble, the one release the rows rest on.

    Raises:
        ValueError: If the ``FILE CREATED`` line or the ``GENE SYMBOL`` header is
            absent (an unexpected download shape, not a silent miss), or if a data row
            has more or fewer fields than the header or records a blank gene symbol.
    """
    reader = csv.reader(io.StringIO(text))
    # A quoted field can span lines: the number recorded is the row's last line.
    numbered = [(reader.line_num, row) for row in reader]
    all_rows = [row for _, row in numbered]
    file_created = _file_created(all_rows, download)
    header_index = _header_index(all_rows, download)
    header = all_rows[header_index]
    rows: list[dict[str, str]] = []
    for line, row in numbered[header_index + 1 :]:
        if not any(row) or _is_separator(row):
            continue
        if len(row) != len(header):
            raise ValueError(
                f'ClinGen {download} download line {line} has {len(row)} fields where the header has {len(header)}'
            )
        if not row[0].strip():
            raise ValueError(f'ClinGen {download} download line {line} records no gene symbol: {row}')
        rows.append(dict(zip(header, row, strict=True)))
    return rows, file_created
