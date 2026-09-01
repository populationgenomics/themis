"""LiveBackend composition: the gene identity `Normalize` surfaces, and how its two legs fail.

Every upstream client function is replaced with a canned Result, so no test here touches the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

import httpx2
import pytest

from themis.evidence.models import evidence_pb2
from themis.rpc import variant_pb2
from themis.services.evidence import errors
from themis.services.evidence.upstreams import allele_registry, variant_validator, vep
from themis.services.evidence.variant import backend as variant_backend


def _returns[T](value: T) -> Callable[..., Awaitable[T]]:
    """An async stand-in for an upstream client function that ignores its args and returns `value`."""

    async def fake(*_args: object, **_kwargs: object) -> T:
        return value

    return fake


def _run[T](call: Callable[[variant_backend.LiveBackend], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient() as client:
            return await call(variant_backend.LiveBackend(client))

    return asyncio.run(run())


def test_normalize_composes_allele_validator_and_vep(monkeypatch: pytest.MonkeyPatch) -> None:
    transcript = variant_pb2.TranscriptProjection(
        transcript='NM_007294.4', hgvs_c='NM_007294.4:c.68A>G', hgvs_p='p.Glu23Gly', mane_select=True
    )
    monkeypatch.setattr(
        allele_registry,
        'fetch_allele_registry',
        _returns(
            allele_registry.AlleleRegistryResult(
                caid='CA123',
                gnomad_v4_id='17-43093464-A-G',
                gnomad_v2_id='17-41245466-A-G',
                transcripts=[transcript],
                canonical_refseq_hgvs='NM_007294.4:c.68A>G',
                gene='BRCA1',
                clinvar_variations=[
                    variant_pb2.ClinVarVariation(variation_id=55407, vcv='VCV000055407', rcv=['RCV000031121'])
                ],
                clinvar_alleles=[
                    variant_pb2.ClinVarAllele(allele_id=70842, preferred_name='NM_007294.4(BRCA1):c.68A>G')
                ],
                raw={'@id': 'CA123'},
                source='ClinGen Allele Registry',
                dataset_versions=(),
                query='allele?hgvs=NM_007294.4:c.68A>G',
            )
        ),
    )
    monkeypatch.setattr(
        variant_validator,
        'fetch_variant_validator',
        _returns(
            variant_validator.VariantValidatorResult(
                transcripts=[transcript],
                grch37_vcf=variant_validator.VcfLocus('17', '41245466', 'A', 'G'),
                grch38_vcf=variant_validator.VcfLocus('17', '43093464', 'A', 'G'),
                gene='BRCA1',
                raw={'flag': 'gene_variant'},
                source='VariantValidator',
                dataset_versions=('VV 2', 'VVDB'),
                query='vv/GRCh38',
            )
        ),
    )
    monkeypatch.setattr(
        vep,
        'fetch_vep',
        _returns(
            vep.VepResult(
                most_severe_consequence=evidence_pb2.CONSEQUENCE_MISSENSE,
                gene_symbol='BRCA1',
                hgnc_id='HGNC:1100',
                raw={'most_severe_consequence': 'missense_variant'},
                source='Ensembl VEP REST',
                dataset_versions=('GRCh38',),
                query='vep/hgvs',
            )
        ),
    )

    normalized = _run(
        lambda be: be.normalize(variant_pb2.NormalizeRequest(variant='NM_007294.4:c.68A>G', genome_build='GRCh38'))
    )
    assert normalized.caid == 'CA123'
    assert normalized.gnomad_v4_id == '17-43093464-A-G'
    assert normalized.consequence == evidence_pb2.CONSEQUENCE_MISSENSE
    assert [t.transcript for t in normalized.transcripts] == ['NM_007294.4']
    assert normalized.transcripts[0].mane_select
    assert normalized.gene_symbol == 'BRCA1'
    assert normalized.hgnc_id == 'HGNC:1100'
    assert [p.source for p in normalized.provenance] == [
        'ClinGen Allele Registry',
        'VariantValidator',
        'Ensembl VEP REST',
    ]
    assert set(normalized.raw.keys()) == {'allele_registry', 'variant_validator', 'vep'}
    # The crosswalk is the accession clinvar.DescribeVariant takes, and the two ClinVar entity levels
    # stay apart: the registry states no key between an allele record and a variation record.
    assert [(v.vcv, list(v.rcv)) for v in normalized.clinvar_variations] == [('VCV000055407', ['RCV000031121'])]
    assert [(a.allele_id, a.preferred_name) for a in normalized.clinvar_alleles] == [
        (70842, 'NM_007294.4(BRCA1):c.68A>G')
    ]


def _normalize_over_projections(
    monkeypatch: pytest.MonkeyPatch,
    validated: Sequence[variant_pb2.TranscriptProjection],
    listed: Sequence[variant_pb2.TranscriptProjection],
) -> variant_pb2.NormalizeResponse:
    monkeypatch.setattr(
        allele_registry,
        'fetch_allele_registry',
        _returns(
            allele_registry.AlleleRegistryResult(
                caid='CA123',
                gnomad_v4_id='17-43093464-A-G',
                gnomad_v2_id=None,
                transcripts=list(listed),
                canonical_refseq_hgvs='NM_007294.4:c.68A>G',
                gene='BRCA1',
                clinvar_variations=[],
                clinvar_alleles=[],
                raw={},
                source='ClinGen Allele Registry',
                dataset_versions=(),
                query='q',
            )
        ),
    )
    monkeypatch.setattr(
        variant_validator,
        'fetch_variant_validator',
        _returns(
            variant_validator.VariantValidatorResult(
                transcripts=list(validated),
                grch37_vcf=None,
                grch38_vcf=None,
                gene='BRCA1',
                raw={},
                source='VariantValidator',
                dataset_versions=(),
                query='q',
            )
        ),
    )
    monkeypatch.setattr(
        vep,
        'fetch_vep',
        _returns(
            vep.VepResult(
                most_severe_consequence=evidence_pb2.CONSEQUENCE_MISSENSE,
                gene_symbol='BRCA1',
                hgnc_id='HGNC:1100',
                raw={},
                source='Ensembl VEP REST',
                dataset_versions=('GRCh38',),
                query='q',
            )
        ),
    )
    return _run(
        lambda be: be.normalize(variant_pb2.NormalizeRequest(variant='NM_007294.4:c.68A>G', genome_build='GRCh38'))
    )


def test_normalize_carries_every_transcript_the_allele_projects_onto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither source alone is the projection set.

    A plural field holding one entry is read as the gene's whole transcript list, and that is what
    VariantValidator alone fills it with: it projects the transcripts the request selected — one,
    under the MANE selector —
    while the registry lists every transcript allele it holds, the Ensembl half of each MANE pair
    included. The field is the union, so its name and its cardinality agree.
    """
    validated = variant_pb2.TranscriptProjection(
        transcript='NM_007294.4',
        hgvs_c='NM_007294.4:c.68A>G',
        hgvs_p='p.Glu23Gly',
        mane_select=True,
        sources=['VariantValidator'],
    )
    normalized = _normalize_over_projections(
        monkeypatch,
        [validated],
        [
            variant_pb2.TranscriptProjection(
                transcript='NM_007294.4', hgvs_c='NM_007294.4:c.68A>G', sources=['ClinGen Allele Registry']
            ),
            variant_pb2.TranscriptProjection(
                transcript='ENST00000357654.9',
                hgvs_c='ENST00000357654.9:c.68A>G',
                mane_select=True,
                sources=['ClinGen Allele Registry'],
            ),
            variant_pb2.TranscriptProjection(
                transcript='NM_007300.4', hgvs_c='NM_007300.4:c.68A>G', sources=['ClinGen Allele Registry']
            ),
        ],
    )

    assert [t.transcript for t in normalized.transcripts] == ['NM_007294.4', 'ENST00000357654.9', 'NM_007300.4']
    projections = {t.transcript: t for t in normalized.transcripts}
    # Both stated the canonical transcript, so it names both — and keeps the validated protein
    # consequence, which the registry's entry did not carry.
    assert list(projections['NM_007294.4'].sources) == ['VariantValidator', 'ClinGen Allele Registry']
    assert projections['NM_007294.4'].hgvs_p == 'p.Glu23Gly'
    assert list(projections['ENST00000357654.9'].sources) == ['ClinGen Allele Registry']


