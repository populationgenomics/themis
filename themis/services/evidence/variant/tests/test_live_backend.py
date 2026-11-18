"""Live `Variant.Normalize` over in-process gRPC and `httpx2` mock transports."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import pathlib
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import override

import grpc
import grpc.aio
import httpx2
import pytest

from themis.common import authored_status
from themis.evidence.models import evidence_pb2
from themis.rpc import variant_pb2, variant_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence import errors
from themis.services.evidence.tests import authz
from themis.services.evidence.upstreams import destinations
from themis.services.evidence.variant import interface

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_ALLELE_FIXTURE = json.loads((_FIXTURES / 'allele_registry.json').read_bytes())
_VALIDATOR_FIXTURE = json.loads((_FIXTURES / 'variant_validator.json').read_bytes())
_VEP_FIXTURE = json.loads((_FIXTURES / 'vep.json').read_bytes())

_TP53_VARIANT = 'NM_000546.6:c.524G>A'
_VV_METADATA = {'variantvalidator_version': '4.0.1.dev7+gbdab9c72f', 'vvdb_version': 'vvdb_2025_3'}


class _AsyncTransport(httpx2.AsyncBaseTransport):
    def __init__(self, handler: Callable[[httpx2.Request], Awaitable[httpx2.Response]]) -> None:
        self._handler = handler

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self._handler(request)


def _run[T](
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx2.AsyncBaseTransport,
    call: Callable[[variant_pb2_grpc.VariantAsyncStub], Awaitable[T]],
) -> T:
    monkeypatch.setenv('THEMIS_VARIANT_BACKEND', 'live')

    async def run() -> T:
        async with (
            contextlib.AsyncExitStack() as stack,
            destinations.admitting_client(timeout=httpx2.Timeout(10.0), transport=transport) as http_client,
        ):
            deps = deps_mod.Deps(authorizer=authz.authorizer(), http_client=http_client, stack=stack)
            async with authz.gated(functools.partial(interface.register, deps=deps)) as channel:
                return await call(variant_pb2_grpc.VariantStub(channel))

    return asyncio.run(run())


def _validator_payload(
    hgvs_c: str,
    *,
    gene: str,
    hgvs_p: str = '',
    mane_select: bool = True,
    grch38_vcf: tuple[str, str, str, str] | None = None,
    grch37_vcf: tuple[str, str, str, str] | None = None,
) -> dict[str, object]:
    loci: dict[str, object] = {}
    if grch38_vcf is not None:
        chrom, pos, ref, alt = grch38_vcf
        loci['grch38'] = {'vcf': {'chr': chrom, 'pos': pos, 'ref': ref, 'alt': alt}}
    if grch37_vcf is not None:
        chrom, pos, ref, alt = grch37_vcf
        loci['grch37'] = {'vcf': {'chr': chrom, 'pos': pos, 'ref': ref, 'alt': alt}}
    return {
        'flag': 'gene_variant',
        'metadata': _VV_METADATA,
        hgvs_c: {
            'hgvs_transcript_variant': hgvs_c,
            'gene_symbol': gene,
            'hgvs_predicted_protein_consequence': {'tlr': hgvs_p},
            'selected_assembly': 'GRCh38',
            'annotations': {'mane_select': mane_select, 'mane_plus_clinical': False},
            'primary_assembly_loci': loci,
        },
    }


def _vep_payload(
    *,
    consequence: str = 'missense_variant',
    gene_symbol: str = 'TP53',
    hgnc_id: str = 'HGNC:11998',
    transcript_id: str = 'ENST00000269305.9',
) -> list[dict[str, object]]:
    consequence_entry: dict[str, object] = {
        'transcript_id': transcript_id,
        'consequence_terms': [consequence],
        'canonical': 1,
    }
    if gene_symbol:
        consequence_entry['gene_symbol'] = gene_symbol
    if hgnc_id:
        consequence_entry['hgnc_id'] = hgnc_id
    return [{'most_severe_consequence': consequence, 'transcript_consequences': [consequence_entry]}]


def test_normalize_composes_allele_validator_and_vep(monkeypatch: pytest.MonkeyPatch) -> None:
    tp53_vep = _vep_payload(consequence='missense_variant', gene_symbol='TP53', hgnc_id='HGNC:11998')

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == 'reg.clinicalgenome.org':
            assert request.url.params.get('hgvs') == _TP53_VARIANT
            return httpx2.Response(200, json=_ALLELE_FIXTURE)
        if request.url.host == 'rest.variantvalidator.org':
            decoded = urllib.parse.unquote(request.url.path)
            assert f'/GRCh38/{_TP53_VARIANT}/mane' in decoded
            return httpx2.Response(200, json=_VALIDATOR_FIXTURE)
        if request.url.path == '/info/software':
            return httpx2.Response(200, json={'release': 116})
        decoded = urllib.parse.unquote(request.url.path)
        assert decoded.endswith(f'/vep/human/hgvs/{_TP53_VARIANT}')
        return httpx2.Response(200, json=tp53_vep)

    normalized = _run(
        monkeypatch,
        httpx2.MockTransport(handler),
        lambda stub: stub.Normalize(variant_pb2.NormalizeRequest(variant=_TP53_VARIANT, genome_build='GRCh38')),
    )
    assert normalized.caid == 'CA000251'
    assert normalized.gnomad_v4_id == '17-7675088-C-T'
    assert normalized.gnomad_v2_id == '17-7578406-C-T'
    assert normalized.consequence == evidence_pb2.CONSEQUENCE_MISSENSE
    assert normalized.gene_symbol == 'TP53'
    assert normalized.hgnc_id == 'HGNC:11998'
    assert [p.source for p in normalized.provenance] == [
        'ClinGen Allele Registry',
        'VariantValidator',
        'Ensembl VEP REST',
    ]
    assert set(normalized.raw.keys()) == {'allele_registry', 'variant_validator', 'vep'}
    assert [v.vcv for v in normalized.clinvar_variations] == ['VCV000012374']
    assert 'RCV000013173' in normalized.clinvar_variations[0].rcv
    assert [(a.allele_id, a.preferred_name) for a in normalized.clinvar_alleles] == [
        (27413, 'NM_000546.6(TP53):c.524G>A (p.Arg175His)')
    ]


def test_normalize_carries_every_transcript_the_allele_projects_onto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither source alone is the projection set: the field is the union deduped on accession base."""
    variant = 'NM_007294.4:c.68A>G'
    mane_block = {
        'maneStatus': 'MANE Select',
        'nucleotide': {
            'Ensembl': {'hgvs': 'ENST00000357654.9:c.68A>G'},
            'RefSeq': {'hgvs': variant},
        },
    }
    registry_payload = {
        '@id': 'http://reg.genome.network/allele/CA123',
        'externalRecords': {'gnomAD_4': [{'id': '17-43093464-A-G'}]},
        'transcriptAlleles': [
            {'geneSymbol': 'BRCA1', 'hgvs': [variant], 'MANE': mane_block},
            {'geneSymbol': 'BRCA1', 'hgvs': ['ENST00000357654.9:c.68A>G'], 'MANE': mane_block},
            {'geneSymbol': 'BRCA1', 'hgvs': ['NM_007300.4:c.68A>G']},
        ],
    }
    vv_payload = _validator_payload(variant, gene='BRCA1', hgvs_p='NP_009225.1:p.(Glu23Gly)', mane_select=True)
    brca1_vep = _vep_payload(consequence='missense_variant', gene_symbol='BRCA1', hgnc_id='HGNC:1100')

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == 'reg.clinicalgenome.org':
            assert request.url.params.get('hgvs') == variant
            return httpx2.Response(200, json=registry_payload)
        if request.url.host == 'rest.variantvalidator.org':
            decoded = urllib.parse.unquote(request.url.path)
            assert f'/GRCh38/{variant}/mane' in decoded
            return httpx2.Response(200, json=vv_payload)
        if request.url.path == '/info/software':
            return httpx2.Response(200, json={'release': 116})
        decoded = urllib.parse.unquote(request.url.path)
        assert decoded.endswith(f'/vep/human/hgvs/{variant}')
        return httpx2.Response(200, json=brca1_vep)

    normalized = _run(
        monkeypatch,
        httpx2.MockTransport(handler),
        lambda stub: stub.Normalize(variant_pb2.NormalizeRequest(variant=variant, genome_build='GRCh38')),
    )
    assert [t.transcript for t in normalized.transcripts] == ['NM_007294.4', 'ENST00000357654.9', 'NM_007300.4']
    projections = {t.transcript: t for t in normalized.transcripts}
    assert list(projections['NM_007294.4'].sources) == ['VariantValidator', 'ClinGen Allele Registry']
    assert projections['NM_007294.4'].hgvs_p == 'NP_009225.1:p.(Glu23Gly)'
    assert list(projections['ENST00000357654.9'].sources) == ['ClinGen Allele Registry']


