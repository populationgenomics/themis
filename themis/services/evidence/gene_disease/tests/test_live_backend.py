"""LiveBackend composition: the entity assembly and the entity resolution `DescribeGene` runs.

The reference tables are seeded in memory and the MONDO client function is replaced with a canned
Result, so no test here touches the network — which several of them assert outright, since an
unresolved request is meant to stay a pure table lookup.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import pathlib
import re
from collections.abc import Awaitable, Callable, Mapping

import httpx2
import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import storage

from themis.rpc import gene_disease_pb2
from themis.services.evidence import errors
from themis.services.evidence.gene_disease import backend as gene_disease_backend
from themis.services.evidence.upstreams import clingen_dosage, clingen_validity, gencc, hgnc, mondo, panelapp
from tools.resources import manifest

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_HGNC_VERSION = '2026-01-01'
# The ids the tests query. The default HGNC set approves each, as the deployed set does for a curated gene.
_APPROVED = {'HGNC:1100': 'BRCA1', 'HGNC:2': 'GENE2', 'HGNC:3': 'GENE3', 'HGNC:9': 'GENE9'}
# A set holding none of those ids: the state of a gene the tables carry before the pinned set does.
_ANOTHER_GENE = {'HGNC:5': 'A1BG'}
# A curated term and a MONDO parent of it, for the entity resolution the rpc runs on request.
_CURATED_TERM = 'MONDO:0000101'
_PARENT_TERM = 'MONDO:0000100'


def _returns[T](value: T) -> Callable[..., Awaitable[T]]:
    """An async stand-in for an upstream client function that ignores its args and returns `value`."""

    async def fake(*_args: object, **_kwargs: object) -> T:
        return value

    return fake


def _tables(
    *,
    validity: clingen_validity.ClinGenValidity | None = None,
    dosage: clingen_dosage.ClinGenDosage | None = None,
    gencc_table: gencc.GenCC | None = None,
    panelapp_table: panelapp.PanelAppTable | None = None,
    hgnc_set: hgnc.HgncCompleteSet | None = None,
) -> gene_disease_backend.ReferenceTables:
    """The reference tables, each empty unless a seeded one is supplied, under an HGNC set.

    The default set holds every id the tests query, so a test that passes none takes the path a
    deployed request takes: the symbol is HGNC's and the response carries its provenance.
    """
    return gene_disease_backend.ReferenceTables(
        validity=validity or clingen_validity.ClinGenValidity({}, ()),
        dosage=dosage or clingen_dosage.ClinGenDosage({}, ()),
        gencc=gencc_table or gencc.GenCC({}, ()),
        panelapp=panelapp_table or panelapp.PanelAppTable({}, '', ()),
        hgnc=hgnc_set if hgnc_set is not None else _hgnc_set(_APPROVED),
    )


def _hgnc_set(approved: Mapping[str, str]) -> hgnc.HgncCompleteSet:
    """An HGNC set approving `approved`'s symbol under each of its ids, none of them renamed."""
    return hgnc.HgncCompleteSet(
        {hgnc_id: hgnc.ApprovedGene(symbol, ()) for hgnc_id, symbol in approved.items()}, _HGNC_VERSION
    )


def _run[T](
    tables: gene_disease_backend.ReferenceTables,
    call: Callable[[gene_disease_backend.LiveBackend], Awaitable[T]],
) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient() as client:
            return await call(gene_disease_backend.LiveBackend(client, tables))

    return asyncio.run(run())


def _validity_table(
    hgnc_id: str, classification: str, moi: str, *, mondo_id: str = _CURATED_TERM, gene_symbol: str = 'GENE'
) -> clingen_validity.ClinGenValidity:
    row = {
        'GENE SYMBOL': gene_symbol,
        'GENE ID (HGNC)': hgnc_id,
        'DISEASE LABEL': 'condition',
        'DISEASE ID (MONDO)': mondo_id,
        'MOI': moi,
        'CLASSIFICATION': classification,
    }
    return clingen_validity.ClinGenValidity({hgnc_id.upper(): [row]}, ('2026-01-01',))