def test_normalize_falls_back_to_registry_symbol_when_vep_lacks_hgnc_id(monkeypatch: pytest.MonkeyPatch) -> None:
    transcript = variant_pb2.TranscriptProjection(transcript='NM_x', hgvs_c='NM_x:c.1A>T', mane_select=True)
    monkeypatch.setattr(
        allele_registry,
        'fetch_allele_registry',
        _returns(
            allele_registry.AlleleRegistryResult(
                caid='CA9',
                gnomad_v4_id=None,
                gnomad_v2_id=None,
                transcripts=[transcript],
                canonical_refseq_hgvs=None,
                gene='TP53',
                clinvar_variations=[],
                clinvar_alleles=[],
                raw={},
                source='ClinGen Allele Registry',
                dataset_versions=(),
                query='q',
            )
        ),
    )
    monkeypatch.setattr(
        variant_validator,
        'fetch_variant_validator',
        _returns(
            variant_validator.VariantValidatorResult(
                transcripts=[transcript],
                grch37_vcf=variant_validator.VcfLocus('chr17', '7579472', 'G', 'C'),
                grch38_vcf=variant_validator.VcfLocus('chr17', '7676154', 'G', 'C'),
                gene='TP53',
                raw={},
                source='VariantValidator',
                dataset_versions=(),
                query='q',
            )
        ),
    )
    # VEP carried no gene identity: symbol falls back to the registry, hgnc_id stays empty (+ a note).
    monkeypatch.setattr(
        vep,
        'fetch_vep',
        _returns(vep.VepResult(evidence_pb2.CONSEQUENCE_NONSENSE, '', '', {}, 'Ensembl VEP REST', ('GRCh38',), 'q')),
    )

    normalized = _run(
        lambda be: be.normalize(variant_pb2.NormalizeRequest(variant='NM_000546.6:c.916C>T', genome_build='GRCh38'))
    )
    assert normalized.gnomad_v4_id == '17-7676154-G-C'
    assert normalized.gnomad_v2_id == '17-7579472-G-C'
    assert normalized.gene_symbol == 'TP53'  # registry symbol, since VEP had none
    assert normalized.hgnc_id == ''  # never fabricated
    assert 'note' in normalized.raw


