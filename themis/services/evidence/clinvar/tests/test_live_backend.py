"""LiveBackend composition: the record mapping, the pool census, and the archive carried whole.

Every upstream client function is replaced with a canned Result — the span leg over the recorded NF1
exon table, the archive leg over the committed VCV XML — so no test here touches the network.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import Awaitable, Callable

import clinvar_proto
import defusedxml.ElementTree
import httpx
import pytest

from themis.rpc import clinvar_pb2
from themis.services.evidence import hgvs
from themis.services.evidence.clinvar import backend as clinvar_backend
from themis.services.evidence.upstreams import clinvar, transcript_structure

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_NF1_TRANSCRIPT = 'NM_001042492.3'
_VCV = 'VCV001731988'


def _returns[T](value: T) -> Callable[..., Awaitable[T]]:
    """An async stand-in for an upstream client function that ignores its args and returns `value`."""

    async def fake(*_args: object, **_kwargs: object) -> T:
        return value

    return fake


def _run[T](call: Callable[[clinvar_backend.LiveBackend], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx.AsyncClient() as client:
            return await call(clinvar_backend.LiveBackend(client))

    return asyncio.run(run())


def _clinvar_record(
    clinvar_id: str,
    title: str,
    classification: str,
    review_stars: int,
    review_status: str,
    conditions: list[str],
    submissions: list[clinvar.ClinvarSubmissionData] | None = None,
) -> clinvar.ClinvarRecordData:
    """One parsed record, its coding span read off ``title`` exactly as the adapter reads it."""
    return clinvar.ClinvarRecordData(
        clinvar_id=clinvar_id,
        hgvs=title,
        classification=classification,
        review_stars=review_stars,
        review_status=review_status,
        conditions=conditions,
        coding_span=hgvs.coding_span(title),
        submissions=[] if submissions is None else submissions,
    )


def _gene_pool(records: list[clinvar.ClinvarRecordData], *, total: int, considered: int) -> clinvar.ClinvarGenePool:
    return clinvar.ClinvarGenePool(
        records=records,
        total=total,
        considered=considered,
        source='NCBI ClinVar (E-utilities)',
        dataset_versions=('clinvar',),
        query='q',
    )


def _variation_archive(fixture: str = 'clinvar_vcv.xml') -> clinvar_proto.clinvar_pb2.VariationArchiveType:
    """The typed archive a committed VCV XML converts to, over the adapter's own converter path."""
    root = defusedxml.ElementTree.fromstring((_FIXTURES / fixture).read_bytes())
    archive = root.find('VariationArchive')
    assert archive is not None, f'{fixture} carries no VariationArchive'
    return clinvar_proto.xml_converter.VariationArchiveType(archive)


def _variant_archive(record: clinvar.ClinvarRecordData) -> clinvar.ClinvarArchive:
    return clinvar.ClinvarArchive(
        record=record,
        variation_archive=_variation_archive(),
        source='NCBI ClinVar (E-utilities)',
        dataset_versions=('clinvar',),
        query='efetch',
    )


def _seed_clinvar(
    monkeypatch: pytest.MonkeyPatch,
    *,
    this_variant: clinvar.ClinvarRecordData,
    pool: clinvar.ClinvarGenePool,
) -> None:
    """Seed the two lookups `describe_variant` composes when the request names a variation."""
    monkeypatch.setattr(clinvar, 'fetch_variant_archive', _returns(_variant_archive(this_variant)))
    monkeypatch.setattr(clinvar, 'fetch_gene_pool', _returns(pool))


def _nf1_structure() -> transcript_structure.TranscriptStructureResult:
    return transcript_structure.parse_transcript_structure(
        json.loads((_FIXTURES / 'transcript_structure.json').read_text()),
        transcript=_NF1_TRANSCRIPT,
        genome_build='GRCh38',
        dataset_versions=('vvta_2025_02',),
        query='gene2transcripts',
    )


