"""Live `Transcript` RPCs over in-process gRPC and `httpx2.MockTransport`."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import pathlib
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping, Sequence

import grpc
import grpc.aio
import httpx2
import pytest

from themis.common import authored_status
from themis.rpc import transcript_pb2, transcript_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.tests import authz
from themis.services.evidence.transcript import interface
from themis.services.evidence.upstreams import clinvar as clinvar_upstream
from themis.services.evidence.upstreams import destinations

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_NF1_TRANSCRIPT = 'NM_001042492.3'
_NF1_STRUCTURE = json.loads((_FIXTURES / 'transcript_structure.json').read_bytes())

_ASSESSED = (100, 200)
_MUSCLE = 'Muscle_Skeletal'
_BRAIN = 'Brain_Cortex'
_VV_METADATA = {
    'metadata': {
        'variantvalidator_version': '4.0.1.dev7+gbdab9c72f',
        'vvdb_version': 'vvdb_2025_3',
        'vvta_version': 'vvta_2025_02',
    }
}


def _pext_region(start: int, stop: int, mean: float, tissues: Mapping[str, float] | None = None) -> dict[str, object]:
    active_tissues = tissues if tissues is not None else {'liver': mean}
    tissue_items = [{'tissue': name, 'value': val} for name, val in active_tissues.items()]
    return {'start': start, 'stop': stop, 'mean': mean, 'tissues': tissue_items}


_GENE_WIDE = _pext_region(100, 200, 0.83, {'muscle_skeletal': 0.94, 'brain_cortex': 0.81})
_EXCLUDED = _pext_region(300, 400, 0.03, {'muscle_skeletal': 0.59, 'brain_cortex': 0.0})
_GENE_WIDE_TAIL = _pext_region(500, 600, 0.83, {'muscle_skeletal': 0.94, 'brain_cortex': 0.81})


def _gnomad_gene_payload(
    symbol: str,
    *,
    loeuf: float | None = 0.25,
    mane: tuple[str, str] | None = None,
    pext_regions: Sequence[dict[str, object]] | None = (),
) -> dict[str, object]:
    mane_block = (
        {
            'refseq_id': mane[0].split('.')[0],
            'refseq_version': mane[0].split('.')[1],
            'ensembl_id': mane[1].split('.')[0],
            'ensembl_version': mane[1].split('.')[1],
        }
        if mane is not None
        else None
    )
    return {
        'data': {
            'gene': {
                'symbol': symbol,
                'gene_id': 'ENSG00000012048',
                'gnomad_constraint': {'oe_lof_upper': loeuf} if loeuf is not None else None,
                'mane_select_transcript': mane_block,
                'pext': {'regions': list(pext_regions)} if pext_regions is not None else None,
            }
        }
    }


def _transcript_record_json(
    accession: str,
    *exons: tuple[int, int],
    mane_select: bool = False,
    coding: bool = True,
    chromosome_accession: str = 'NC_000017.11',
) -> dict[str, object]:
    cursor = 1
    exon_entries: list[dict[str, object]] = []
    for index, (start, end) in enumerate(exons, start=1):
        length = end - start + 1
        exon_entries.append(
            {
                'exon_number': index,
                'genomic_start': start,
                'genomic_end': end,
                'transcript_start': cursor,
                'transcript_end': cursor + length - 1,
                'cigar': f'{length}=',
            }
        )
        cursor += length
    total_length = max(cursor - 1, 1)
    return {
        'reference': accession,
        'length': total_length,
        'coding_start': 1 if coding else None,
        'coding_end': total_length if coding else None,
        'annotations': {'mane_select': mane_select, 'mane_plus_clinical': False},
        'genomic_spans': {
            chromosome_accession: {
                'orientation': 1,
                'exon_structure': exon_entries,
            }
        },
    }


def _unreadable_record_json(accession: str) -> dict[str, object]:
    return {'reference': accession, 'genomic_spans': {}}


def _gtex_row(transcript: str, tissue: str, median: float) -> dict[str, object]:
    return {
        'transcriptId': transcript,
        'tissueSiteDetailId': tissue,
        'median': median,
        'gencodeId': 'ENSG00000012048.23',
        'datasetId': 'gtex_v10',
        'unit': 'TPM',
    }


def _clinvar_summary_item(uid: str, accession: str, title: str) -> dict[str, object]:
    return {
        'uid': uid,
        'accession': accession,
        'title': title,
        'germline_classification': {
            'description': 'Pathogenic',
            'review_status': 'criteria provided, multiple submitters, no conflicts',
            'trait_set': [{'trait_name': 'condition'}],
        },
        'variation_set': [],
    }


def _exon_relevance_handler(
    *,
    gene: str = 'BRCA1',
    transcript: str = 'NM_000000.1',
    structure_gene: str | None = None,
    inventory_gene: str | None = None,
    structure_exons: Sequence[tuple[int, int]] = (_ASSESSED,),
    refseq_records: Sequence[dict[str, object]] | None = None,
    ensembl_records: Sequence[dict[str, object]] | None = None,
    gnomad_payload: dict[str, object] | None = None,
    clinvar_items: Sequence[dict[str, object]] = (),
    clinvar_total: int = 0,
    gtex_rows: Sequence[dict[str, object]] | None = None,
    seen_gtex_tissues: list[list[str]] | None = None,
) -> Callable[[httpx2.Request], httpx2.Response]:
    s_gene = gene if structure_gene is None else structure_gene
    i_gene = gene if inventory_gene is None else inventory_gene
    structure_doc = [
        {
            'current_symbol': s_gene,
            'transcripts': [_transcript_record_json(transcript, *structure_exons, mane_select=True)],
        }
    ]
    refseq_doc = [
        {
            'current_symbol': i_gene,
            'transcripts': (
                list(refseq_records)
                if refseq_records is not None
                else [_transcript_record_json(transcript, _ASSESSED, mane_select=True)]
            ),
        }
    ]
    ensembl_doc = [
        {
            'current_symbol': i_gene,
            'transcripts': (
                list(ensembl_records)
                if ensembl_records is not None
                else [_transcript_record_json('ENST00000352993.7', _ASSESSED)]
            ),
        }
    ]
    gnomad_doc = (
        gnomad_payload
        if gnomad_payload is not None
        else _gnomad_gene_payload(gene, loeuf=0.25, mane=None, pext_regions=())
    )
    default_gtex = [_gtex_row('ENST00000352993.7', 'Liver', 0.67), _gtex_row('ENST00000352993.7', 'Lung', 0.02)]
    active_gtex = list(gtex_rows) if gtex_rows is not None else default_gtex
    uids = [str(item['uid']) for item in clinvar_items]
    by_uid = {str(item['uid']): item for item in clinvar_items}

    def handler(request: httpx2.Request) -> httpx2.Response:
        host = request.url.host
        path = urllib.parse.unquote(request.url.path)
        if host == 'gnomad.broadinstitute.org':
            body = json.loads(request.content)
            assert body['variables']['gene'] == gene
            return httpx2.Response(200, json=gnomad_doc)
        if host == 'eutils.ncbi.nlm.nih.gov':
            if path.endswith('/esearch.fcgi'):
                assert f'{gene}[gene]' in (request.url.params.get('term') or '')
                retstart = int(request.url.params.get('retstart') or '0')
                retmax = int(request.url.params.get('retmax') or '20')
                return httpx2.Response(
                    200,
                    json={
                        'esearchresult': {
                            'count': str(clinvar_total),
                            'idlist': uids[retstart : retstart + retmax],
                        }
                    },
                )
            assert path.endswith('/esummary.fcgi')
            form = urllib.parse.parse_qs(request.content.decode())
            requested_uids = [u for u in form.get('id', [''])[0].split(',') if u]
            return httpx2.Response(
                200,
                json={'result': {'uids': requested_uids, **{u: by_uid[u] for u in requested_uids}}},
            )
        if host == 'gtexportal.org':
            if path.endswith('/reference/gene'):
                assert request.url.params.get('geneId') == gene
                assert request.url.params.get('gencodeVersion') == 'v39'
                return httpx2.Response(
                    200,
                    json={
                        'data': [
                            {
                                'geneSymbol': gene,
                                'geneSymbolUpper': gene.upper(),
                                'gencodeId': 'ENSG00000012048.23',
                                'gencodeVersion': 'v39',
                            }
                        ],
                        'paging_info': {'numberOfPages': 1, 'page': 0},
                    },
                )
            assert path.endswith('/medianTranscriptExpression')
            assert request.url.params.get('gencodeId') == 'ENSG00000012048.23'
            requested_tissues = request.url.params.get_list('tissueSiteDetailId')
            if seen_gtex_tissues is not None:
                seen_gtex_tissues.append(requested_tissues)
            filtered = (
                [row for row in active_gtex if row['tissueSiteDetailId'] in requested_tissues]
                if requested_tissues
                else active_gtex
            )
            return httpx2.Response(200, json={'data': filtered, 'paging_info': {'numberOfPages': 1, 'page': 0}})
        if path == '/hello/':
            return httpx2.Response(200, json=_VV_METADATA)
        if path.endswith(f'/{transcript}/{transcript}/refseq/GRCh38'):
            return httpx2.Response(200, json=structure_doc)
        if path.endswith(f'/{gene}/all/refseq/GRCh38'):
            return httpx2.Response(200, json=refseq_doc)
        if path.endswith(f'/{gene}/all/ensembl/GRCh38'):
            return httpx2.Response(200, json=ensembl_doc)
        raise AssertionError(f'unexpected upstream request: {request.url}')

    return handler


def _run[T](
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx2.Request], httpx2.Response],
    call: Callable[[transcript_pb2_grpc.TranscriptAsyncStub], Awaitable[T]],
) -> T:
    monkeypatch.setenv('THEMIS_TRANSCRIPT_BACKEND', 'live')
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
                return await call(transcript_pb2_grpc.TranscriptStub(channel))

    return asyncio.run(run())


def _request(
    *,
    gene: str = 'BRCA1',
    transcript: str = 'NM_000000.1',
    exon: int = 1,
    in_mane_select: bool = True,
    in_mane_plus_clinical: bool = False,
    tissues: Sequence[str] = (),
    include_gtex_detail: bool = False,
) -> transcript_pb2.AssessExonRelevanceRequest:
    return transcript_pb2.AssessExonRelevanceRequest(
        gene=gene,
        transcript=transcript,
        exon=exon,
        in_mane_select=in_mane_select,
        in_mane_plus_clinical=in_mane_plus_clinical,
        tissues=tissues,
        include_gtex_detail=include_gtex_detail,
    )


def test_exon_relevance_composes_gnomad_clinvar_and_gtex(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_tissues: list[list[str]] = []
    handler = _exon_relevance_handler(
        gene='BRCA1',
        transcript='NM_007294.4',
        structure_exons=((150, 350),),
        refseq_records=[_transcript_record_json('NM_007294.4', (150, 350), mane_select=True)],
        gnomad_payload=_gnomad_gene_payload(
            'BRCA1',
            loeuf=0.25,
            mane=('NM_007294.4', 'ENST00000357654.9'),
            pext_regions=[_pext_region(100, 200, 0.9), _pext_region(300, 400, 0.4)],
        ),
        clinvar_items=[
            _clinvar_summary_item(str(i + 1), f'VCV00000000{i + 1}', f'NM_007294.4(BRCA1):c.{i + 1}G>A')
            for i in range(3)
        ],
        clinvar_total=9,
        seen_gtex_tissues=seen_tissues,
    )
    signals = _run(
        monkeypatch,
        handler,
        lambda stub: stub.AssessExonRelevance(_request(gene='BRCA1', transcript='NM_007294.4', exon=1)),
    )
    assert seen_tissues == [[]]
    assert signals.in_mane_select
    assert not signals.in_mane_plus_clinical
    assert signals.HasField('loeuf')
    assert signals.loeuf == 0.25
    assert signals.clinvar_plp_density == 3
    assert [p.source for p in signals.provenance] == [
        'gnomAD GraphQL',
        'NCBI ClinVar (E-utilities)',
        'GTEx',
        'VariantValidator gene2transcripts',
        'VariantValidator gene2transcripts',
        'VariantValidator gene2transcripts',
    ]
    assert 'BRCA1/all/refseq/GRCh38' in signals.provenance[-2].query
    assert 'BRCA1/all/ensembl/GRCh38' in signals.provenance[-1].query
    assert signals.pext_mane_select.refseq == 'NM_007294.4'
    assert signals.pext_mane_select.ensembl == 'ENST00000357654.9'
    assert [entry.exon for entry in signals.pext] == [1]
    assert signals.pext[0].HasField('mean')
    assert signals.pext[0].mean == pytest.approx((0.9 + 0.4) / 2)
    assert not signals.pext[0].tissues
    assert not signals.tissues_without_pext
    assert signals.raw['clinvar_plp_density_scope'] == 'gene'
    assert signals.raw['clinvar_total_in_gene'] == 9
    assert signals.raw['clinvar_considered_in_gene'] == 3
    assert signals.raw['clinvar_pool_truncated'] is True
    assert len(signals.raw.fields['pext_regions'].list_value.values) == 2
    assert [(e.transcript, e.tissue, e.median_tpm) for e in signals.gtex_expression] == [
        ('ENST00000352993.7', 'Liver', 0.67)
    ]


def _exon_relevance_over_pext(
    monkeypatch: pytest.MonkeyPatch, *, tissues: list[str]
) -> transcript_pb2.AssessExonRelevanceResponse:
    """Exon-relevance over a three-exon ANO5 transcript whose middle exon sits in the depressed region."""
    exons = ((100, 150), (300, 400), (500, 549))
    handler = _exon_relevance_handler(
        gene='ANO5',
        transcript='NM_173596.5',
        structure_exons=exons,
        refseq_records=[_transcript_record_json('NM_173596.5', *exons, mane_select=True)],
        ensembl_records=[_transcript_record_json('ENST00000324559.8', *exons)],
        gnomad_payload=_gnomad_gene_payload(
            'ANO5',
            loeuf=0.25,
            mane=('NM_173596.5', 'ENST00000324559.8'),
            pext_regions=[_GENE_WIDE, _EXCLUDED, _GENE_WIDE_TAIL],
        ),
    )
    return _run(
        monkeypatch,
        handler,
        lambda stub: stub.AssessExonRelevance(_request(gene='ANO5', transcript='NM_173596.5', exon=2, tissues=tissues)),
    )


def test_exon_relevance_returns_the_whole_gene_pext_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    signals = _exon_relevance_over_pext(monkeypatch, tissues=[])

    profile = {entry.exon: entry.mean for entry in signals.pext}
    assert profile.keys() == {1, 2, 3}
    assert profile[2] < profile[1] == profile[3]


def test_exon_relevance_reports_pext_per_requested_tissue(monkeypatch: pytest.MonkeyPatch) -> None:
    signals = _exon_relevance_over_pext(monkeypatch, tissues=[_MUSCLE, _BRAIN])

    by_exon = {entry.exon: {value.tissue: value.value for value in entry.tissues} for entry in signals.pext}
    queried, elsewhere = by_exon[2], by_exon[1]
    assert signals.pext[1].mean / signals.pext[0].mean < 0.1
    assert queried[_MUSCLE] / elsewhere[_MUSCLE] > 0.5
    assert queried[_BRAIN] < elsewhere[_BRAIN]
    assert not signals.tissues_without_pext


def test_exon_relevance_names_a_requested_tissue_gnomad_has_no_pext_for(monkeypatch: pytest.MonkeyPatch) -> None:
    signals = _exon_relevance_over_pext(monkeypatch, tissues=[_MUSCLE, 'Bladder'])

    assert [value.tissue for value in signals.pext[1].tissues] == [_MUSCLE]
    assert list(signals.tissues_without_pext) == ['Bladder']


def test_exon_relevance_says_how_much_of_each_exon_pext_covers(monkeypatch: pytest.MonkeyPatch) -> None:
    signals = _exon_relevance_over_pext(monkeypatch, tissues=[])

    covered = {entry.exon: (entry.covered_bases, entry.exon_bases) for entry in signals.pext}
    assert covered[1] == (51, 51)
    assert covered[2] == (101, 101)


def test_an_exon_no_region_covers_has_no_pext_value(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _exon_relevance_handler(
        gene='ANO5',
        transcript='NM_173596.5',
        structure_exons=((9000, 9100),),
        refseq_records=[_transcript_record_json('NM_173596.5', (9000, 9100), mane_select=True)],
        gnomad_payload=_gnomad_gene_payload('ANO5', pext_regions=[_GENE_WIDE]),
    )
    signals = _run(
        monkeypatch,
        handler,
        lambda stub: stub.AssessExonRelevance(_request(gene='ANO5', transcript='NM_173596.5', exon=1)),
    )
    assert [entry.exon for entry in signals.pext] == [1]
    assert not signals.pext[0].HasField('mean')
    assert signals.pext[0].covered_bases == 0
    assert signals.pext[0].exon_bases == 101


def test_a_gene_without_pext_reports_no_missing_tissue(monkeypatch: pytest.MonkeyPatch) -> None:
    signals = _run(
        monkeypatch,
        _exon_relevance_handler(gene='BRCA1', transcript='NM_007294.4'),
        lambda stub: stub.AssessExonRelevance(
            _request(gene='BRCA1', transcript='NM_007294.4', exon=1, tissues=[_MUSCLE, 'Bladder'])
        ),
    )
    assert not signals.pext[0].HasField('mean')
    assert not signals.tissues_without_pext


def test_exon_relevance_rejects_an_exon_the_transcript_does_not_have(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        _run(
            monkeypatch,
            _exon_relevance_handler(gene='BRCA1', transcript='NM_007294.4'),
            lambda stub: stub.AssessExonRelevance(_request(gene='BRCA1', transcript='NM_007294.4', exon=99)),
        )
    assert caught.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert authored_status.authored(caught.value)
    assert 'asked for exon 99' in (caught.value.details() or '')


def test_exon_relevance_withholds_the_gtex_grid_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    signals = _run(
        monkeypatch,
        _exon_relevance_handler(gene='BRCA1', transcript='NM_007294.4'),
        lambda stub: stub.AssessExonRelevance(_request(gene='BRCA1', transcript='NM_007294.4', exon=1)),
    )
    isoforms = signals.raw.fields['gtex_isoforms'].struct_value
    assert 'rows' not in isoforms.fields
    assert isoforms.fields['rows_withheld'].number_value == 2
    assert [e.tissue for e in signals.gtex_expression] == ['Liver']


def test_exon_relevance_passes_the_tissue_filter_and_serves_the_grid_on_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_tissues: list[list[str]] = []
    signals = _run(
        monkeypatch,
        _exon_relevance_handler(
            gene='BRCA1',
            transcript='NM_007294.4',
            seen_gtex_tissues=seen_tissues,
        ),
        lambda stub: stub.AssessExonRelevance(
            _request(
                gene='BRCA1',
                transcript='NM_007294.4',
                exon=1,
                tissues=['Liver', 'Nerve_Tibial'],
                include_gtex_detail=True,
            )
        ),
    )
    assert seen_tissues == [['Liver', 'Nerve_Tibial']]
    isoforms = signals.raw.fields['gtex_isoforms'].struct_value
    assert len(isoforms.fields['rows'].list_value.values) == 1
    assert list(signals.tissues_without_expression) == ['Nerve_Tibial']
    empty = [value.string_value for value in isoforms.fields['tissues_without_rows'].list_value.values]
    assert empty == ['Nerve_Tibial']


def _inventory(
    monkeypatch: pytest.MonkeyPatch,
    refseq: Sequence[dict[str, object]],
    ensembl: Sequence[dict[str, object]],
    *,
    gtex_rows: Sequence[dict[str, object]] | None = None,
) -> transcript_pb2.AssessExonRelevanceResponse:
    """Exon-relevance over an inventory the test states, against exon 1 at `_ASSESSED`."""
    handler = _exon_relevance_handler(
        gene='BRCA1',
        transcript='NM_000000.1',
        structure_exons=(_ASSESSED,),
        refseq_records=refseq,
        ensembl_records=ensembl,
        gtex_rows=gtex_rows,
    )
    return _run(
        monkeypatch,
        handler,
        lambda stub: stub.AssessExonRelevance(_request(gene='BRCA1', transcript='NM_000000.1', exon=1)),
    )


def _by_membership(
    signals: transcript_pb2.AssessExonRelevanceResponse,
) -> dict[transcript_pb2.ExonMembership, list[str]]:
    grouped: dict[transcript_pb2.ExonMembership, list[str]] = {}
    for entry in signals.transcript_inventory:
        grouped.setdefault(entry.membership, []).append(entry.transcript)
    return grouped


def test_an_inventory_with_no_omitting_transcript_states_its_own_denominator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _inventory(
        monkeypatch,
        [
            _transcript_record_json('NM_000000.1', (50, 60), _ASSESSED, (400, 500), mane_select=True),
            _transcript_record_json('NM_000001.1', _ASSESSED, (400, 500)),
            _transcript_record_json('NM_000002.2', (50, 60), _ASSESSED),
        ],
        [_transcript_record_json('ENST00000352993.7', (50, 60), _ASSESSED, (400, 500))],
    )

    assert set(_by_membership(signals)) == {transcript_pb2.EXON_MEMBERSHIP_CARRIES_THE_EXON}
    assert [(d.namespace, d.transcripts_considered) for d in signals.inventory_denominators] == [
        (transcript_pb2.TRANSCRIPT_NAMESPACE_REFSEQ, 3),
        (transcript_pb2.TRANSCRIPT_NAMESPACE_ENSEMBL, 1),
    ]
    assert {e.transcript: e.namespace for e in signals.transcript_inventory} == {
        'NM_000000.1': transcript_pb2.TRANSCRIPT_NAMESPACE_REFSEQ,
        'NM_000001.1': transcript_pb2.TRANSCRIPT_NAMESPACE_REFSEQ,
        'NM_000002.2': transcript_pb2.TRANSCRIPT_NAMESPACE_REFSEQ,
        'ENST00000352993.7': transcript_pb2.TRANSCRIPT_NAMESPACE_ENSEMBL,
    }
    assessed = [entry.transcript for entry in signals.transcript_inventory if entry.assessed_transcript]
    assert assessed == ['NM_000000.1']


def test_the_three_ways_of_lacking_the_exon_stay_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    signals = _inventory(
        monkeypatch,
        [
            _transcript_record_json('NM_000000.1', (50, 60), _ASSESSED, (400, 500), mane_select=True),
            _transcript_record_json('NM_000001.1', (50, 60), (400, 500)),
            _transcript_record_json('NM_000002.2', (50, 60), (150, 200), (400, 500)),
            _transcript_record_json('NM_000003.3', (50, 60), (80, 260)),
            _transcript_record_json('NM_000004.4', (90, 140), (160, 210)),
            _transcript_record_json('NM_000005.5', (50, 60), (70, 80)),
            _transcript_record_json('NM_000006.6', (400, 500), (600, 700)),
        ],
        [_transcript_record_json('ENST00000352993.7', _ASSESSED)],
    )

    assert _by_membership(signals) == {
        transcript_pb2.EXON_MEMBERSHIP_CARRIES_THE_EXON: ['NM_000000.1', 'ENST00000352993.7'],
        transcript_pb2.EXON_MEMBERSHIP_SPANS_BUT_SKIPS: ['NM_000001.1'],
        transcript_pb2.EXON_MEMBERSHIP_CARRIES_A_DIFFERENT_INTERVAL: ['NM_000002.2', 'NM_000003.3', 'NM_000004.4'],
        transcript_pb2.EXON_MEMBERSHIP_DOES_NOT_REACH: ['NM_000005.5', 'NM_000006.6'],
    }


def test_a_different_interval_reports_how_much_of_the_exon_the_transcript_keeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _inventory(
        monkeypatch,
        [
            _transcript_record_json('NM_000000.1', _ASSESSED, mane_select=True),
            _transcript_record_json('NM_000001.1', (103, 200)),
            _transcript_record_json('NM_000002.2', (90, 100)),
            _transcript_record_json('NM_000003.3', (90, 140), (160, 210)),
        ],
        [],
    )

    overlaps = {e.transcript: [(s.start, s.end) for s in e.overlapping_exons] for e in signals.transcript_inventory}
    assert overlaps['NM_000001.1'] == [(103, 200)]
    assert overlaps['NM_000002.2'] == [(90, 100)]
    assert overlaps['NM_000003.3'] == [(90, 140), (160, 210)]
    assert overlaps['NM_000000.1'] == []


def test_no_mane_pair_is_an_absence_not_an_empty_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    signals = _inventory(monkeypatch, [_transcript_record_json('NM_000000.1', _ASSESSED, mane_select=True)], [])

    assert not signals.HasField('pext_mane_select')


def test_expression_joins_a_transcript_the_two_releases_version_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _inventory(
        monkeypatch,
        [_transcript_record_json('NM_000000.1', _ASSESSED, mane_select=True)],
        [_transcript_record_json('ENST00000352993.9', _ASSESSED)],
        gtex_rows=[_gtex_row('ENST00000352993.7', 'Liver', 0.67)],
    )

    joined = next(e for e in signals.transcript_inventory if e.accession_base == 'ENST00000352993')
    assert joined.transcript == 'ENST00000352993.9'
    assert [(row.transcript, row.tissue, row.median_tpm) for row in joined.expression] == [
        ('ENST00000352993.7', 'Liver', 0.67)
    ]
    assert [row.transcript_base for row in joined.expression] == ['ENST00000352993']
    assert not signals.transcripts_without_structure
    assert not signals.transcripts_without_expression


def test_one_measurement_is_not_reported_against_two_versions_of_its_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _inventory(
        monkeypatch,
        [_transcript_record_json('NM_000000.1', _ASSESSED, mane_select=True)],
        [
            _transcript_record_json('ENST00000352993.7', _ASSESSED),
            _transcript_record_json('ENST00000352993.9', (400, 500)),
        ],
        gtex_rows=[_gtex_row('ENST00000352993.7', 'Liver', 0.67)],
    )

    carrying = {e.transcript: [row.median_tpm for row in e.expression] for e in signals.transcript_inventory}
    assert carrying['ENST00000352993.7'] == [0.67]
    assert carrying['ENST00000352993.9'] == []
    assert list(signals.transcripts_without_expression) == ['ENST00000352993.9']


def test_a_transcript_only_one_side_holds_is_named_on_the_side_that_lacks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _inventory(
        monkeypatch,
        [_transcript_record_json('NM_000000.1', _ASSESSED, mane_select=True)],
        [
            _transcript_record_json('ENST00000352993.7', _ASSESSED),
            _transcript_record_json('ENST00000999999.1', _ASSESSED),
        ],
    )

    assert list(signals.transcripts_without_expression) == ['ENST00000999999.1']
    assert not signals.transcripts_without_structure


def test_expression_over_a_transcript_no_entry_covers_stands_against_no_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _inventory(
        monkeypatch,
        [_transcript_record_json('NM_000000.1', _ASSESSED, mane_select=True)],
        [],
        gtex_rows=[_gtex_row('ENST00000683148.1', 'Liver', 4.2)],
    )

    assert list(signals.transcripts_without_structure) == ['ENST00000683148.1']
    assert [e.transcript for e in signals.transcript_inventory] == ['NM_000000.1']
    assert [e.transcript for e in signals.gtex_expression] == ['ENST00000683148.1']


def test_a_transcript_whose_record_was_not_classified_is_not_reported_as_unheld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _inventory(
        monkeypatch,
        [_transcript_record_json('NM_000000.1', _ASSESSED, mane_select=True)],
        [_transcript_record_json('ENST00000352993.7', _ASSESSED, chromosome_accession='NC_000001.11')],
        gtex_rows=[_gtex_row('ENST00000352993.7', 'Liver', 0.67)],
    )

    assert list(signals.transcripts_without_structure) == ['ENST00000352993.7']
    ensembl = signals.inventory_denominators[1]
    assert ensembl.transcripts_considered == 0
    assert list(ensembl.transcripts_not_classified) == ['ENST00000352993.7']


def test_a_record_with_no_comparable_alignment_gets_no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    signals = _inventory(
        monkeypatch,
        [
            _transcript_record_json('NM_000000.1', _ASSESSED, mane_select=True),
            _transcript_record_json('NM_000004.4', _ASSESSED, chromosome_accession='NC_000001.11'),
            _unreadable_record_json('NR_000005.1'),
        ],
        [_transcript_record_json('ENST00000352993.7', _ASSESSED)],
    )

    assert [e.transcript for e in signals.transcript_inventory] == ['NM_000000.1', 'ENST00000352993.7']
    refseq = signals.inventory_denominators[0]
    assert refseq.transcripts_considered == 1
    assert set(refseq.transcripts_not_classified) == {'NR_000005.1', 'NM_000004.4'}


def test_a_gene_and_transcript_from_different_genes_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _exon_relevance_handler(
        gene='OTHER',
        transcript='NM_007294.4',
        structure_gene='BRCA1',
        inventory_gene='OTHER',
        refseq_records=[],
        ensembl_records=[],
    )
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        _run(
            monkeypatch,
            handler,
            lambda stub: stub.AssessExonRelevance(_request(gene='OTHER', transcript='NM_007294.4', exon=1)),
        )
    assert caught.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert authored_status.authored(caught.value)
    assert 'OTHER' in (caught.value.details() or '')
    assert 'BRCA1' in (caught.value.details() or '')


def test_a_non_coding_model_is_reported_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    signals = _inventory(
        monkeypatch,
        [
            _transcript_record_json('NM_000000.1', _ASSESSED, mane_select=True),
            _transcript_record_json('NR_000006.1', _ASSESSED, coding=False),
        ],
        [],
    )

    assert {e.transcript: e.coding for e in signals.transcript_inventory} == {'NM_000000.1': True, 'NR_000006.1': False}
    assert {e.transcript: e.mane_select for e in signals.transcript_inventory} == {
        'NM_000000.1': True,
        'NR_000006.1': False,
    }


def test_an_assessed_transcript_the_set_has_superseded_still_matches_on_its_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _inventory(
        monkeypatch,
        [_transcript_record_json('NM_000000.4', _ASSESSED, mane_select=True)],
        [],
    )

    assessed = [e for e in signals.transcript_inventory if e.assessed_transcript]
    assert [e.transcript for e in assessed] == ['NM_000000.4']


def _nf1_structure_handler(request: httpx2.Request) -> httpx2.Response:
    if request.url.path == '/hello/':
        return httpx2.Response(200, json=_VV_METADATA)
    assert request.url.path.endswith(f'/{_NF1_TRANSCRIPT}/{_NF1_TRANSCRIPT}/refseq/GRCh38')
    return httpx2.Response(200, json=_NF1_STRUCTURE)


def test_get_structure_locates_the_requested_position(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _run(
        monkeypatch,
        _nf1_structure_handler,
        lambda stub: stub.GetStructure(
            transcript_pb2.GetStructureRequest(transcript=_NF1_TRANSCRIPT, genome_build='GRCh38', cds_position=3496)
        ),
    )
    assert resp.gene == 'NF1'
    assert resp.mane_select
    assert resp.chromosome_accession == 'NC_000017.11'
    assert len(resp.exons) == 58
    assert resp.position.exon == 26
    assert resp.position.nt_to_exon_end == 1
    assert resp.provenance[0].source == 'VariantValidator gene2transcripts'
    assert resp.provenance[0].retrieved_at.seconds > 0


def test_get_structure_omits_the_position_when_none_was_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _run(
        monkeypatch,
        _nf1_structure_handler,
        lambda stub: stub.GetStructure(
            transcript_pb2.GetStructureRequest(transcript=_NF1_TRANSCRIPT, genome_build='GRCh38')
        ),
    )
    assert not resp.HasField('position')
