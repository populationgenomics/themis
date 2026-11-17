"""HGNC complete set: TSV parsing, HGNC-id keying, which rows name a gene, and its previous symbols.

The recorded fixture is the complete set's header with three of its rows, unchanged: two of its first
rows and the row of a gene with two previous symbols. One further row stands for the gene the
gene-disease fixtures are built around, carrying its leading columns and its last one only. Parsing
is exercised from bytes; no network.
"""

from __future__ import annotations

import codecs
import pathlib
from collections.abc import Sequence

import pytest

from themis.services.evidence.upstreams import hgnc

_FIXTURE = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'hgnc_complete_set.tsv'
_VERSION = '2026-07-07'

# The columns the parser reads, in the set's own order; the real set carries 54.
_REQUIRED = ['hgnc_id', 'symbol', 'status', 'prev_symbol']


def _tsv(header: Sequence[str], *rows: Sequence[str]) -> bytes:
    return ''.join('\t'.join(line) + '\n' for line in (header, *rows)).encode()


def _set(*rows: Sequence[str]) -> hgnc.HgncCompleteSet:
    return hgnc.HgncCompleteSet.from_bytes(_tsv(_REQUIRED, *rows), version=_VERSION)


def _recorded() -> hgnc.HgncCompleteSet:
    return hgnc.HgncCompleteSet.from_bytes(_FIXTURE.read_bytes(), version=_VERSION)


def _previous_symbols(table: hgnc.HgncCompleteSet, hgnc_id: str) -> tuple[str, ...]:
    result = table.lookup(hgnc_id)
    assert result is not None
    return result.previous_symbols


def test_lookup_carries_the_approved_symbol_and_the_source_stamp() -> None:
    result = _recorded().lookup('HGNC:1100')
    assert result is not None
    assert result.gene_symbol == 'BRCA1'
    assert result.source == 'HGNC'
    assert result.dataset_versions == (_VERSION,)  # the file states no release; its reader does
    assert result.query == 'HGNC:1100'


def test_a_row_with_quoted_multi_valued_columns_keeps_its_symbol() -> None:
    # A2M's alias symbols and accessions are quoted, pipe-separated lists in the recorded set.
    result = _recorded().lookup('HGNC:7')
    assert result is not None
    assert result.gene_symbol == 'A2M'


@pytest.mark.parametrize(
    ('hgnc_id', 'expected'),
    [('HGNC:7', ()), ('HGNC:23336', ('CPAMD9',)), ('HGNC:17272', ('CENPJ', 'MCPH6'))],
    ids=['never_renamed', 'one_bare_symbol', 'a_quoted_list'],
)
def test_lookup_carries_the_previous_symbols_in_each_form_the_set_writes(
    hgnc_id: str, expected: tuple[str, ...]
) -> None:
    # Exactly the previous symbols: the rows of HGNC:7 and HGNC:17272 list alias symbols too.
    assert _previous_symbols(_recorded(), hgnc_id) == expected


def test_absent_id_is_none() -> None:
    assert _recorded().lookup('HGNC:404040') is None


def test_case_insensitive() -> None:
    assert _recorded().lookup('hgnc:1100') is not None


def test_a_withdrawn_id_names_no_gene() -> None:
    table = _set(['HGNC:1', 'KEPT', 'Approved', ''], ['HGNC:2', 'GONE', 'Entry Withdrawn', ''])
    assert table.lookup('HGNC:1') is not None
    assert table.lookup('HGNC:2') is None


@pytest.mark.parametrize('status', ['Approved ', 'approved', 'Merged/Split', ''])
def test_a_status_the_reader_does_not_know_raises_value_error(status: str) -> None:
    # Skipped as unapproved, the row's gene would answer as an id that names no gene.
    with pytest.raises(ValueError, match='line 3 has status'):
        _set(['HGNC:1', 'ONE', 'Approved', ''], ['HGNC:2', 'TWO', status, ''])


@pytest.mark.parametrize('moved_first', [True, False])
def test_a_previous_symbol_another_gene_carries_today_is_left_out(moved_first: bool) -> None:
    renamed = ['HGNC:1', 'NEW1', 'Approved', '"MOVED1|OLD1"']
    carrier = ['HGNC:2', 'MOVED1', 'Approved', '']
    table = _set(carrier, renamed) if moved_first else _set(renamed, carrier)
    assert _previous_symbols(table, 'HGNC:1') == ('OLD1',)


def test_a_gene_whose_only_previous_symbol_moved_lists_none() -> None:
    table = _set(['HGNC:1', 'NEW1', 'Approved', 'MOVED1'], ['HGNC:2', 'MOVED1', 'Approved', ''])
    assert _previous_symbols(table, 'HGNC:1') == ()


def test_a_previous_symbol_equal_to_the_approved_one_is_left_out() -> None:
    table = _set(['HGNC:1', 'SAME1', 'Approved', '"SAME1|OLD1"'])
    assert _previous_symbols(table, 'HGNC:1') == ('OLD1',)


def test_a_previous_symbol_two_genes_list_is_returned_for_neither() -> None:
    table = _set(
        ['HGNC:1', 'NEW1', 'Approved', '"SHARED1|OLD1"'],
        ['HGNC:2', 'NEW2', 'Approved', '"OLD2|SHARED1"'],
        ['HGNC:3', 'NEW3', 'Approved', 'OLD3'],
    )
    # Every id still names its gene, and keeps the previous symbols only it lists.
    assert _previous_symbols(table, 'HGNC:1') == ('OLD1',)
    assert _previous_symbols(table, 'HGNC:2') == ('OLD2',)
    assert _previous_symbols(table, 'HGNC:3') == ('OLD3',)