def _gencc_table(
    hgnc_id: str,
    classification: str,
    *,
    notes: str = '',
    mondo_id: str = _CURATED_TERM,
    moi_curie: str = 'HP:0000006',
    moi_title: str = 'Autosomal dominant',
    gene_symbol: str = 'GENE',
) -> gencc.GenCC:
    row = {
        'gene_curie': hgnc_id,
        'gene_symbol': gene_symbol,
        'disease_title': 'condition',
        'disease_curie': mondo_id,
        'classification_title': classification,
        'moi_curie': moi_curie,
        'moi_title': moi_title,
        'submitter_title': 'ClinGen',
        'submitted_as_notes': notes,
        'submitted_run_date': '2026-01-01',
    }
    return gencc.GenCC({hgnc_id.upper(): [row]}, ('2026-01-01',))


def _dosage_table(hgnc_id: str, label: str, *, gene_symbol: str = 'GENE') -> clingen_dosage.ClinGenDosage:
    row = {'GENE SYMBOL': gene_symbol, 'HGNC ID': hgnc_id, 'HAPLOINSUFFICIENCY': label}
    return clingen_dosage.ClinGenDosage({hgnc_id.upper(): row}, ('2026-01-01',))


def _panelapp_table(
    hgnc_id: str,
    *,
    max_confidence: int,
    moi: str = '',
    mode_of_pathogenicity: str = '',
    evaluations: tuple[str, ...] = (),
    entries: list[dict[str, object]] | None = None,
    gene_symbol: str = 'GENE',
) -> panelapp.PanelAppTable:
    entry = {
        'gene_symbol': gene_symbol,
        'max_confidence': max_confidence,
        'mode_of_inheritance': moi,
        'mode_of_pathogenicity': mode_of_pathogenicity,
        'evaluations': list(evaluations),
        'entries': entries if entries is not None else [{'confidence_level': str(max_confidence), 'publications': []}],
    }
    return panelapp.PanelAppTable({hgnc_id.upper(): entry}, 'Mendeliome, Incidentalome', ('2026-01-01',))


def _reference_blobs() -> dict[str, bytes]:
    """The reference objects read from the upstream fixtures, keyed by their bucket object name."""
    return {
        gene_disease_backend._GENCC_OBJECT: (_FIXTURES / 'gencc.tsv').read_bytes(),
        gene_disease_backend._VALIDITY_OBJECT: (_FIXTURES / 'clingen_validity.csv').read_bytes(),
        gene_disease_backend._DOSAGE_OBJECT: (_FIXTURES / 'clingen_dosage.csv').read_bytes(),
        gene_disease_backend._PANELAPP_OBJECT: (_FIXTURES / 'panelapp.json').read_bytes(),
        gene_disease_backend._HGNC_OBJECT: (_FIXTURES / 'hgnc_complete_set.tsv').read_bytes(),
    }


class _Blob:
    """A blob handle whose download raises for an object the bucket does not hold, as in GCS."""

    def __init__(self, name: str, held: Mapping[str, bytes]) -> None:
        self._name = name
        self._held = held

    def download_as_bytes(self) -> bytes:
        if self._name not in self._held:
            raise api_exceptions.NotFound(f'no such object: {self._name}')
        return self._held[self._name]


class _Bucket:
    def __init__(self, held: Mapping[str, bytes]) -> None:
        self._held = held

    def blob(self, name: str) -> _Blob:
        return _Blob(name, self._held)


class _Client:
    """A ``storage.Client`` stand-in over one bucket's objects."""

    def __init__(self, held: Mapping[str, bytes]) -> None:
        self._held = held

    def bucket(self, _name: str) -> _Bucket:
        return _Bucket(self._held)

    def close(self) -> None:
        pass