def test_normalize_falls_back_to_registry_symbol_when_vep_lacks_hgnc_id(monkeypatch: pytest.MonkeyPatch) -> None:
    variant = 'NM_000546.6:c.916C>T'
    registry_payload = {
        '@id': 'http://reg.genome.network/allele/CA9',
        'transcriptAlleles': [{'geneSymbol': 'TP53', 'hgvs': [variant]}],
    }
    vv_payload = _validator_payload(
        variant,
        gene='TP53',
        grch38_vcf=('chr17', '7676154', 'G', 'C'),
        grch37_vcf=('chr17', '7579472', 'G', 'C'),
    )
    identityless_vep = _vep_payload(consequence='stop_gained', gene_symbol='', hgnc_id='')

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == 'reg.clinicalgenome.org':
            assert request.url.params.get('hgvs') == variant
            return httpx2.Response(200, json=registry_payload)
        if request.url.host == 'rest.variantvalidator.org':
            decoded = urllib.parse.unquote(request.url.path)
            assert f'/GRCh38/{variant}/mane' in decoded
            return httpx2.Response(200, json=vv_payload)
        if request.url.path == '/info/software':
            return httpx2.Response(200, json={'release': 116})
        decoded = urllib.parse.unquote(request.url.path)
        assert decoded.endswith(f'/vep/human/hgvs/{variant}')
        return httpx2.Response(200, json=identityless_vep)

    normalized = _run(
        monkeypatch,
        httpx2.MockTransport(handler),
        lambda stub: stub.Normalize(variant_pb2.NormalizeRequest(variant=variant, genome_build='GRCh38')),
    )
    assert normalized.gnomad_v4_id == '17-7676154-G-C'
    assert normalized.gnomad_v2_id == '17-7579472-G-C'
    assert normalized.gene_symbol == 'TP53'
    assert normalized.hgnc_id == ''
    assert 'note' in normalized.raw


