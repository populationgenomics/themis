"""LiveBackend composition: the entity assembly and the entity resolution `DescribeGene` runs.

The reference tables are seeded in memory and the MONDO client function is replaced with a canned
Result, so no test here touches the network — which several of them assert outright, since an
unresolved request is meant to stay a pure table lookup.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Awaitable, Callable

import httpx2
import pytest

from themis.rpc import gene_disease_pb2
from themis.services.evidence import errors
from themis.services.evidence.gene_disease import backend as gene_disease_backend
from themis.services.evidence.upstreams import clingen_dosage, clingen_validity, gencc, mondo, panelapp

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
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
) -> gene_disease_backend.ReferenceTables:
    """The four reference tables, empty unless a seeded one is supplied."""
    return gene_disease_backend.ReferenceTables(
        validity=validity or clingen_validity.ClinGenValidity({}, ()),
        dosage=dosage or clingen_dosage.ClinGenDosage({}, ()),
        gencc=gencc_table or gencc.GenCC({}, ()),
        panelapp=panelapp_table or panelapp.PanelAppTable({}, '', ()),
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
    hgnc_id: str, classification: str, moi: str, *, mondo_id: str = _CURATED_TERM
) -> clingen_validity.ClinGenValidity:
    row = {
        'GENE SYMBOL': 'GENE',
        'GENE ID (HGNC)': hgnc_id,
        'DISEASE LABEL': 'condition',
        'DISEASE ID (MONDO)': mondo_id,
        'MOI': moi,
        'CLASSIFICATION': classification,
    }
    return clingen_validity.ClinGenValidity({hgnc_id.upper(): [row]}, ('2026-01-01',))


def _gencc_table(
    hgnc_id: str, classification: str, *, notes: str = '', mondo_id: str = _CURATED_TERM, moi_curie: str = 'HP:0000006'
) -> gencc.GenCC:
    row = {
        'gene_curie': hgnc_id,
        'gene_symbol': 'GENE',
        'disease_title': 'condition',
        'disease_curie': mondo_id,
        'classification_title': classification,
        'moi_curie': moi_curie,
        'moi_title': 'Autosomal dominant',
        'submitter_title': 'ClinGen',
        'submitted_as_notes': notes,
        'submitted_run_date': '2026-01-01',
    }
    return gencc.GenCC({hgnc_id.upper(): [row]}, ('2026-01-01',))


def _dosage_table(hgnc_id: str, label: str) -> clingen_dosage.ClinGenDosage:
    row = {'GENE SYMBOL': 'GENE', 'HGNC ID': hgnc_id, 'HAPLOINSUFFICIENCY': label}
    return clingen_dosage.ClinGenDosage({hgnc_id.upper(): row}, ('2026-01-01',))


def _panelapp_table(
    hgnc_id: str,
    *,
    max_confidence: int,
    moi: str = '',
    mode_of_pathogenicity: str = '',
    evaluations: tuple[str, ...] = (),
    entries: list[dict[str, object]] | None = None,
) -> panelapp.PanelAppTable:
    entry = {
        'gene_symbol': 'GENE',
        'max_confidence': max_confidence,
        'mode_of_inheritance': moi,
        'mode_of_pathogenicity': mode_of_pathogenicity,
        'evaluations': list(evaluations),
        'entries': entries if entries is not None else [{'confidence_level': str(max_confidence), 'publications': []}],
    }
    return panelapp.PanelAppTable({hgnc_id.upper(): entry}, 'Mendeliome, Incidentalome', ('2026-01-01',))


def _reference_blobs() -> dict[str, bytes]:
    """The four reference dumps read from the upstream fixtures, keyed by their bucket object name."""
    return {
        gene_disease_backend._GENCC_OBJECT: (_FIXTURES / 'gencc.tsv').read_bytes(),
        gene_disease_backend._VALIDITY_OBJECT: (_FIXTURES / 'clingen_validity.csv').read_bytes(),
        gene_disease_backend._DOSAGE_OBJECT: (_FIXTURES / 'clingen_dosage.csv').read_bytes(),
        gene_disease_backend._PANELAPP_OBJECT: (_FIXTURES / 'panelapp.json').read_bytes(),
    }


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
    assert len(resp.provenance) == 4


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
        _tables(),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:404040')),
    )
    assert absent.coverage == gene_disease_pb2.GENE_COVERAGE_ABSENT
    assert not absent.entities
    assert not absent.gene_scoped.sources_holding_the_gene
    assert not absent.gene_scoped.HasField('haploinsufficiency_score')
    assert absent.raw.fields['clingen_dosage'].WhichOneof('kind') == 'null_value'
    assert not absent.provenance

    held = _run(
        _tables(panelapp_table=_panelapp_table('HGNC:9', max_confidence=3, moi='MONOALLELIC')),
        lambda be: be.describe_gene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:9')),
    )
    assert held.coverage == gene_disease_pb2.GENE_COVERAGE_NO_VALIDITY_ASSERTION
    assert not held.entities
    assert held.gene_scoped.sources_holding_the_gene == ['PanelApp Australia']


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
    assert len(resp.provenance) == 1


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
                gene_disease_pb2.DescribeGeneRequest(
                    hgnc_id='HGNC:1100',
                    mondo_id=_CURATED_TERM,
                    inheritance=gene_disease_pb2.INHERITANCE_AUTOSOMAL_DOMINANT,
                )
            )

    resp = asyncio.run(run())
    assert resp.resolution.relation == gene_disease_pb2.TERM_RELATION_SAME
    assert [e.gate_level for e in resp.resolution.entities] == [gene_disease_pb2.GATE_LEVEL_LIMITED]
    assert [p.source for p in resp.provenance] == ['ClinGen Gene Validity']  # no MONDO round-trip
    assert 'mondo_ancestors' not in resp.raw


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
        lambda be: be.describe_gene(
            gene_disease_pb2.DescribeGeneRequest(
                hgnc_id='HGNC:1100',
                mondo_id=_PARENT_TERM,
                inheritance=gene_disease_pb2.INHERITANCE_AUTOSOMAL_DOMINANT,
            )
        ),
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