def test_gene_disease_returns_every_curated_entity_unreduced() -> None:
    resp = _run(
        _tables(
            validity=_validity_table('HGNC:1100', 'Moderate', 'AD'),
            gencc_table=_gencc_table('HGNC:1100', 'Strong'),
            dosage=_dosage_table('HGNC:1100', 'Sufficient Evidence for Haploinsufficiency'),
            panelapp_table=_panelapp_table('HGNC:1100', max_confidence=3, moi='BIALLELIC'),
        ),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100')),
    )
    # Each source's own assertion about the entity survives; nothing is maxed across the two.
    assert [(e.source, e.validity_classification, e.gate_level) for e in resp.entities] == [
        ('ClinGen Gene Validity', 'Moderate', gene_disease_pb2.GATE_LEVEL_MODERATE),
        ('GenCC', 'Strong', gene_disease_pb2.GATE_LEVEL_STRONG),
    ]
    assert resp.coverage == gene_disease_pb2.GENE_COVERAGE_CURATED
    assert not resp.HasField('resolution')
    assert resp.gene_scoped.haploinsufficiency_score == 3
    assert resp.gene_scoped.mode_of_inheritance == 'BIALLELIC'  # PanelApp's, and marked as the gene's
    assert resp.raw['panelapp_max_confidence'] == 3
    assert resp.gene_symbol == 'BRCA1'  # HGNC's, not the tables' spelling
    assert [p.source for p in resp.provenance] == [
        'HGNC',
        'ClinGen Gene Validity',
        'GenCC',
        'ClinGen Dosage Sensitivity',
        'PanelApp Australia',
    ]


def test_gene_disease_gencc_only_tier_keeps_its_own_vocabulary_and_gate_level() -> None:
    resp = _run(
        _tables(gencc_table=_gencc_table('HGNC:2', 'Supportive')),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:2')),
    )
    # `Supportive` is not a gate level; the entity carries the level so no caller maps it by hand.
    assert [(e.validity_classification, e.gate_level) for e in resp.entities] == [
        ('Supportive', gene_disease_pb2.GATE_LEVEL_LIMITED)
    ]


def test_gene_disease_tells_an_absent_gene_from_one_with_no_validity_assertion() -> None:
    absent = _run(
        _tables(hgnc_set=_hgnc_set({'HGNC:404040': 'GENE'})),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:404040')),
    )
    assert absent.coverage == gene_disease_pb2.GENE_COVERAGE_ABSENT
    assert not absent.entities
    assert not absent.gene_scoped.sources_holding_the_gene
    assert not absent.gene_scoped.HasField('haploinsufficiency_score')
    assert absent.raw.fields['clingen_dosage'].WhichOneof('kind') == 'null_value'
    assert [p.source for p in absent.provenance] == ['HGNC']  # no table answered for the gene

    held = _run(
        _tables(panelapp_table=_panelapp_table('HGNC:9', max_confidence=3, moi='MONOALLELIC')),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:9')),
    )
    assert held.coverage == gene_disease_pb2.GENE_COVERAGE_NO_VALIDITY_ASSERTION
    assert not held.entities
    assert held.gene_scoped.sources_holding_the_gene == ['PanelApp Australia']