def test_normalize_canonicalises_onto_the_refseq_mane_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    """The HGVS handed downstream must be RefSeq: VariantValidator rejects Ensembl accessions.

    The registry lists a MANE Select transcript once per accession namespace with the Ensembl entry
    first, so a canonical taken from the head of the projection list fails VariantValidator.
    """
    non_mane_input = 'NM_000267.3:c.3496G>C'
    canonical_refseq = 'NM_001042492.3:c.3496G>C'
    ensembl_mane = 'ENST00000358273.9:c.3496G>C'
    mane_block = {
        'maneStatus': 'MANE Select',
        'nucleotide': {
            'Ensembl': {'hgvs': ensembl_mane},
            'RefSeq': {'hgvs': canonical_refseq},
        },
    }
    registry_payload = {
        '@id': 'http://reg.genome.network/allele/CA398989536',
        'transcriptAlleles': [
            {'geneSymbol': 'NF1', 'hgvs': [ensembl_mane], 'MANE': mane_block},
            {'geneSymbol': 'NF1', 'hgvs': [canonical_refseq], 'MANE': mane_block},
            {'geneSymbol': 'NF1', 'hgvs': [non_mane_input]},
        ],
    }
    vv_payload = _validator_payload(canonical_refseq, gene='NF1', grch38_vcf=('chr17', '31232881', 'G', 'C'))

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == 'reg.clinicalgenome.org':
            assert request.url.params.get('hgvs') == non_mane_input
            return httpx2.Response(200, json=registry_payload)
        if request.url.host == 'rest.variantvalidator.org':
            decoded = urllib.parse.unquote(request.url.path)
            if canonical_refseq not in decoded:
                return httpx2.Response(
                    200,
                    json={
                        'flag': 'warning',
                        'metadata': {},
                        'validation_warning_1': {
                            'hgvs_transcript_variant': '',
                            'validation_warnings': ['InvalidFieldError: Ensembl transcript is not in RefSeq'],
                        },
                    },
                )
            return httpx2.Response(200, json=vv_payload)
        if request.url.path == '/info/software':
            return httpx2.Response(200, json={'release': 116})
        decoded = urllib.parse.unquote(request.url.path)
        assert decoded.endswith(f'/vep/human/hgvs/{canonical_refseq}'), decoded
        return httpx2.Response(200, json=_VEP_FIXTURE)

    normalized = _run(
        monkeypatch,
        httpx2.MockTransport(handler),
        lambda stub: stub.Normalize(variant_pb2.NormalizeRequest(variant=non_mane_input, genome_build='GRCh38')),
    )
    assert normalized.caid == 'CA398989536'
    assert normalized.gene_symbol == 'NF1'
    assert normalized.hgnc_id == 'HGNC:7765'
    assert canonical_refseq in urllib.parse.unquote(normalized.provenance[1].query)
    assert canonical_refseq in urllib.parse.unquote(normalized.provenance[2].query)


