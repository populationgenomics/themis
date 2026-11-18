"""Live `GeneDisease.DescribeGene` over in-process gRPC, `fake-gcs-server`, and `httpx2.MockTransport`."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import functools
import pathlib
import re
from collections.abc import Mapping, Sequence

import grpc
import grpc.aio
import httpx2
import pytest
from google.cloud import storage

from themis.common import authored_status
from themis.rpc import gene_disease_pb2, gene_disease_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.gene_disease import backend as gene_disease_backend
from themis.services.evidence.gene_disease import interface
from themis.services.evidence.gene_disease import servicer as servicer_mod
from themis.services.evidence.tests import authz
from themis.services.evidence.upstreams import (
    clingen_dosage,
    clingen_validity,
    destinations,
    gencc,
    hgnc,
    panelapp,
)
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


def _no_network(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f'unexpected network call: {request.url}')


def _ols4_transport(ancestors_by_term: Mapping[str, Sequence[str]]) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == '/ols4/api/ontologies/mondo':
            return httpx2.Response(200, json={'config': {'version': '2026-07-01'}})
        match = re.search(r'MONDO_\d+', str(request.url))
        assert match is not None, f'unexpected OLS4 request: {request.url}'
        term = match.group(0).replace('_', ':')
        assert term in ancestors_by_term, f'unexpected curated term queried: {term}'
        terms = [{'obo_id': ancestor, 'label': f'label of {ancestor}'} for ancestor in ancestors_by_term[term]]
        return httpx2.Response(
            200,
            json={
                '_embedded': {'terms': terms},
                'page': {'totalPages': 1, 'totalElements': len(terms)},
            },
        )

    return httpx2.MockTransport(handler)


def _tables(
    *,
    validity: clingen_validity.ClinGenValidity | None = None,
    dosage: clingen_dosage.ClinGenDosage | None = None,
    gencc_table: gencc.GenCC | None = None,
    panelapp_table: panelapp.PanelAppTable | None = None,
    hgnc_set: hgnc.HgncCompleteSet | None = None,
) -> gene_disease_backend.ReferenceTables:
    """The reference tables, each empty unless a seeded one is supplied, under an HGNC set."""
    return gene_disease_backend.ReferenceTables(
        validity=validity or clingen_validity.ClinGenValidity({}, ()),
        dosage=dosage or clingen_dosage.ClinGenDosage({}, ()),
        gencc=gencc_table or gencc.GenCC({}, ()),
        panelapp=panelapp_table or panelapp.PanelAppTable({}, '', ()),
        hgnc=hgnc_set if hgnc_set is not None else _hgnc_set(_APPROVED),
    )


def _hgnc_set(approved: Mapping[str, str]) -> hgnc.HgncCompleteSet:
    return hgnc.HgncCompleteSet(
        {hgnc_id: hgnc.ApprovedGene(symbol, ()) for hgnc_id, symbol in approved.items()}, _HGNC_VERSION
    )


def _describe(
    tables: gene_disease_backend.ReferenceTables,
    request: gene_disease_pb2.DescribeGeneRequest,
    *,
    transport: httpx2.MockTransport | None = None,
) -> gene_disease_pb2.DescribeGeneResponse:
    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        active_transport = transport if transport is not None else httpx2.MockTransport(_no_network)
        async with destinations.admitting_client(
            timeout=httpx2.Timeout(10.0), transport=active_transport
        ) as http_client:
            backend = gene_disease_backend.LiveBackend(http_client, tables)

            def register(server: grpc.aio.Server) -> None:
                gene_disease_pb2_grpc.add_GeneDiseaseServicer_to_server(servicer_mod.Servicer(backend), server)

            async with authz.gated(register) as channel:
                return await gene_disease_pb2_grpc.GeneDiseaseStub(channel).DescribeGene(request)

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


def _seed_live_bucket(
    monkeypatch: pytest.MonkeyPatch,
    bucket: storage.Bucket,
    gcs_endpoint: str,
    *,
    omit: str | None = None,
) -> None:
    assert bucket.name is not None
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'live')
    monkeypatch.setenv('THEMIS_RESOURCES_BUCKET', bucket.name)
    monkeypatch.setenv('STORAGE_EMULATOR_HOST', gcs_endpoint)
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'themis-test')
    for name, content in _reference_blobs().items():
        if name != omit:
            bucket.blob(name).upload_from_string(content)


def test_gene_disease_returns_every_curated_entity_unreduced() -> None:
    resp = _describe(
        _tables(
            validity=_validity_table('HGNC:1100', 'Moderate', 'AD'),
            gencc_table=_gencc_table('HGNC:1100', 'Strong'),
            dosage=_dosage_table('HGNC:1100', 'Sufficient Evidence for Haploinsufficiency'),
            panelapp_table=_panelapp_table('HGNC:1100', max_confidence=3, moi='BIALLELIC'),
        ),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'),
    )
    assert [(e.source, e.validity_classification, e.gate_level) for e in resp.entities] == [
        ('ClinGen Gene Validity', 'Moderate', gene_disease_pb2.GATE_LEVEL_MODERATE),
        ('GenCC', 'Strong', gene_disease_pb2.GATE_LEVEL_STRONG),
    ]
    assert resp.coverage == gene_disease_pb2.GENE_COVERAGE_CURATED
    assert not resp.HasField('resolution')
    assert resp.gene_scoped.haploinsufficiency_score == 3
    assert resp.gene_scoped.mode_of_inheritance == 'BIALLELIC'
    assert resp.raw['panelapp_max_confidence'] == 3
    assert resp.gene_symbol == 'BRCA1'
    assert [p.source for p in resp.provenance] == [
        'HGNC',
        'ClinGen Gene Validity',
        'GenCC',
        'ClinGen Dosage Sensitivity',
        'PanelApp Australia',
    ]


def _validity_rows(
    hgnc_id: str, *curations: tuple[str, str, str], gene_symbol: str = 'GENE'
) -> clingen_validity.ClinGenValidity:
    rows = [
        {
            'GENE SYMBOL': gene_symbol,
            'GENE ID (HGNC)': hgnc_id,
            'DISEASE LABEL': f'condition {mondo_id}',
            'DISEASE ID (MONDO)': mondo_id,
            'MOI': moi,
            'CLASSIFICATION': classification,
        }
        for mondo_id, moi, classification in curations
    ]
    return clingen_validity.ClinGenValidity({hgnc_id.upper(): rows}, ('2026-01-01',))


def _gencc_rows(
    hgnc_id: str, *rows_spec: tuple[str, str, str, str, str, str], gene_symbol: str = 'GENE'
) -> gencc.GenCC:
    rows = [
        {
            'gene_curie': hgnc_id,
            'gene_symbol': gene_symbol,
            'disease_title': f'condition {mondo_id}',
            'disease_curie': mondo_id,
            'classification_title': classification,
            'moi_curie': moi_curie,
            'moi_title': moi_title,
            'submitter_title': submitter,
            'submitted_as_notes': notes,
            'submitted_run_date': '2026-01-01',
        }
        for mondo_id, moi_curie, moi_title, submitter, classification, notes in rows_spec
    ]
    return gencc.GenCC({hgnc_id.upper(): rows}, ('2026-01-01',))


def test_gene_disease_non_gate_named_tiers_keep_their_own_vocabulary_and_gate_level() -> None:
    resp = _describe(
        _tables(
            validity=_validity_rows(
                'HGNC:2',
                (_CURATED_TERM, 'AD', 'Refuted'),
                ('MONDO:0000102', 'AR', 'No Known Disease Relationship'),
            ),
            gencc_table=_gencc_table('HGNC:2', 'Supportive', mondo_id=_PARENT_TERM),
        ),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:2'),
    )
    assert [(e.validity_classification, e.gate_level) for e in resp.entities] == [
        ('Refuted', gene_disease_pb2.GATE_LEVEL_DISPUTED_OR_REFUTED),
        ('No Known Disease Relationship', gene_disease_pb2.GATE_LEVEL_LESS_THAN_LIMITED),
        ('Supportive', gene_disease_pb2.GATE_LEVEL_LIMITED),
    ]


def test_each_record_states_its_sources_own_mode_term() -> None:
    resp = _describe(
        _tables(
            validity=_validity_table('HGNC:1100', 'Definitive', 'XL'),
            gencc_table=_gencc_rows(
                'HGNC:1100',
                (_CURATED_TERM, 'HP:0001419', 'X-linked recessive', 'Submitter X', 'Strong', ''),
            ),
        ),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'),
    )
    assert [e.inheritance_term for e in resp.entities] == ['XL', 'X-linked recessive']


def test_x_linked_dominant_and_recessive_assertions_stay_two_records() -> None:
    resp = _describe(
        _tables(
            gencc_table=_gencc_rows(
                'HGNC:1100',
                (_CURATED_TERM, 'HP:0001419', 'X-linked recessive', 'Submitter X', 'Limited', ''),
                (_CURATED_TERM, 'HP:0001423', 'X-linked dominant', 'Submitter Y', 'Definitive', 'a narrative'),
            ),
        ),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'),
    )
    assert [(e.inheritance_term, e.validity_classification) for e in resp.entities] == [
        ('X-linked recessive', 'Limited'),
        ('X-linked dominant', 'Definitive'),
    ]


def test_an_assertion_stating_no_mode_fails_loud() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        _describe(
            _tables(validity=_validity_table('HGNC:1100', 'Definitive', ' ')),
            gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'),
        )
    assert caught.value.code() is grpc.StatusCode.UNKNOWN
    assert 'states no mode of inheritance' in (caught.value.details() or '')


def test_one_source_stating_one_record_twice_reduces_to_its_strongest() -> None:
    resp = _describe(
        _tables(
            validity=_validity_rows(
                'HGNC:1100',
                (_CURATED_TERM, 'AD', 'Limited'),
                (_CURATED_TERM, 'AD', 'Definitive'),
            )
        ),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'),
    )
    assert [(e.inheritance_term, e.validity_classification, e.gate_level) for e in resp.entities] == [
        ('AD', 'Definitive', gene_disease_pb2.GATE_LEVEL_DEFINITIVE)
    ]


def test_a_gencc_entity_keeps_every_submitter_assertion_and_non_blank_note() -> None:
    resp = _describe(
        _tables(
            gencc_table=_gencc_rows(
                'HGNC:1100',
                (_CURATED_TERM, 'HP:0000006', 'Autosomal dominant', 'Submitter X', 'Definitive', 'a mechanism note'),
                (_CURATED_TERM, 'HP:0000006', 'Autosomal dominant', 'Submitter Y', 'Limited', ''),
            )
        ),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'),
    )
    [entity] = resp.entities
    assert [(s.submitter, s.validity_classification) for s in entity.submissions] == [
        ('Submitter X', 'Definitive'),
        ('Submitter Y', 'Limited'),
    ]
    assert [s.text for s in entity.mechanism_statements] == ['a mechanism note']


def test_gene_disease_tells_an_absent_gene_from_one_with_no_validity_assertion() -> None:
    absent = _describe(
        _tables(hgnc_set=_hgnc_set({'HGNC:404040': 'GENE'})),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:404040'),
    )
    assert absent.coverage == gene_disease_pb2.GENE_COVERAGE_ABSENT
    assert not absent.entities
    assert not absent.gene_scoped.sources_holding_the_gene
    assert not absent.gene_scoped.HasField('haploinsufficiency_score')
    assert absent.raw.fields['clingen_dosage'].WhichOneof('kind') == 'null_value'
    assert [p.source for p in absent.provenance] == ['HGNC']

    held = _describe(
        _tables(panelapp_table=_panelapp_table('HGNC:9', max_confidence=3, moi='MONOALLELIC')),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:9'),
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
    resp = _describe(tables, gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'))
    assert resp.gene_symbol == 'TABLE1'
    assert not resp.previous_symbols
    assert 'HGNC' not in [p.source for p in resp.provenance]


def test_gene_disease_names_an_uncurated_gene_from_the_hgnc_set() -> None:
    resp = _describe(
        _tables(
            validity=_validity_table('HGNC:1100', 'Definitive', 'AD'),
            hgnc_set=_hgnc_set({'HGNC:404040': 'UNCURATED1'}),
        ),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:404040'),
    )
    assert resp.coverage == gene_disease_pb2.GENE_COVERAGE_ABSENT
    assert resp.gene_symbol == 'UNCURATED1'
    assert [(p.source, list(p.dataset_versions)) for p in resp.provenance] == [('HGNC', [_HGNC_VERSION])]


def test_gene_disease_answers_a_renamed_gene_under_its_approved_symbol_with_its_previous_ones() -> None:
    renamed = hgnc.HgncCompleteSet({'HGNC:1100': hgnc.ApprovedGene('CURRENT1', ('FORMER1', 'FORMER0'))}, _HGNC_VERSION)
    resp = _describe(
        _tables(validity=_validity_table('HGNC:1100', 'Definitive', 'AD', gene_symbol='FORMER1'), hgnc_set=renamed),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'),
    )
    assert resp.gene_symbol == 'CURRENT1'
    assert resp.previous_symbols == ['FORMER1', 'FORMER0']
    assert resp.coverage == gene_disease_pb2.GENE_COVERAGE_CURATED
    assert {p.source for p in resp.provenance} == {'HGNC', 'ClinGen Gene Validity'}
    rows = resp.raw.fields['clingen_validity'].struct_value.fields['rows'].list_value.values
    assert rows[0].struct_value.fields['GENE SYMBOL'].string_value == 'FORMER1'


def test_gene_disease_fails_an_id_neither_the_hgnc_set_nor_a_table_holds() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        _describe(
            _tables(
                validity=_validity_table('HGNC:1100', 'Definitive', 'AD'),
                hgnc_set=_hgnc_set({'HGNC:1100': 'BRCA1'}),
            ),
            gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:404040'),
        )
    assert caught.value.code() is grpc.StatusCode.NOT_FOUND
    assert authored_status.authored(caught.value)
    assert 'no gene under HGNC:404040' in (caught.value.details() or '')


def test_gene_disease_reads_an_id_hgnc_holds_unapproved_as_one_the_set_lacks() -> None:
    unapproved = hgnc.HgncCompleteSet.from_bytes(
        b'hgnc_id\tsymbol\tstatus\tprev_symbol\nHGNC:1100\tBRCA1\tApproved\t\nHGNC:2\tGONE\tEntry Withdrawn\t\n',
        version=_HGNC_VERSION,
    )
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        _describe(
            _tables(hgnc_set=unapproved),
            gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:2'),
        )
    assert caught.value.code() is grpc.StatusCode.NOT_FOUND
    assert 'no gene under HGNC:2' in (caught.value.details() or '')

    held = _describe(
        _tables(dosage=_dosage_table('HGNC:2', 'No Evidence for Haploinsufficiency'), hgnc_set=unapproved),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:2'),
    )
    assert held.gene_symbol == 'GENE'
    assert [p.source for p in held.provenance] == ['ClinGen Dosage Sensitivity']


def test_gene_disease_takes_the_first_table_symbol_where_two_spell_it_differently() -> None:
    resp = _describe(
        _tables(
            validity=_validity_table('HGNC:1100', 'Definitive', 'AD', gene_symbol='CURRENT1'),
            panelapp_table=_panelapp_table('HGNC:1100', max_confidence=3, gene_symbol='FORMER1'),
            hgnc_set=_hgnc_set(_ANOTHER_GENE),
        ),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'),
    )
    assert resp.gene_symbol == 'CURRENT1'
    assert resp.raw.fields['panelapp'].struct_value.fields['gene_symbol'].string_value == 'FORMER1'


def test_gene_disease_dosage_zero_is_present_not_absent() -> None:
    resp = _describe(
        _tables(dosage=_dosage_table('HGNC:3', 'No Evidence for Haploinsufficiency')),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:3'),
    )
    assert resp.gene_scoped.HasField('haploinsufficiency_score')
    assert resp.gene_scoped.haploinsufficiency_score == 0


def test_gene_disease_panelapp_green_is_not_a_curated_entity() -> None:
    resp = _describe(
        _tables(
            panelapp_table=_panelapp_table(
                'HGNC:9', max_confidence=3, moi='MONOALLELIC', mode_of_pathogenicity='gain-of-function'
            )
        ),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:9'),
    )
    assert not resp.entities
    assert resp.raw['panelapp_max_confidence'] == 3
    assert resp.gene_scoped.mode_of_inheritance == 'MONOALLELIC'
    assert resp.gene_scoped.mode_of_pathogenicity == 'gain-of-function'
    assert [p.source for p in resp.provenance] == ['HGNC', 'PanelApp Australia']


def test_gene_disease_scopes_mechanism_statements_to_what_they_are_about() -> None:
    resp = _describe(
        _tables(
            gencc_table=_gencc_table('HGNC:1100', 'Definitive', notes='loss-of-function mechanism'),
            panelapp_table=_panelapp_table(
                'HGNC:1100',
                max_confidence=3,
                evaluations=('green: LoF', 'expert review'),
                entries=[{'confidence_level': '3', 'publications': ['111', '222']}],
            ),
        ),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'),
    )
    entity_statements = [s for entity in resp.entities for s in entity.mechanism_statements]
    assert [(s.source, s.context, s.text) for s in entity_statements] == [
        ('GenCC', 'condition', 'loss-of-function mechanism')
    ]
    panel_statements = resp.gene_scoped.mechanism_statements
    assert [s.text for s in panel_statements] == ['green: LoF', 'expert review']
    assert all(s.source == 'PanelApp Australia' for s in panel_statements)
    assert all(s.context == 'Mendeliome, Incidentalome' for s in panel_statements)
    panelapp_raw = resp.raw.fields['panelapp'].struct_value
    assert 'entries' in panelapp_raw.fields
    first_entry = panelapp_raw.fields['entries'].list_value.values[0].struct_value
    publications = [value.string_value for value in first_entry.fields['publications'].list_value.values]
    assert publications == ['111', '222']


def test_gene_disease_resolving_the_curated_term_reads_no_ontology() -> None:
    resp = _describe(
        _tables(validity=_validity_table('HGNC:1100', 'Limited', 'AD')),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id=_CURATED_TERM),
    )
    assert resp.resolution.relation == gene_disease_pb2.TERM_RELATION_SAME
    assert [e.gate_level for e in resp.resolution.entities] == [gene_disease_pb2.GATE_LEVEL_LIMITED]
    assert [p.source for p in resp.provenance] == ['HGNC', 'ClinGen Gene Validity']
    assert 'mondo_ancestors' not in resp.raw


def test_gene_disease_answers_a_gene_carrying_a_mode_outside_the_framework() -> None:
    resp = _describe(
        _tables(
            validity=_validity_table('HGNC:1100', 'Definitive', 'AR'),
            gencc_table=_gencc_table(
                'HGNC:1100', 'Supportive', moi_curie='HP:0010984', moi_title='Digenic inheritance'
            ),
        ),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id=_CURATED_TERM),
    )
    assert [(e.source, e.inheritance_term) for e in resp.resolution.entities] == [
        ('ClinGen Gene Validity', 'AR'),
        ('GenCC', 'Digenic inheritance'),
    ]


def test_gene_disease_resolving_a_parent_term_reads_the_subclass_closure() -> None:
    resp = _describe(
        _tables(
            validity=_validity_rows(
                'HGNC:1100',
                (_CURATED_TERM, 'AD', 'Definitive'),
                (_CURATED_TERM, 'AR', 'Limited'),
            ),
            gencc_table=_gencc_table('HGNC:1100', 'Definitive', mondo_id='OMIM:123456'),
        ),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id=_PARENT_TERM),
        transport=_ols4_transport({_CURATED_TERM: (_PARENT_TERM,)}),
    )
    assert resp.resolution.relation == gene_disease_pb2.TERM_RELATION_DESCENDANT
    assert resp.resolution.mondo_id == _CURATED_TERM
    assert resp.resolution.requested_mondo_id == _PARENT_TERM
    assert [e.inheritance_term for e in resp.resolution.entities] == ['AD', 'AR']
    assert 'EBI OLS4 (MONDO)' in [p.source for p in resp.provenance]
    assert resp.raw['mondo_ancestors'] == {_CURATED_TERM: [_PARENT_TERM]}


def test_gene_disease_resolves_the_requested_term_to_every_record_on_it_even_when_a_descendant_is_curated() -> None:
    tables = _tables(
        validity=_validity_rows(
            'HGNC:1100',
            (_PARENT_TERM, 'AD', 'Definitive'),
            (_PARENT_TERM, 'AR', 'Limited'),
            (_CURATED_TERM, 'AD', 'Definitive'),
        ),
        gencc_table=_gencc_rows(
            'HGNC:1100',
            (_PARENT_TERM, 'HP:0000007', 'Autosomal recessive', 'Submitter X', 'Strong', ''),
            ('MONDO:0000102', 'HP:0000006', 'Autosomal dominant', 'Submitter Y', 'Definitive', ''),
        ),
    )
    resp = _describe(
        tables,
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id=_PARENT_TERM),
    )
    assert resp.resolution.relation == gene_disease_pb2.TERM_RELATION_SAME
    assert resp.resolution.mondo_id == _PARENT_TERM
    assert [(e.source, e.inheritance_term, e.validity_classification) for e in resp.resolution.entities] == [
        ('ClinGen Gene Validity', 'AD', 'Definitive'),
        ('ClinGen Gene Validity', 'AR', 'Limited'),
        ('GenCC', 'Autosomal recessive', 'Strong'),
    ]


def test_gene_disease_refuses_when_two_curated_terms_sit_under_the_requested_one() -> None:
    sibling = 'MONDO:0000102'
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        _describe(
            _tables(
                validity=_validity_rows(
                    'HGNC:1100',
                    (_CURATED_TERM, 'AD', 'Definitive'),
                    (sibling, 'AD', 'Limited'),
                )
            ),
            gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id=_PARENT_TERM),
            transport=_ols4_transport({_CURATED_TERM: (_PARENT_TERM,), sibling: (_PARENT_TERM,)}),
        )
    assert caught.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert authored_status.authored(caught.value)
    details = caught.value.details() or ''
    assert 'names no one of them' in details
    assert _CURATED_TERM in details
    assert sibling in details


def test_gene_disease_never_asks_mondo_about_a_curated_term_outside_mondo() -> None:
    tables = _tables(gencc_table=_gencc_table('HGNC:1100', 'Definitive', mondo_id='OMIM:123456'))
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        _describe(
            tables,
            gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id=_PARENT_TERM),
        )
    assert caught.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert authored_status.authored(caught.value)
    assert 'not the nearest level' in (caught.value.details() or '')


def test_gene_disease_answers_an_uncurated_gene_with_its_coverage_not_a_refusal() -> None:
    resp = _describe(
        _tables(dosage=_dosage_table('HGNC:3', 'Sufficient Evidence for Haploinsufficiency')),
        gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:3', mondo_id=_PARENT_TERM),
    )
    assert resp.coverage == gene_disease_pb2.GENE_COVERAGE_NO_VALIDITY_ASSERTION
    assert not resp.HasField('resolution')
    assert resp.gene_scoped.haploinsufficiency_score == 3


def test_gene_disease_refuses_an_entity_the_gene_is_not_curated_for() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        _describe(
            _tables(validity=_validity_table('HGNC:1100', 'Definitive', 'AD')),
            gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id=_PARENT_TERM),
            transport=_ols4_transport({_CURATED_TERM: ('MONDO:0000001',)}),
        )
    assert caught.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert authored_status.authored(caught.value)
    assert 'not the nearest level' in (caught.value.details() or '')


def test_gene_disease_from_reference_dumps_makes_no_network_call(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
) -> None:
    _seed_live_bucket(monkeypatch, gcs_bucket, gcs_endpoint)

    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with (
            contextlib.AsyncExitStack() as stack,
            destinations.admitting_client(
                timeout=httpx2.Timeout(10.0), transport=httpx2.MockTransport(_no_network)
            ) as http_client,
        ):
            deps = deps_mod.Deps(authorizer=authz.authorizer(), http_client=http_client, stack=stack)
            async with authz.gated(functools.partial(interface.register, deps=deps)) as channel:
                return await gene_disease_pb2_grpc.GeneDiseaseStub(channel).DescribeGene(
                    gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100')
                )

    resp = asyncio.run(run())
    assert resp.gene_symbol == 'BRCA1'
    hgnc_stamps = [p for p in resp.provenance if p.source == 'HGNC']
    assert [list(p.dataset_versions) for p in hgnc_stamps] == [[gene_disease_backend._HGNC_VERSION]]
    assert {e.source for e in resp.entities} == {'ClinGen Gene Validity', 'GenCC'}
    assert all(e.mondo_id.startswith('MONDO:') for e in resp.entities)
    assert gene_disease_pb2.GATE_LEVEL_UNSPECIFIED not in {e.gate_level for e in resp.entities}
    assert resp.gene_scoped.mechanism_statements


def test_create_fetches_the_resources_bucket_once(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
) -> None:
    """After startup loads the dumps into memory, serving never touches the bucket again."""
    _seed_live_bucket(monkeypatch, gcs_bucket, gcs_endpoint)

    async def run() -> tuple[gene_disease_pb2.DescribeGeneResponse, gene_disease_pb2.DescribeGeneResponse]:
        async with (
            contextlib.AsyncExitStack() as stack,
            destinations.admitting_client(
                timeout=httpx2.Timeout(10.0), transport=httpx2.MockTransport(_no_network)
            ) as http_client,
        ):
            deps = deps_mod.Deps(authorizer=authz.authorizer(), http_client=http_client, stack=stack)
            async with authz.gated(functools.partial(interface.register, deps=deps)) as channel:
                for blob in list(gcs_bucket.list_blobs()):
                    blob.delete()
                stub = gene_disease_pb2_grpc.GeneDiseaseStub(channel)
                first = await stub.DescribeGene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'))
                second = await stub.DescribeGene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'))
                return first, second

    first, second = asyncio.run(run())
    assert first.gene_symbol == 'BRCA1'
    assert second.gene_symbol == 'BRCA1'
    assert {e.source for e in second.entities} == {'ClinGen Gene Validity', 'GenCC'}


@pytest.mark.parametrize('missing', gene_disease_backend._REFERENCE_OBJECTS)
def test_create_fails_when_a_reference_object_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
    missing: str,
) -> None:
    _seed_live_bucket(monkeypatch, gcs_bucket, gcs_endpoint, omit=missing)

    async def run() -> None:
        async with (
            contextlib.AsyncExitStack() as stack,
            destinations.admitting_client(
                timeout=httpx2.Timeout(10.0), transport=httpx2.MockTransport(_no_network)
            ) as http_client,
        ):
            deps = deps_mod.Deps(authorizer=authz.authorizer(), http_client=http_client, stack=stack)
            async with authz.gated(functools.partial(interface.register, deps=deps)):
                pass

    with pytest.raises(RuntimeError, match=re.escape(missing)):
        asyncio.run(run())


def test_the_hgnc_object_is_where_the_mirror_writes_the_complete_set() -> None:
    pinned = manifest.load()
    read = dataclasses.replace(pinned.table('hgnc-complete-set'), version=gene_disease_backend._HGNC_VERSION)
    assert f'{pinned.dataset}/{read.object}' == gene_disease_backend._HGNC_OBJECT


def test_the_hgnc_release_is_not_ahead_of_the_one_the_manifest_pins() -> None:
    pinned = dt.date.fromisoformat(manifest.load().table('hgnc-complete-set').version)
    assert dt.date.fromisoformat(gene_disease_backend._HGNC_VERSION) <= pinned