@pytest.mark.parametrize(
    'tables',
    [
        _tables(
            validity=_validity_table('HGNC:1100', 'Definitive', 'AD', gene_symbol='TABLE1'),
            hgnc_set=_hgnc_set(_ANOTHER_GENE),
        ),
        _tables(
            gencc_table=_gencc_table('HGNC:1100', 'Definitive', gene_symbol='TABLE1'),
            hgnc_set=_hgnc_set(_ANOTHER_GENE),
        ),
        _tables(
            dosage=_dosage_table('HGNC:1100', 'No Evidence for Haploinsufficiency', gene_symbol='TABLE1'),
            hgnc_set=_hgnc_set(_ANOTHER_GENE),
        ),
        _tables(
            panelapp_table=_panelapp_table('HGNC:1100', max_confidence=3, gene_symbol='TABLE1'),
            hgnc_set=_hgnc_set(_ANOTHER_GENE),
        ),
    ],
    ids=['clingen_validity', 'gencc', 'clingen_dosage', 'panelapp'],
)
def test_gene_disease_names_a_gene_the_hgnc_set_lacks_from_whichever_table_carries_it(
    tables: gene_disease_backend.ReferenceTables,
) -> None:
    # The tables are refreshed more often than the set is pinned, so a table can carry a gene the set
    # does not hold yet. One table alone is enough to name it.
    resp = _run(tables, lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100')))
    assert resp.gene_symbol == 'TABLE1'
    assert not resp.previous_symbols  # only the set states them
    assert 'HGNC' not in [p.source for p in resp.provenance]


def test_gene_disease_names_an_uncurated_gene_from_the_hgnc_set() -> None:
    # A gene no table curates is still a gene: the answer is its coverage, under its approved symbol.
    resp = _run(
        _tables(
            validity=_validity_table('HGNC:1100', 'Definitive', 'AD'),
            hgnc_set=_hgnc_set({'HGNC:404040': 'UNCURATED1'}),
        ),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:404040')),
    )
    assert resp.coverage == gene_disease_pb2.GENE_COVERAGE_ABSENT
    assert resp.gene_symbol == 'UNCURATED1'
    assert [(p.source, list(p.dataset_versions)) for p in resp.provenance] == [('HGNC', [_HGNC_VERSION])]


def test_gene_disease_answers_a_renamed_gene_under_its_approved_symbol_with_its_previous_ones() -> None:
    # A curation table can lag an HGNC rename, and so can the source of a caller's symbol: the
    # previous symbols are what tells either that the records are this gene's.
    renamed = hgnc.HgncCompleteSet({'HGNC:1100': hgnc.ApprovedGene('CURRENT1', ('FORMER1', 'FORMER0'))}, _HGNC_VERSION)
    resp = _run(
        _tables(validity=_validity_table('HGNC:1100', 'Definitive', 'AD', gene_symbol='FORMER1'), hgnc_set=renamed),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100')),
    )
    assert resp.gene_symbol == 'CURRENT1'
    assert resp.previous_symbols == ['FORMER1', 'FORMER0']
    assert resp.coverage == gene_disease_pb2.GENE_COVERAGE_CURATED
    assert {p.source for p in resp.provenance} == {'HGNC', 'ClinGen Gene Validity'}
    # The table's own spelling stays readable.
    rows = resp.raw.fields['clingen_validity'].struct_value.fields['rows'].list_value.values
    assert rows[0].struct_value.fields['GENE SYMBOL'].string_value == 'FORMER1'


def test_gene_disease_fails_an_id_neither_the_hgnc_set_nor_a_table_holds() -> None:
    # Answered as an uncurated gene, a mistyped id would read as a finding about a gene.
    with pytest.raises(errors.UnknownVariantError, match='no gene under HGNC:404040'):
        _run(
            _tables(
                validity=_validity_table('HGNC:1100', 'Definitive', 'AD'),
                hgnc_set=_hgnc_set({'HGNC:1100': 'BRCA1'}),
            ),
            lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:404040')),
        )


def test_gene_disease_reads_an_id_hgnc_holds_unapproved_as_one_the_set_lacks() -> None:
    unapproved = hgnc.HgncCompleteSet.from_bytes(
        b'hgnc_id\tsymbol\tstatus\tprev_symbol\nHGNC:1100\tBRCA1\tApproved\t\nHGNC:2\tGONE\tEntry Withdrawn\t\n',
        version=_HGNC_VERSION,
    )
    with pytest.raises(errors.UnknownVariantError, match='no gene under HGNC:2'):
        _run(
            _tables(hgnc_set=unapproved),
            lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:2')),
        )
    # A table that still carries the id answers with its records, under the symbol it records.
    held = _run(
        _tables(dosage=_dosage_table('HGNC:2', 'No Evidence for Haploinsufficiency'), hgnc_set=unapproved),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:2')),
    )
    assert held.gene_symbol == 'GENE'
    assert [p.source for p in held.provenance] == ['ClinGen Dosage Sensitivity']


def test_gene_disease_takes_the_first_table_symbol_where_two_spell_it_differently() -> None:
    resp = _run(
        _tables(
            validity=_validity_table('HGNC:1100', 'Definitive', 'AD', gene_symbol='CURRENT1'),
            panelapp_table=_panelapp_table('HGNC:1100', max_confidence=3, gene_symbol='FORMER1'),
            hgnc_set=_hgnc_set(_ANOTHER_GENE),
        ),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100')),
    )
    assert resp.gene_symbol == 'CURRENT1'
    # The other table's spelling stays readable.
    assert resp.raw.fields['panelapp'].struct_value.fields['gene_symbol'].string_value == 'FORMER1'


def test_gene_disease_dosage_zero_is_present_not_absent() -> None:
    resp = _run(
        _tables(dosage=_dosage_table('HGNC:3', 'No Evidence for Haploinsufficiency')),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:3')),
    )
    assert resp.gene_scoped.HasField('haploinsufficiency_score')
    assert resp.gene_scoped.haploinsufficiency_score == 0


def test_gene_disease_panelapp_green_is_not_a_curated_entity() -> None:
    resp = _run(
        _tables(
            panelapp_table=_panelapp_table(
                'HGNC:9', max_confidence=3, moi='MONOALLELIC', mode_of_pathogenicity='gain-of-function'
            )
        ),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:9')),
    )
    assert not resp.entities
    assert resp.raw['panelapp_max_confidence'] == 3
    assert resp.gene_scoped.mode_of_inheritance == 'MONOALLELIC'
    assert resp.gene_scoped.mode_of_pathogenicity == 'gain-of-function'
    assert [p.source for p in resp.provenance] == ['HGNC', 'PanelApp Australia']


def test_gene_disease_scopes_mechanism_statements_to_what_they_are_about() -> None:
    resp = _run(
        _tables(
            gencc_table=_gencc_table('HGNC:1100', 'Definitive', notes='loss-of-function mechanism'),
            panelapp_table=_panelapp_table(
                'HGNC:1100',
                max_confidence=3,
                evaluations=('green: LoF', 'expert review'),
                entries=[{'confidence_level': '3', 'publications': ['111', '222']}],
            ),
        ),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100')),
    )
    # GenCC's narrative is about one entity and rides on it; PanelApp's is the panel's, so the gene's.
    entity_statements = [s for entity in resp.entities for s in entity.mechanism_statements]
    assert [(s.source, s.context, s.text) for s in entity_statements] == [
        ('GenCC', 'condition', 'loss-of-function mechanism')
    ]
    panel_statements = resp.gene_scoped.mechanism_statements
    assert [s.text for s in panel_statements] == ['green: LoF', 'expert review']
    assert all(s.source == 'PanelApp Australia' for s in panel_statements)
    assert all(s.context == 'Mendeliome, Incidentalome' for s in panel_statements)
    # raw['panelapp'] passes the full per-gene entry through, so the agent still sees the publications.
    panelapp_raw = resp.raw.fields['panelapp'].struct_value
    assert 'entries' in panelapp_raw.fields
    first_entry = panelapp_raw.fields['entries'].list_value.values[0].struct_value
    publications = [value.string_value for value in first_entry.fields['publications'].list_value.values]
    assert publications == ['111', '222']


def test_gene_disease_resolving_the_curated_term_reads_no_ontology() -> None:
    def _no_network(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError('a request naming a curated term resolves in memory and must not reach the ontology')

    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(_no_network)) as client:
            backend = gene_disease_backend.LiveBackend(
                client, _tables(validity=_validity_table('HGNC:1100', 'Limited', 'AD'))
            )
            return await backend.describe_gene(
                gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id=_CURATED_TERM)
            )

    resp = asyncio.run(run())
    assert resp.resolution.relation == gene_disease_pb2.TERM_RELATION_SAME
    assert [e.gate_level for e in resp.resolution.entities] == [gene_disease_pb2.GATE_LEVEL_LIMITED]
    assert [p.source for p in resp.provenance] == ['HGNC', 'ClinGen Gene Validity']  # no MONDO round-trip
    assert 'mondo_ancestors' not in resp.raw


