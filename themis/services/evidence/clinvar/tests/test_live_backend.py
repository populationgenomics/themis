"""Live `ClinVar` RPCs over in-process gRPC and `httpx2.MockTransport`."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import pathlib
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping, Sequence

import clinvar_proto
import httpx2
import pytest

from themis.rpc import clinvar_pb2, clinvar_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.clinvar import interface
from themis.services.evidence.tests import authz
from themis.services.evidence.upstreams import clinvar as clinvar_upstream
from themis.services.evidence.upstreams import destinations

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_NF1_TRANSCRIPT = 'NM_001042492.3'
_NF1_STRUCTURE = json.loads((_FIXTURES / 'transcript_structure.json').read_bytes())
_CLINVAR_JSON = json.loads((_FIXTURES / 'clinvar.json').read_bytes())
_EXPERT_PANEL_XML = (_FIXTURES / 'clinvar_vcv_expert_panel.xml').read_bytes()
_OBSERVATIONS_XML = (_FIXTURES / 'clinvar_vcv_observations.xml').read_bytes()
_VV_METADATA = {
    'metadata': {
        'variantvalidator_version': '4.0.1.dev7+gbdab9c72f',
        'vvdb_version': 'vvdb_2025_3',
        'vvta_version': 'vvta_2025_02',
    }
}


def _summary_item(
    uid: str,
    accession: str,
    title: str,
    classification: str,
    review_status: str,
    *,
    span: tuple[int, int] | None = None,
) -> dict[str, object]:
    locations = [{'assembly_name': 'GRCh38', 'start': str(span[0]), 'stop': str(span[1])}] if span is not None else []
    return {
        'uid': uid,
        'accession': accession,
        'title': title,
        'germline_classification': {
            'description': classification,
            'review_status': review_status,
            'trait_set': [{'trait_name': 'Neurofibromatosis, type 1'}],
        },
        'variation_set': [{'variation_loc': locations}],
    }


def _eutils_handler(
    *,
    term_substrings: Sequence[str],
    items: Sequence[Mapping[str, object]],
    total: int,
    archives: Mapping[str, bytes] | None = None,
) -> Callable[[httpx2.Request], httpx2.Response]:
    uids = [str(item['uid']) for item in items]
    by_uid = {str(item['uid']): item for item in items}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/efetch.fcgi'):
            assert archives is not None, 'an unset accession names no archive to fetch'
            vcv = request.url.params.get('id') or ''
            assert vcv in archives
            return httpx2.Response(200, content=archives[vcv])
        if request.url.path.endswith('/esearch.fcgi'):
            term = request.url.params.get('term') or ''
            for substring in term_substrings:
                assert substring in term
            retstart = int(request.url.params.get('retstart') or '0')
            retmax = int(request.url.params.get('retmax') or '20')
            return httpx2.Response(
                200,
                json={'esearchresult': {'count': str(total), 'idlist': uids[retstart : retstart + retmax]}},
            )
        assert request.url.path.endswith('/esummary.fcgi')
        form = urllib.parse.parse_qs(request.content.decode())
        requested_uids = [u for u in form.get('id', [''])[0].split(',') if u]
        assert requested_uids
        return httpx2.Response(
            200,
            json={'result': {'uids': requested_uids, **{u: by_uid[u] for u in requested_uids}}},
        )

    return handler


def _run[T](
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx2.Request], httpx2.Response],
    call: Callable[[clinvar_pb2_grpc.ClinVarAsyncStub], Awaitable[T]],
) -> T:
    monkeypatch.setenv('THEMIS_CLINVAR_BACKEND', 'live')
    monkeypatch.setattr(clinvar_upstream, '_RATE_LIMIT_DELAY_S', 0)

    async def run() -> T:
        async with (
            contextlib.AsyncExitStack() as stack,
            destinations.admitting_client(
                timeout=httpx2.Timeout(10.0), transport=httpx2.MockTransport(handler)
            ) as http_client,
        ):
            deps = deps_mod.Deps(authorizer=authz.authorizer(), http_client=http_client, stack=stack)
            async with authz.gated(functools.partial(interface.register, deps=deps)) as channel:
                return await call(clinvar_pb2_grpc.ClinVarStub(channel))

    return asyncio.run(run())


def test_clinvar_maps_records_and_the_pool_census(monkeypatch: pytest.MonkeyPatch) -> None:
    vcv = 'VCV000000576'
    handler = _eutils_handler(
        term_substrings=('PAH[gene]',),
        items=[
            _summary_item(
                '1',
                'VCV000000001',
                'NM_000277.3(PAH):c.1222C>T',
                'Pathogenic',
                'reviewed by expert panel',
            ),
            _summary_item(
                '2',
                'VCV000000002',
                'GRCh38/hg38 12q23.2(chr12:102836885-102917603)x1',
                'Pathogenic',
                'criteria provided, multiple submitters, no conflicts',
            ),
            _summary_item(
                '3',
                'VCV000000003',
                'NM_000277.3(PAH):c.1315+1G>A',
                'Pathogenic',
                'criteria provided, single submitter',
            ),
        ],
        total=731,
        archives={vcv: _EXPERT_PANEL_XML},
    )

    resp = _run(
        monkeypatch,
        handler,
        lambda stub: stub.DescribeVariant(clinvar_pb2.DescribeVariantRequest(vcv=vcv, gene='PAH', max_pool_records=2)),
    )
    assert resp.this_variant.clinvar_id == vcv
    assert resp.this_variant.classification == 'Pathogenic'
    assert 'Phenylketonuria' in resp.this_variant.conditions
    assert 'not provided' in resp.this_variant.conditions
    assert [r.clinvar_id for r in resp.classified_in_gene] == ['VCV000000001', 'VCV000000002']
    assert resp.classified_in_gene[0].review_stars == 3
    assert resp.provenance[0].source == 'NCBI ClinVar (E-utilities)'
    assert resp.total_in_gene == 731
    assert resp.considered_in_gene == 2
    assert resp.pool_truncated
    assert [s.scv for s in resp.this_variant.submissions] == ['SCV000110366', 'SCV000852099']
    lab, panel = resp.this_variant.submissions
    assert lab.organization_category == 'laboratory'
    assert lab.assertion_method == 'EGL Classification Definitions 2015'
    assert '23500595' in lab.pubmed_ids
    assert panel.erepo_url.startswith('https://erepo.clinicalgenome.org/')
    assert [(z.zygosity, z.count) for z in lab.observations[0].zygosities] == [
        ('Homozygote', 4),
        ('SingleHeterozygote', 67),
    ]
    assert lab.observations[0].variant_alleles == 71
    assert not resp.classified_in_gene[0].submissions
    coding = resp.classified_in_gene[0].coding_span
    assert coding.transcript == 'NM_000277.3'
    assert (coding.start.position, coding.end.position) == (1222, 1222)
    assert coding.start.region == clinvar_pb2.CODING_REGION_CDS
    assert not resp.classified_in_gene[1].HasField('coding_span')
    assert list(resp.records_with_unparsed_hgvs) == ['VCV000000002']
    assert 'PAH[gene]' in resp.esearch_term
    assert resp.variation_archive.accession == vcv
    assert resp.variation_archive.variation_type == 'single nucleotide variant'
    assert resp.variation_archive.record_type == clinvar_proto.clinvar_pb2.VariationArchiveType.RECORD_TYPE_CLASSIFIED


def test_clinvar_omits_an_unstated_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unstated count must stay absent — read as 0 it becomes an observation of nobody."""
    vcv = 'VCV000000009'
    unstated_zygosity_xml = _OBSERVATIONS_XML.replace(
        b'<Attribute Type="Homozygote" integerValue="1" />',
        b'<Attribute Type="Homozygote" />',
    )
    handler = _eutils_handler(
        term_substrings=('HFE[gene]',),
        items=[],
        total=0,
        archives={vcv: unstated_zygosity_xml},
    )

    resp = _run(
        monkeypatch,
        handler,
        lambda stub: stub.DescribeVariant(clinvar_pb2.DescribeVariantRequest(vcv=vcv, gene='HFE', max_pool_records=50)),
    )
    registry_sub = next(s for s in resp.this_variant.submissions if s.scv == 'SCV003931195')
    mapped = registry_sub.observations[0]
    assert not mapped.HasField('variant_alleles')
    assert [z.zygosity for z in mapped.zygosities] == ['Homozygote']
    assert not mapped.zygosities[0].HasField('count')


