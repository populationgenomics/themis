"""HGNC: the symbol HGNC approves for an HGNC id, and the symbols it approved for the gene before.

The complete set (tab-separated, one row per gene record) is parsed once from its pinned copy in the
reference bucket and indexed by HGNC id. ``lookup`` returns the approved symbol of the gene an id
names, with the gene's previous symbols. An id the set does not hold is a ``None`` result, and so is
an id it holds as withdrawn: HGNC approves no symbol under either.

HGNC has moved symbols between genes, so one gene's previous symbol can be another gene's approved
symbol, or a previous symbol of another gene too. A caller that compares symbols to confirm an id
would take such a symbol as confirming the wrong gene, so the previous symbols returned are only the
ones that name no other gene in the set.

Only the id, the symbol, the status and the previous symbols of a row are read.
"""

from __future__ import annotations

import codecs
import collections
import csv
import dataclasses
import io
import re
from collections.abc import Mapping

_SOURCE = 'HGNC'

_HGNC_COL = 'hgnc_id'
_SYMBOL_COL = 'symbol'
_STATUS_COL = 'status'
_PREVIOUS_COL = 'prev_symbol'
_REQUIRED_COLS = (_HGNC_COL, _SYMBOL_COL, _STATUS_COL, _PREVIOUS_COL)

_APPROVED = 'Approved'
_WITHDRAWN = 'Entry Withdrawn'
_HGNC_ID = re.compile(r'HGNC:[1-9][0-9]*')
_NOT_IN_A_SYMBOL = re.compile(r'[\s,"]')


@dataclasses.dataclass(frozen=True)
class ApprovedGene:
    """What the set records for one approved id.

    Attributes:
        symbol: The symbol HGNC approves for the id.
        previous_symbols: The symbols HGNC approved for the gene earlier that name no other gene in
            the set, in the set's order.
    """

    symbol: str
    previous_symbols: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class HgncResult:
    """The gene HGNC names under one id.

    Attributes:
        gene_symbol: The symbol HGNC approves for the id.
        previous_symbols: The symbols HGNC approved for the gene earlier that name no other gene in
            the set; empty where there is none.
        source: Provenance source label.
        dataset_versions: The release of the complete set the symbols are read from.
        query: The looked-up HGNC id.
    """

    gene_symbol: str
    previous_symbols: tuple[str, ...]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def _columns(header: list[str]) -> tuple[int, int, int, int]:
    """Where the id, the symbol, the status and the previous symbols sit in a row."""
    missing = [col for col in _REQUIRED_COLS if col not in header]
    if missing:
        raise ValueError(f'HGNC complete set is missing columns {missing}')
    return (
        header.index(_HGNC_COL),
        header.index(_SYMBOL_COL),
        header.index(_STATUS_COL),
        header.index(_PREVIOUS_COL),
    )


def _is_symbol(text: str) -> bool:
    """Whether the text is one symbol: not empty, and free of whitespace, commas and quotes."""
    return bool(text) and _NOT_IN_A_SYMBOL.search(text) is None


def _previous_symbols(field: str, hgnc_id: str, line: int) -> tuple[str, ...]:
    """The symbols one ``prev_symbol`` field lists.

    The set writes no previous symbol as an empty field, one as the bare symbol, and several as one
    double-quoted field with ``|`` between them.

    Raises:
        ValueError: If the field is in none of those three forms.
    """
    if not field:
        return ()
    quoted = len(field) > 1 and field.startswith('"') and field.endswith('"')
    symbols = tuple((field[1:-1] if quoted else field).split('|'))
    if not all(_is_symbol(symbol) for symbol in symbols) or (len(symbols) > 1) != quoted:
        raise ValueError(
            f'HGNC complete set line {line} lists the previous symbols of {hgnc_id} as {field!r}: neither one symbol '
            'nor a quoted list with | between its symbols'
        )
    return symbols


def _approved_genes(
    symbols: Mapping[str, str], listed_previous: Mapping[str, tuple[str, ...]]
) -> dict[str, ApprovedGene]:
    """Each approved id's gene, with only the previous symbols that name no other gene in the set.

    A previous symbol is dropped where the set approves it for a gene today, which is a symbol HGNC
    moved to another gene or, rarely, the gene's own approved symbol. It is dropped from every gene
    that lists it where two or more do.

    Args:
        symbols: The approved symbol of each approved id.
        listed_previous: The previous symbols the set lists for each of those ids.
    """
    approved_today = set(symbols.values())
    # A set per id: a symbol one row lists twice is still one gene's.
    listing_ids = collections.Counter(previous for listed in listed_previous.values() for previous in set(listed))
    return {
        hgnc_id: ApprovedGene(
            symbol=symbol,
            previous_symbols=tuple(
                previous
                for previous in listed_previous[hgnc_id]
                if previous not in approved_today and listing_ids[previous] == 1
            ),
        )
        for hgnc_id, symbol in symbols.items()
    }