def test_normalize_canonicalises_onto_the_refseq_mane_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    """The HGVS handed downstream must be RefSeq: VariantValidator rejects Ensembl accessions.

    The registry lists a MANE Select transcript once per accession namespace with the Ensembl entry
    first, so a canonical taken from the head of the projection list is the one that breaks.
    """
    projections = [
        variant_pb2.TranscriptProjection(
            transcript='ENST00000358273.9', hgvs_c='ENST00000358273.9:c.3496G>C', mane_select=True
        ),
        variant_pb2.TranscriptProjection(
            transcript='NM_001042492.3', hgvs_c='NM_001042492.3:c.3496G>C', mane_select=True
        ),
    ]
    monkeypatch.setattr(
        allele_registry,
        'fetch_allele_registry',
        _returns(
            allele_registry.AlleleRegistryResult(
                caid='CA398989536',
                gnomad_v4_id=None,
                gnomad_v2_id=None,
                transcripts=projections,
                canonical_refseq_hgvs='NM_001042492.3:c.3496G>C',
                gene='NF1',
                clinvar_variations=[],
                clinvar_alleles=[],
                raw={},
                source='ClinGen Allele Registry',
                dataset_versions=(),
                query='q',
            )
        ),
    )
    seen: dict[str, str] = {}

    async def fake_variant_validator(
        _build: str, variant: str, _select: str, **_kwargs: object
    ) -> variant_validator.VariantValidatorResult:
        seen['variant_validator'] = variant
        return variant_validator.VariantValidatorResult(
            transcripts=projections[1:],
            grch37_vcf=None,
            grch38_vcf=variant_validator.VcfLocus('chr17', '31232881', 'G', 'C'),
            gene='NF1',
            raw={},
            source='VariantValidator',
            dataset_versions=(),
            query='q',
        )

    async def fake_vep(variant: str, _predictors: list[str], _build: str, **_kwargs: object) -> vep.VepResult:
        seen['vep'] = variant
        return vep.VepResult(
            evidence_pb2.CONSEQUENCE_MISSENSE, 'NF1', 'HGNC:7765', {}, 'Ensembl VEP REST', ('GRCh38',), 'q'
        )

    monkeypatch.setattr(variant_validator, 'fetch_variant_validator', fake_variant_validator)
    monkeypatch.setattr(vep, 'fetch_vep', fake_vep)

    # A non-MANE transcript of the same gene, so the canonicalisation step is doing real work.
    _run(lambda be: be.normalize(variant_pb2.NormalizeRequest(variant='NM_000267.3:c.3496G>C', genome_build='GRCh38')))

    assert seen == {'variant_validator': 'NM_001042492.3:c.3496G>C', 'vep': 'NM_001042492.3:c.3496G>C'}