def test_clinvar_surfaces_the_review_status_phrase_behind_the_stars(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pool record carries no submissions, so the record itself must state its consensus tier."""
    handler = _eutils_handler(
        term_substrings=('BRCA1[gene]',),
        items=[
            _summary_item('1', 'VCV000000001', 'NM_007294.4(BRCA1):c.1A>G', 'Pathogenic', 'reviewed by expert panel'),
            _summary_item(
                '2', 'VCV000000002', 'NM_007294.4(BRCA1):c.2A>G', 'Pathogenic', 'criteria provided, single submitter'
            ),
        ],
        total=2,
    )

    resp = _run(
        monkeypatch,
        handler,
        lambda stub: stub.DescribeVariant(clinvar_pb2.DescribeVariantRequest(gene='BRCA1', max_pool_records=50)),
    )
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
    esearch_gene = _CLINVAR_JSON['esearch_gene']['esearchresult']
    esummary_result = _CLINVAR_JSON['esummary_gene']['result']
    fixture_items = [esummary_result[uid] for uid in esummary_result['uids']]
    handler = _eutils_handler(
        term_substrings=('NF1[gene]',),
        items=fixture_items,
        total=int(esearch_gene['count']),
    )

    resp = _run(
        monkeypatch,
        handler,
        lambda stub: stub.DescribeVariant(clinvar_pb2.DescribeVariantRequest(gene='NF1', max_pool_records=5)),
    )
    assert not resp.HasField('this_variant')
    assert not resp.HasField('variation_archive')
    assert len(resp.classified_in_gene) == 5
    assert [p.source for p in resp.provenance] == ['NCBI ClinVar (E-utilities)']


def test_search_coding_span_searches_the_projected_interval_and_keeps_every_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The census the classification-scoped pool cannot give: the benign and VUS arms of *_INF."""
    # c.3496..3498 on NF1 NM_001042492.3 spans exon 26's last base (31232881) into exon 27 (31233003).
    eutils = _eutils_handler(
        term_substrings=('NF1[gene]', '31232881:31233003[Base Position]'),
        items=[
            _summary_item(
                '1',
                'VCV000000001',
                f'{_NF1_TRANSCRIPT}(NF1):c.3496G>C',
                'Likely benign',
                'criteria provided, single submitter',
                span=(31232881, 31232881),
            ),
            _summary_item(
                '2',
                'VCV000000002',
                f'{_NF1_TRANSCRIPT}(NF1):c.3497T>G',
                'Uncertain significance',
                'criteria provided, single submitter',
                span=(31233002, 31233002),
            ),
            _summary_item(
                '3',
                'VCV000000003',
                'GRCh38/hg38 17q11.2(chr17:31232000-31234000)x1',
                'Pathogenic',
                'criteria provided, multiple submitters, no conflicts',
                span=(31232000, 31234000),
            ),
        ],
        total=3,
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == '/hello/':
            return httpx2.Response(200, json=_VV_METADATA)
        if request.url.host == 'rest.variantvalidator.org':
            assert request.url.path.endswith(f'/{_NF1_TRANSCRIPT}/{_NF1_TRANSCRIPT}/refseq/GRCh38')
            return httpx2.Response(200, json=_NF1_STRUCTURE)
        return eutils(request)

    resp = _run(
        monkeypatch,
        handler,
        lambda stub: stub.SearchCodingSpan(
            clinvar_pb2.SearchCodingSpanRequest(
                transcript=_NF1_TRANSCRIPT, cds_start=3496, cds_end=3498, max_records=50
            )
        ),
    )
    assert resp.gene == 'NF1'
    assert (resp.searched_span.start, resp.searched_span.end) == (31232881, 31233003)
    assert {r.classification for r in resp.records} == {'Likely benign', 'Uncertain significance', 'Pathogenic'}
    assert list(resp.records_with_unparsed_hgvs) == ['VCV000000003']
    assert (resp.total_in_span, resp.considered_in_span, resp.span_truncated) == (3, 3, False)
    assert {p.source for p in resp.provenance} == {
        'VariantValidator gene2transcripts',
        'NCBI ClinVar (E-utilities)',
    }
    assert '31232881:31233003[Base Position]' in resp.esearch_term
    assert resp.variantvalidator_transcript['reference'] == _NF1_TRANSCRIPT
