"""PanelApp Australia dump: HGNC-id keying, confidence/MOI/veto, evaluations, fail-loud shape.

The recorded fixture is a ``panelapp/dump.json`` subset the refresh job produces. Malformed
shapes are exercised with inline JSON. No network — the dump is parsed from bytes.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from themis.services.evidence.upstreams import panelapp

_FIXTURE = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'panelapp.json'


def _table() -> panelapp.PanelAppTable:
    return panelapp.PanelAppTable.from_bytes(_FIXTURE.read_bytes())


def test_lookup_returns_confidence_moi_and_evaluations() -> None:
    result = _table().lookup('HGNC:1100')
    assert result is not None
    assert result.gene_symbol == 'BRCA1'
    assert result.max_confidence == 3
    assert result.mode_of_inheritance == 'BIALLELIC, autosomal or pseudoautosomal'
    assert result.mode_of_pathogenicity == ''  # BRCA1 carries no GoF veto
    assert len(result.evaluations) == 2
    assert all(comment.strip() for comment in result.evaluations)
    assert result.source == 'PanelApp Australia'
    assert result.dataset_versions == ('2026-07-24',)
    assert result.query == 'HGNC:1100'
    # The panel scope names both dumped panels, for MechanismStatement context.
    assert 'Mendeliome' in result.panel_scope
    assert 'Incidentalome' in result.panel_scope


def test_lookup_exposes_full_entries_with_publications() -> None:
    result = _table().lookup('HGNC:1100')
    assert result is not None
    # The full per-panel gene JSON survives verbatim (generous, not hand-picked): each entry keeps
    # the rich fields the agent mines — the publications list and the gene_data block.
    assert result.entries
    assert any(entry.get('publications') for entry in result.entries)
    assert all('gene_data' in entry for entry in result.entries)


def test_mode_of_pathogenicity_veto_is_surfaced() -> None:
    result = _table().lookup('HGNC:1097')
    assert result is not None
    assert result.mode_of_pathogenicity == 'gain-of-function'
    assert result.evaluations == []  # a veto without evaluation comments


def test_gene_in_no_panel_is_none() -> None:
    assert _table().lookup('HGNC:404040') is None


def test_case_insensitive() -> None:
    assert _table().lookup('hgnc:1100') is not None


@pytest.mark.parametrize('missing', ['dataset_versions', 'panels', 'genes'])
def test_dump_missing_top_level_key_raises_value_error(missing: str) -> None:
    dump = {
        'dataset_versions': ['2026-07-24'],
        'panels': {'137': 'Mendeliome'},
        'genes': {},
    }
    del dump[missing]
    with pytest.raises(ValueError, match=missing.split('_', maxsplit=1)[0]):
        panelapp.PanelAppTable.from_bytes(json.dumps(dump).encode())


def test_malformed_gene_entry_raises_value_error() -> None:
    dump = {
        'dataset_versions': ['2026-07-24'],
        'panels': {'137': 'Mendeliome'},
        'genes': {
            'HGNC:1': {
                'gene_symbol': 'G',
                'max_confidence': 'three',  # not an int
                'mode_of_inheritance': 'AD',
                'mode_of_pathogenicity': '',
                'evaluations': [],
            }
        },
    }
    table = panelapp.PanelAppTable.from_bytes(json.dumps(dump).encode())
    with pytest.raises(ValueError, match='max_confidence'):
        table.lookup('HGNC:1')


def test_gene_entry_missing_field_raises_value_error() -> None:
    dump = {
        'dataset_versions': ['2026-07-24'],
        'panels': {'137': 'Mendeliome'},
        'genes': {'HGNC:1': {'gene_symbol': 'G', 'max_confidence': 3, 'mode_of_inheritance': 'AD'}},
    }
    table = panelapp.PanelAppTable.from_bytes(json.dumps(dump).encode())
    with pytest.raises(ValueError, match='mode_of_pathogenicity'):
        table.lookup('HGNC:1')


def test_malformed_entries_raises_value_error() -> None:
    dump = {
        'dataset_versions': ['2026-07-24'],
        'panels': {'137': 'Mendeliome'},
        'genes': {
            'HGNC:1': {
                'gene_symbol': 'G',
                'max_confidence': 3,
                'mode_of_inheritance': 'AD',
                'mode_of_pathogenicity': '',
                'evaluations': [],
                'entries': 'not-a-list',
            }
        },
    }
    table = panelapp.PanelAppTable.from_bytes(json.dumps(dump).encode())
    with pytest.raises(ValueError, match='entries'):
        table.lookup('HGNC:1')