def test_gene_disease_answers_a_gene_carrying_a_mode_outside_the_framework() -> None:
    # GenCC files a digenic assertion (HP:0010984) against CLCNKB, a mode outside the framework's five.
    # The record stands as the source states it, and the gene's other records and the resolution of
    # its term are answered whole.
    resp = _run(
        _tables(
            validity=_validity_table('HGNC:1100', 'Definitive', 'AR'),
            gencc_table=_gencc_table(
                'HGNC:1100', 'Supportive', moi_curie='HP:0010984', moi_title='Digenic inheritance'
            ),
        ),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id=_CURATED_TERM)),
    )
    assert [(e.source, e.inheritance_term) for e in resp.resolution.entities] == [
        ('ClinGen Gene Validity', 'AR'),
        ('GenCC', 'Digenic inheritance'),
    ]


def test_gene_disease_resolving_a_parent_term_reads_the_subclass_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mondo,
        'fetch_subclass_closure',
        _returns(
            mondo.MondoClosureResult(
                ancestors={_CURATED_TERM: (_PARENT_TERM,)},
                raw={_CURATED_TERM: [_PARENT_TERM]},
                source='EBI OLS4 (MONDO)',
                dataset_versions=('MONDO 2026-07-01',),
                query=f'ancestors of [{_CURATED_TERM!r}]',
            )
        ),
    )
    resp = _run(
        _tables(validity=_validity_table('HGNC:1100', 'Definitive', 'AD')),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id=_PARENT_TERM)),
    )
    assert resp.resolution.relation == gene_disease_pb2.TERM_RELATION_DESCENDANT
    assert resp.resolution.mondo_id == _CURATED_TERM
    assert resp.resolution.requested_mondo_id == _PARENT_TERM
    assert 'EBI OLS4 (MONDO)' in [p.source for p in resp.provenance]
    assert resp.raw['mondo_ancestors'] == {_CURATED_TERM: [_PARENT_TERM]}