def test_clinvar_maps_records_and_the_pool_census(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = clinvar.ClinvarObservationData(
        origin='germline',
        affected_status='yes',
        zygosities=[
            clinvar.ClinvarZygosityCountData('Homozygote', 5),
            clinvar.ClinvarZygosityCountData('SingleHeterozygote', 119),
        ],
        variant_alleles=124,
        age='minimum=35years',
        sex='female',
        collection_method='clinical testing',
        descriptions=['segregates in three affected relatives', 'a second reported family'],
        traits=['Neurofibromatosis, type 1'],
        pubmed_ids=['24789688'],
    )
    submission = clinvar.ClinvarSubmissionData(
        scv='SCV000000001',
        submitter='A Lab',
        organization_category='laboratory',
        classification='Pathogenic',
        review_status='criteria provided, single submitter',
        date_evaluated='2024-10-25',
        assertion_method='ACMG Guidelines, 2015',
        mode_of_inheritance='Autosomal dominant inheritance',
        comment='truncating in a gene where LoF is the mechanism',
        conditions=['Neurofibromatosis, type 1'],
        pubmed_ids=['24789688'],
        erepo_url='https://erepo.clinicalgenome.org/evrepo/ui/classification/x',
        observations=[observation],
    )
    this_variant = _clinvar_record(
        'VCV9',
        'NM_007294.4(BRCA1):c.5266dupC',
        'Likely pathogenic',
        1,
        'criteria provided, single submitter',
        ['Hereditary cancer-predisposing syndrome', 'not provided'],
        [submission],
    )
    pool = [
        _clinvar_record(
            'VCV1', 'NM_007294.4(BRCA1):c.1521_1523delCTT', 'Pathogenic', 3, 'reviewed by expert panel', ['c']
        ),
        # A copy-number record: ClinVar titles it cytogenetically, so it carries no c. span.
        _clinvar_record(
            'VCV2',
            'GRCh38/hg38 17q21.31(chr17:43044295-43125364)x1',
            'Pathogenic',
            2,
            'criteria provided, multiple submitters, no conflicts',
            ['c'],
        ),
    ]
    _seed_clinvar(monkeypatch, this_variant=this_variant, pool=_gene_pool(pool, total=7310, considered=500))
    resp = _run(lambda be: be.describe_variant(clinvar_pb2.DescribeVariantRequest(vcv=_VCV, gene='BRCA1')))
    assert resp.this_variant.clinvar_id == 'VCV9'
    assert resp.this_variant.classification == 'Likely pathogenic'
    # A trait SET reaches the message as one, not joined into a string.
    assert list(resp.this_variant.conditions) == ['Hereditary cancer-predisposing syndrome', 'not provided']
    assert [r.clinvar_id for r in resp.classified_in_gene] == ['VCV1', 'VCV2']
    assert resp.classified_in_gene[0].review_stars == 3
    assert resp.provenance[0].source == 'NCBI ClinVar (E-utilities)'
    # The census: without it a 500-record pool is indistinguishable from a complete one.
    assert resp.total_in_gene == 7310
    assert resp.considered_in_gene == 500
    assert resp.pool_truncated
    # The per-submission evidence *_INF eligibility and circularity are judged over.
    assert [s.scv for s in resp.this_variant.submissions] == ['SCV000000001']
    mapped = resp.this_variant.submissions[0]
    assert mapped.assertion_method == 'ACMG Guidelines, 2015'
    assert mapped.organization_category == 'laboratory'
    assert list(mapped.pubmed_ids) == ['24789688']
    assert mapped.erepo_url
    # A cohort observed at two zygosities keeps both, and the allele count stays the allele count.
    assert [(z.zygosity, z.count) for z in mapped.observations[0].zygosities] == [
        ('Homozygote', 5),
        ('SingleHeterozygote', 119),
    ]
    assert mapped.observations[0].variant_alleles == 124
    assert len(mapped.observations[0].descriptions) == 2
    assert list(mapped.observations[0].pubmed_ids) == ['24789688']
    assert list(mapped.observations[0].traits) == ['Neurofibromatosis, type 1']
    # The gene pool is summarised in bulk, so it carries no submission detail to invent.
    assert not resp.classified_in_gene[0].submissions
    # A record placeable against an exon table carries its span; one that is not is NAMED, so it
    # cannot drop out of a per-exon tally by being absent from every bucket.
    deletion = resp.classified_in_gene[0].coding_span
    assert deletion.transcript == 'NM_007294.4'
    assert (deletion.start.position, deletion.end.position) == (1521, 1523)
    assert deletion.start.region == clinvar_pb2.CODING_REGION_CDS
    assert not resp.classified_in_gene[1].HasField('coding_span')
    assert list(resp.records_with_unparsed_hgvs) == ['VCV2']
    # The term is what says what the pool is a pool OF: the membership disjunction plus the floor.
    assert resp.esearch_term == 'q'
    # The archive comes back whole, carrying the two facts the reading above cannot: what kind of
    # unit ClinVar classified, and whether it classified it in its own right.
    assert resp.variation_archive.accession == _VCV
    assert resp.variation_archive.variation_type == 'single nucleotide variant'
    assert resp.variation_archive.record_type == clinvar_proto.clinvar_pb2.VariationArchiveType.RECORD_TYPE_CLASSIFIED


def test_clinvar_omits_an_unstated_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unstated count must stay absent — read as 0 it becomes an observation of nobody."""
    observation = clinvar.ClinvarObservationData(
        origin='germline',
        affected_status='unknown',
        zygosities=[clinvar.ClinvarZygosityCountData('Homozygote', None)],
        variant_alleles=None,
        age='',
        sex='',
        collection_method='clinical testing',
        descriptions=[],
        traits=[],
        pubmed_ids=[],
    )
    submission = clinvar.ClinvarSubmissionData(
        scv='SCV1',
        submitter='A Lab',
        organization_category='laboratory',
        classification='Pathogenic',
        review_status='criteria provided, single submitter',
        date_evaluated='',
        assertion_method='',
        mode_of_inheritance='',
        comment='',
        conditions=[],
        pubmed_ids=[],
        erepo_url='',
        observations=[observation],
    )
    _seed_clinvar(
        monkeypatch,
        this_variant=_clinvar_record(
            'VCV9',
            'NM_007294.4(BRCA1):c.68A>G',
            'Pathogenic',
            1,
            'criteria provided, single submitter',
            ['c'],
            [submission],
        ),
        pool=_gene_pool([], total=0, considered=0),
    )
    resp = _run(lambda be: be.describe_variant(clinvar_pb2.DescribeVariantRequest(vcv=_VCV, gene='X')))
    mapped = resp.this_variant.submissions[0].observations[0]
    assert not mapped.HasField('variant_alleles')
    assert not mapped.zygosities[0].HasField('count')


def test_clinvar_surfaces_the_review_status_phrase_behind_the_stars(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pool record carries no submissions, so the record itself must state its consensus tier."""
    pool = [
        _clinvar_record('VCV1', 'NM_007294.4(BRCA1):c.1A>G', 'Pathogenic', 3, 'reviewed by expert panel', ['c']),
        _clinvar_record(
            'VCV2', 'NM_007294.4(BRCA1):c.2A>G', 'Pathogenic', 1, 'criteria provided, single submitter', ['c']
        ),
    ]
    monkeypatch.setattr(clinvar, 'fetch_gene_pool', _returns(_gene_pool(pool, total=2, considered=2)))
    resp = _run(lambda be: be.describe_variant(clinvar_pb2.DescribeVariantRequest(gene='BRCA1')))

    assert [r.review_status for r in resp.classified_in_gene] == [
        'reviewed by expert panel',
        'criteria provided, single submitter',
    ]
    assert [r.review_stars for r in resp.classified_in_gene] == [3, 1]


def test_an_unnamed_variation_answers_with_the_gene_pool_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The crosswalk naming no ClinVar variation is the ordinary case for a novel allele.

    There is nothing to fetch, so nothing is fetched — and the absence of `this_variant` is the
    request's own, never a lookup that came back empty.
    """

    async def never(*_args: object, **_kwargs: object) -> clinvar.ClinvarArchive:
        raise AssertionError('an unset accession names no archive to fetch')

    monkeypatch.setattr(clinvar, 'fetch_variant_archive', never)
    monkeypatch.setattr(clinvar, 'fetch_gene_pool', _returns(_gene_pool([], total=0, considered=0)))

    resp = _run(lambda be: be.describe_variant(clinvar_pb2.DescribeVariantRequest(gene='FOXG1')))

    assert not resp.HasField('this_variant')
    assert not resp.HasField('variation_archive')
    # One provenance entry per request issued, so the pool's is the only one here.
    assert [p.source for p in resp.provenance] == ['NCBI ClinVar (E-utilities)']


def test_search_coding_span_searches_the_projected_interval_and_keeps_every_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The census the classification-scoped pool cannot give: the benign and VUS arms of *_INF."""
    searched: dict[str, object] = {}

    async def fake_span(gene: str, start: int, end: int, **_kwargs: object) -> clinvar.ClinvarSpanRecords:
        searched.update(gene=gene, start=start, end=end)
        return clinvar.ClinvarSpanRecords(
            records=[
                _clinvar_record('VCV1', f'{_NF1_TRANSCRIPT}(NF1):c.3496G>C', 'Likely benign', 1, 'single', ['c']),
                _clinvar_record('VCV2', f'{_NF1_TRANSCRIPT}(NF1):c.3497T>G', 'Uncertain significance', 1, 's', ['c']),
                _clinvar_record('VCV3', 'GRCh38/hg38 17q11.2(chr17:31...)x1', 'Pathogenic', 2, 'multiple', ['c']),
            ],
            total=3,
            considered=3,
            source='NCBI ClinVar (E-utilities)',
            dataset_versions=('clinvar',),
            query='term',
        )

    monkeypatch.setattr(transcript_structure, 'fetch_transcript_structure', _returns(_nf1_structure()))
    monkeypatch.setattr(clinvar, 'fetch_span_records', fake_span)

    resp = _run(
        lambda be: be.search_coding_span(
            clinvar_pb2.SearchCodingSpanRequest(
                transcript=_NF1_TRANSCRIPT, cds_start=3496, cds_end=3498, max_records=50
            )
        )
    )

    # The gene scoping the search is the exon table's, not a request field that could disagree with it.
    assert searched['gene'] == 'NF1'
    assert resp.gene == 'NF1'
    assert resp.searched_span.start == searched['start']
    assert resp.searched_span.end == searched['end']
    # Every classification, which is the whole point; and the unplaceable record is named, not dropped.
    assert {r.classification for r in resp.records} == {'Likely benign', 'Uncertain significance', 'Pathogenic'}
    assert list(resp.records_with_unparsed_hgvs) == ['VCV3']
    assert (resp.total_in_span, resp.considered_in_span, resp.span_truncated) == (3, 3, False)
    assert {p.source for p in resp.provenance} == {
        'VariantValidator gene2transcripts',
        'NCBI ClinVar (E-utilities)',
    }
    # Two clauses ride in the term, so it is what says which records could have landed in the span.
    assert resp.esearch_term == 'term'
    # The projection is replayable against what produced `searched_span`.
    assert resp.variantvalidator_transcript['reference'] == _NF1_TRANSCRIPT