def _allele() -> allele_registry.AlleleRegistryResult:
    return allele_registry.AlleleRegistryResult(
        caid='CA123',
        gnomad_v4_id=None,
        gnomad_v2_id=None,
        transcripts=[],
        canonical_refseq_hgvs='NM_007294.4:c.68A>G',
        gene='BRCA1',
        clinvar_variations=[],
        clinvar_alleles=[],
        raw={},
        source='ClinGen Allele Registry',
        dataset_versions=(),
        query='q',
    )


def _normalizing(monkeypatch: pytest.MonkeyPatch) -> Callable[[variant_backend.LiveBackend], Awaitable[object]]:
    monkeypatch.setattr(allele_registry, 'fetch_allele_registry', _returns(_allele()))
    request = variant_pb2.NormalizeRequest(variant='NM_007294.4:c.68A>G', genome_build='GRCh38')
    return lambda be: be.normalize(request)


def test_normalize_runs_its_two_independent_legs_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither reads the other, so awaiting them in turn stacks their timeouts under the rpc ceiling.

    Asserted as overlap rather than as elapsed time: the property is that the second leg starts
    before the first finishes, which no scheduling jitter can turn true or false by accident.
    """
    running = 0
    overlapped = False

    async def leg(*_args: object, **_kwargs: object) -> object:
        nonlocal running, overlapped
        running += 1
        overlapped = overlapped or running > 1
        await asyncio.sleep(0.01)
        running -= 1
        raise errors.UnknownVariantError('leg reached')

    monkeypatch.setattr(variant_validator, 'fetch_variant_validator', leg)
    monkeypatch.setattr(vep, 'fetch_vep', leg)
    with pytest.raises(errors.UnknownVariantError):
        _run(_normalizing(monkeypatch))
    assert overlapped


def test_a_failing_leg_keeps_its_own_status_rather_than_a_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """A BaseExceptionGroup would reach the servicer as an uncharacterised fault and be retried."""

    async def refuses(*_args: object, **_kwargs: object) -> object:
        raise errors.InvalidRequestError('VEP refused the expression')

    async def slow(*_args: object, **_kwargs: object) -> object:
        await asyncio.sleep(10)
        raise AssertionError('the sibling leg should have been cancelled')

    monkeypatch.setattr(variant_validator, 'fetch_variant_validator', slow)
    monkeypatch.setattr(vep, 'fetch_vep', refuses)
    with pytest.raises(errors.InvalidRequestError, match='VEP refused'):
        _run(_normalizing(monkeypatch))


def test_when_both_legs_fail_neither_error_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unknown(*_args: object, **_kwargs: object) -> object:
        raise errors.UnknownVariantError('VariantValidator holds no record')

    async def refuses(*_args: object, **_kwargs: object) -> object:
        raise errors.InvalidRequestError('VEP refused the expression')

    monkeypatch.setattr(variant_validator, 'fetch_variant_validator', unknown)
    monkeypatch.setattr(vep, 'fetch_vep', refuses)
    with pytest.raises(errors.UnknownVariantError) as caught:
        _run(_normalizing(monkeypatch))
    carried = [str(e) for e in getattr(caught.value.__cause__, 'exceptions', [])]
    assert any('VEP refused' in message for message in carried)