def test_gene_disease_answers_an_uncurated_gene_with_its_coverage_not_a_refusal() -> None:
    # Naming an entity must not cost the caller the gene-scoped signals: there is nothing to resolve
    # against and nothing to be wrong about, and `coverage` is the answer to which absence this is.
    resp = _run(
        _tables(dosage=_dosage_table('HGNC:3', 'Sufficient Evidence for Haploinsufficiency')),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:3', mondo_id=_PARENT_TERM)),
    )
    assert resp.coverage == gene_disease_pb2.GENE_COVERAGE_NO_VALIDITY_ASSERTION
    assert not resp.HasField('resolution')
    assert resp.gene_scoped.haploinsufficiency_score == 3


def test_gene_disease_refuses_an_entity_the_gene_is_not_curated_for(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mondo,
        'fetch_subclass_closure',
        _returns(
            mondo.MondoClosureResult(
                ancestors={_CURATED_TERM: ('MONDO:0000001',)},
                raw={},
                source='EBI OLS4 (MONDO)',
                dataset_versions=('MONDO 2026-07-01',),
                query='q',
            )
        ),
    )
    with pytest.raises(errors.UnresolvedEntityError, match='not the nearest level'):
        _run(
            _tables(validity=_validity_table('HGNC:1100', 'Definitive', 'AD')),
            lambda be: be.describe_gene(
                gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id=_PARENT_TERM)
            ),
        )