def test_a_previous_symbol_one_row_lists_twice_is_still_that_gene_alone() -> None:
    table = _set(['HGNC:1', 'NEW1', 'Approved', '"OLD1|OLD1"'], ['HGNC:2', 'NEW2', 'Approved', ''])
    assert 'OLD1' in _previous_symbols(table, 'HGNC:1')


def test_a_withdrawn_row_does_not_claim_its_symbol() -> None:
    # A withdrawn id approves no symbol, so a gene that carried the symbol earlier still lists it.
    table = _set(['HGNC:1', 'NEW1', 'Approved', 'OLD1'], ['HGNC:2', 'OLD1', 'Entry Withdrawn', ''])
    assert _previous_symbols(table, 'HGNC:1') == ('OLD1',)


def test_an_unbalanced_quote_does_not_swallow_the_rows_after_it() -> None:
    table = hgnc.HgncCompleteSet.from_bytes(
        _tsv(
            ['hgnc_id', 'symbol', 'name', 'status', 'prev_symbol'],
            ['HGNC:1', 'ONE', '"an opened quote', 'Approved', ''],
            ['HGNC:2', 'TWO', 'a name', 'Approved', ''],
        ),
        version=_VERSION,
    )
    assert table.lookup('HGNC:1') is not None
    assert table.lookup('HGNC:2') is not None


def test_empty_set_raises_value_error() -> None:
    with pytest.raises(ValueError, match='is empty'):
        hgnc.HgncCompleteSet.from_bytes(b'', version=_VERSION)


def test_a_byte_order_mark_is_named_as_the_fault() -> None:
    # Decoded with the mark, the header's first column reads as an unknown one and hides the cause.
    with pytest.raises(ValueError, match='byte-order mark'):
        hgnc.HgncCompleteSet.from_bytes(codecs.BOM_UTF8 + _FIXTURE.read_bytes(), version=_VERSION)


@pytest.mark.parametrize('dropped', _REQUIRED)
def test_missing_required_column_raises_value_error(dropped: str) -> None:
    header = [col for col in _REQUIRED if col != dropped]
    with pytest.raises(ValueError, match='missing columns'):
        hgnc.HgncCompleteSet.from_bytes(_tsv(header, ['x', 'y', 'z']), version=_VERSION)


def test_a_set_approving_no_gene_raises_value_error() -> None:
    with pytest.raises(ValueError, match="no row with status 'Approved'"):
        _set(['HGNC:1', 'ONE', 'Entry Withdrawn', ''])


@pytest.mark.parametrize(
    ('rows', 'expected'),
    [
        ([['HGNC:1', 'ONE', 'Approved']], 'line 2 has 3 fields where the header has 4'),
        # A stray tab before the status moves an empty field into the status column.
        ([['HGNC:1', 'ONE', '', 'Approved', '']], 'line 2 has 5 fields where the header has 4'),
        ([['1', 'ONE', 'Approved', '']], 'not an HGNC id'),
        ([['HGNC:01', 'ONE', 'Approved', '']], 'not an HGNC id'),
        ([['HGNC:1\N{ARABIC-INDIC DIGIT ONE}', 'ONE', 'Approved', '']], 'not an HGNC id'),
        ([['HGNC:1', 'ONE', 'Approved', ''], ['HGNC:1', 'UNO', 'Approved', '']], 'holds HGNC:1 twice'),
        ([['HGNC:1', '', 'Approved', '']], "line 2 approves '' for HGNC:1"),
        ([['HGNC:1', ' ', 'Approved', '']], "line 2 approves ' ' for HGNC:1"),
        # Kept, a padded symbol would not match the previous symbol it should remove from another gene.
        ([['HGNC:1', 'ONE ', 'Approved', '']], "line 2 approves 'ONE ' for HGNC:1"),
        ([['HGNC:1', 'ONE,UNO', 'Approved', '']], "line 2 approves 'ONE,UNO' for HGNC:1"),
    ],
    ids=[
        'short_row',
        'shifted_row',
        'malformed_id',
        'zero_led_id',
        'non_ascii_digit_id',
        'repeated_id',
        'empty_symbol',
        'blank_symbol',
        'padded_symbol',
        'two_symbols',
    ],
)
def test_a_malformed_row_raises_value_error(rows: list[list[str]], expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        _set(*rows)


@pytest.mark.parametrize(
    'field',
    [
        'OLD1|OLD2',
        '"OLD1"',
        '"OLD1|OLD2',
        '"OLD1||OLD2"',
        '"OLD1| "',
        '"',
        'OLD1, OLD2',
        'OLD1,OLD2',
        ' OLD1',
        '"OLD1| OLD2"',
        'OLD1 OLD2',
    ],
    ids=[
        'unquoted_list',
        'quoted_single',
        'unclosed_quote',
        'empty_element',
        'blank_element',
        'lone_quote',
        'comma_and_space_list',
        'comma_list',
        'padded_symbol',
        'padded_element',
        'space_list',
    ],
)
def test_previous_symbols_in_a_form_the_set_does_not_write_raise_value_error(field: str) -> None:
    # Read loosely, a format the set moved to would list a wrong symbol as one the gene carried.
    with pytest.raises(ValueError, match='line 2 lists the previous symbols of HGNC:1'):
        _set(['HGNC:1', 'ONE', 'Approved', field])