class HgncCompleteSet:
    """The HGNC complete set: HGNC id -> the symbol HGNC approves for it, and the gene's previous ones.

    Parsed once via :meth:`from_bytes`; ``lookup`` is a synchronous in-memory query. An id HGNC
    approves no symbol under is a ``None`` result, never a fabricated symbol.

    Attributes:
        version: The release of the set, as the reference bucket names it.
    """

    def __init__(self, approved: Mapping[str, ApprovedGene], version: str) -> None:
        self._approved = approved
        self.version = version

    @classmethod
    def from_bytes(cls, data: bytes, *, version: str) -> HgncCompleteSet:
        """Parse and index the complete set (raw TSV bytes from the reference bucket).

        Args:
            data: The set's bytes.
            version: The release the bytes are. The file states none, so its reader does.

        Raises:
            ValueError: If the set starts with a byte-order mark, is empty or lacks a required
                column; if a row has more or fewer fields than the header, carries an id that is
                not an HGNC id or that an earlier row carries, or has a status that is neither
                approved nor withdrawn; if an approved row has a symbol that is not one symbol or a
                malformed list of previous symbols; or if no row is approved.
        """
        if data.startswith(codecs.BOM_UTF8):
            raise ValueError(
                'HGNC complete set starts with a UTF-8 byte-order mark; the set is read as HGNC publishes it, '
                'which is without one'
            )
        # QUOTE_NONE: every line is one row, so a stray quote cannot swallow the rows after it.
        reader = csv.reader(io.StringIO(data.decode('utf-8')), delimiter='\t', quoting=csv.QUOTE_NONE)
        header = next(reader, None)
        if header is None:
            raise ValueError('HGNC complete set is empty')
        id_at, symbol_at, status_at, previous_at = _columns(header)
        symbols: dict[str, str] = {}
        listed_previous: dict[str, tuple[str, ...]] = {}
        seen: set[str] = set()
        for row in reader:
            if not row:
                continue
            # A stray or a missing tab moves every later field one column over, the status among them.
            if len(row) != len(header):
                raise ValueError(
                    f'HGNC complete set line {reader.line_num} has {len(row)} fields where the header has {len(header)}'
                )
            hgnc_id = row[id_at].upper()
            if _HGNC_ID.fullmatch(hgnc_id) is None:
                raise ValueError(f'HGNC complete set line {reader.line_num} has id {row[id_at]!r}, not an HGNC id')
            if hgnc_id in seen:
                raise ValueError(f'HGNC complete set holds {hgnc_id} twice')
            seen.add(hgnc_id)
            if row[status_at] == _WITHDRAWN:
                continue
            if row[status_at] != _APPROVED:
                raise ValueError(
                    f'HGNC complete set line {reader.line_num} has status {row[status_at]!r}, which is neither '
                    f'{_APPROVED!r} nor {_WITHDRAWN!r}'
                )
            if not _is_symbol(row[symbol_at]):
                raise ValueError(
                    f'HGNC complete set line {reader.line_num} approves {row[symbol_at]!r} for {hgnc_id}, which is '
                    'not one symbol'
                )
            symbols[hgnc_id] = row[symbol_at]
            listed_previous[hgnc_id] = _previous_symbols(row[previous_at], hgnc_id, reader.line_num)
        if not symbols:
            raise ValueError(f'HGNC complete set has no row with status {_APPROVED!r}')
        return cls(_approved_genes(symbols, listed_previous), version)

    def lookup(self, hgnc_id: str) -> HgncResult | None:
        """Return the gene HGNC names under the id, or ``None`` if it approves no symbol under it.

        Args:
            hgnc_id: HGNC id (``HGNC:nnnn``, case-insensitive).
        """
        gene = self._approved.get(hgnc_id.upper())
        if gene is None:
            return None
        return HgncResult(
            gene_symbol=gene.symbol,
            previous_symbols=gene.previous_symbols,
            source=_SOURCE,
            dataset_versions=(self.version,),
            query=hgnc_id,
        )