def test_gene_disease_from_reference_dumps_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gene_disease_backend, '_download_reference_blobs', lambda _bucket: _reference_blobs())

    def _no_network(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError('an unresolved describe_gene is a pure table lookup and must not touch the network')

    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(_no_network)) as client:
            backend = await gene_disease_backend.LiveBackend.create(
                http_client=client, resources_bucket='resources-bucket'
            )
            return await backend.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'))

    resp = asyncio.run(run())
    assert resp.gene_symbol == 'BRCA1'
    # The symbol is the HGNC set's, stamped with the version the backend pins.
    hgnc_stamps = [p for p in resp.provenance if p.source == 'HGNC']
    assert [list(p.dataset_versions) for p in hgnc_stamps] == [[gene_disease_backend._HGNC_VERSION]]
    # Both sources' curations survive the join, each entity gate-levelled: the dumps' own vocabulary
    # is what the assembly is held to, so a table whose classifications move fails here.
    assert {e.source for e in resp.entities} == {'ClinGen Gene Validity', 'GenCC'}
    assert all(e.mondo_id.startswith('MONDO:') for e in resp.entities)
    assert gene_disease_pb2.GATE_LEVEL_UNSPECIFIED not in {e.gate_level for e in resp.entities}
    assert resp.gene_scoped.mechanism_statements


def test_create_fetches_the_resources_bucket_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {'downloads': 0}

    def fake_download(bucket: str) -> dict[str, bytes]:
        calls['downloads'] += 1
        assert bucket == 'resources-bucket'
        return _reference_blobs()

    monkeypatch.setattr(gene_disease_backend, '_download_reference_blobs', fake_download)

    async def build() -> gene_disease_backend.LiveBackend:
        async with httpx2.AsyncClient() as client:
            return await gene_disease_backend.LiveBackend.create(
                http_client=client, resources_bucket='resources-bucket'
            )

    backend = asyncio.run(build())
    assert isinstance(backend, gene_disease_backend.LiveBackend)
    assert calls == {'downloads': 1}


@pytest.mark.parametrize('missing', gene_disease_backend._REFERENCE_OBJECTS)
def test_create_fails_when_a_reference_object_is_missing(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    # Started without one, the backend would answer for every gene from a table it never read.
    held = {name: data for name, data in _reference_blobs().items() if name != missing}
    monkeypatch.setattr(storage, 'Client', lambda: _Client(held))

    async def build() -> gene_disease_backend.LiveBackend:
        async with httpx2.AsyncClient() as client:
            return await gene_disease_backend.LiveBackend.create(
                http_client=client, resources_bucket='resources-bucket'
            )

    with pytest.raises(RuntimeError, match=re.escape(missing)):
        asyncio.run(build())


def test_the_hgnc_object_is_where_the_mirror_writes_the_complete_set() -> None:
    # The mirror composes the name from the manifest's entry and the backend spells it out, so a
    # change to the entry's source, role or suffix has to reach the backend too.
    pinned = manifest.load()
    read = dataclasses.replace(pinned.table('hgnc-complete-set'), version=gene_disease_backend._HGNC_VERSION)
    assert f'{pinned.dataset}/{read.object}' == gene_disease_backend._HGNC_OBJECT


def test_the_hgnc_release_is_not_ahead_of_the_one_the_manifest_pins() -> None:
    # No bucket holds a release the manifest has not pinned, so a backend that reads one fails its
    # start in every environment. It may be behind: the backend moves in a later merge than the
    # manifest, once the mirror has written the manifest's release.
    pinned = datetime.date.fromisoformat(manifest.load().table('hgnc-complete-set').version)
    assert datetime.date.fromisoformat(gene_disease_backend._HGNC_VERSION) <= pinned