def test_normalize_runs_its_two_independent_legs_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both downstream legs reach an `asyncio.Barrier(2)`; sequential execution deadlocks."""
    barrier: asyncio.Barrier | None = None
    tp53_vep = _vep_payload(consequence='missense_variant', gene_symbol='TP53', hgnc_id='HGNC:11998')

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal barrier
        if barrier is None:
            barrier = asyncio.Barrier(2)
        if request.url.host == 'reg.clinicalgenome.org':
            assert request.url.params.get('hgvs') == _TP53_VARIANT
            return httpx2.Response(200, json=_ALLELE_FIXTURE)
        if request.url.path == '/info/software':
            return httpx2.Response(200, json={'release': 116})
        async with asyncio.timeout(5):
            await barrier.wait()
        decoded = urllib.parse.unquote(request.url.path)
        if request.url.host == 'rest.variantvalidator.org':
            assert f'/GRCh38/{_TP53_VARIANT}/mane' in decoded
            return httpx2.Response(200, json=_VALIDATOR_FIXTURE)
        assert decoded.endswith(f'/vep/human/hgvs/{_TP53_VARIANT}')
        return httpx2.Response(200, json=tp53_vep)

    normalized = _run(
        monkeypatch,
        _AsyncTransport(handler),
        lambda stub: stub.Normalize(variant_pb2.NormalizeRequest(variant=_TP53_VARIANT, genome_build='GRCh38')),
    )
    assert normalized.caid == 'CA000251'


def test_a_failing_leg_keeps_its_own_status_rather_than_a_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """A BaseExceptionGroup would reach the servicer as UNKNOWN; the sibling leg is cancelled."""
    validator_started: asyncio.Event | None = None
    validator_cancelled = False

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal validator_started, validator_cancelled
        if validator_started is None:
            validator_started = asyncio.Event()
        if request.url.host == 'reg.clinicalgenome.org':
            assert request.url.params.get('hgvs') == _TP53_VARIANT
            return httpx2.Response(200, json=_ALLELE_FIXTURE)
        if request.url.host == 'rest.variantvalidator.org':
            decoded = urllib.parse.unquote(request.url.path)
            assert f'/GRCh38/{_TP53_VARIANT}/mane' in decoded
            validator_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                validator_cancelled = True
                raise
            raise AssertionError('unreachable')
        if request.url.path == '/info/software':
            return httpx2.Response(200, json={'release': 116})
        decoded = urllib.parse.unquote(request.url.path)
        assert decoded.endswith(f'/vep/human/hgvs/{_TP53_VARIANT}')
        async with asyncio.timeout(5):
            await validator_started.wait()
        return httpx2.Response(400, json={'error': 'VEP refused the expression'})

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        _run(
            monkeypatch,
            _AsyncTransport(handler),
            lambda stub: stub.Normalize(variant_pb2.NormalizeRequest(variant=_TP53_VARIANT, genome_build='GRCh38')),
        )
    assert caught.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert authored_status.authored(caught.value)
    assert 'VEP refused the expression' in (caught.value.details() or '')
    assert validator_cancelled


def test_when_both_legs_fail_neither_error_is_lost(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    validator_started: asyncio.Event | None = None

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal validator_started
        if validator_started is None:
            validator_started = asyncio.Event()
        if request.url.host == 'reg.clinicalgenome.org':
            assert request.url.params.get('hgvs') == _TP53_VARIANT
            return httpx2.Response(200, json=_ALLELE_FIXTURE)
        if request.url.host == 'rest.variantvalidator.org':
            decoded = urllib.parse.unquote(request.url.path)
            assert f'/GRCh38/{_TP53_VARIANT}/mane' in decoded
            validator_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise httpx2.ConnectError('connection reset by peer', request=request) from None
            raise AssertionError('unreachable')
        if request.url.path == '/info/software':
            return httpx2.Response(200, json={'release': 116})
        decoded = urllib.parse.unquote(request.url.path)
        assert decoded.endswith(f'/vep/human/hgvs/{_TP53_VARIANT}')
        async with asyncio.timeout(5):
            await validator_started.wait()
        return httpx2.Response(503, text='VEP unavailable')

    with caplog.at_level(logging.WARNING), pytest.raises(grpc.aio.AioRpcError) as caught:
        _run(
            monkeypatch,
            _AsyncTransport(handler),
            lambda stub: stub.Normalize(variant_pb2.NormalizeRequest(variant=_TP53_VARIANT, genome_build='GRCh38')),
        )
    assert caught.value.code() is grpc.StatusCode.UNAVAILABLE
    assert authored_status.authored(caught.value)
    logged_exc = next(r.exc_info[1] for r in caplog.records if r.exc_info and r.exc_info[1] is not None)
    upstreams = {
        errors.upstream_of(e)
        for e in getattr(logged_exc.__cause__, 'exceptions', ())
        if isinstance(e, httpx2.HTTPError)
    }
    assert upstreams == {'rest.variantvalidator.org', 'Ensembl VEP REST'}
