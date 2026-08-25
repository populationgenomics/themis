"""The rpc responses the door tests read, built from the generated message classes.

Every payload here is **hand-written to the shape its proto documents** — the `raw` paths the
contract's comments name, the null blocks the upstream sets, the typed fields beside them — and
parsed into the real message with `json_format.ParseDict`, so a door is exercised against the class
the agent holds rather than a stand-in. They are written rather than captured because no upstream is
reachable from the test suite, and the shape a door reads is the contract's claim about the upstream
rather than any one variant's answer.

Each builder returns a fresh payload, so a test can mutate one into the case it is about.
"""

from __future__ import annotations

from collections.abc import Sequence

from google.protobuf import json_format

from themis.evidence.models import evidence_pb2
from themis.rpc import clinvar_pb2, gnomad_pb2, mavedb_pb2, splice_pb2, transcript_pb2, vep_pb2


def provenance(source: str, *dataset_versions: str) -> evidence_pb2.Provenance:
    """One provenance record, as every response carries at least one."""
    return evidence_pb2.Provenance(source=source, dataset_versions=list(dataset_versions), query='(the request issued)')


def gnomad_payload() -> dict[str, object]:
    """The gnomAD `variant` query's answer: both callsets called, both passing variant QC.

    The paths `gnomad.proto` maps to the codes: `variant.joint.faf95.popmax` (POP_FRQ),
    `variant.<callset>.filters` (the QC verdict that figure has to pass), and
    `variant.<callset>.homozygote_count` / `.hemizygote_count` (POP_HMZ).
    """
    return {
        'variant': {
            'variant_id': '1-100000-A-G',
            'reference_genome': 'GRCh38',
            'exome': {
                'ac': 4,
                'an': 1461836,
                'af': 2.7e-06,
                'homozygote_count': 1,
                'hemizygote_count': 0,
                'filters': [],
                'flags': ['lcr'],
                'faf95': {'popmax': 1.02e-06, 'popmax_population': 'nfe'},
            },
            'genome': {
                'ac': 2,
                'an': 152312,
                'af': 1.31e-05,
                'homozygote_count': 2,
                'hemizygote_count': 0,
                'filters': [],
                'flags': [],
                'faf95': {'popmax': 9.3e-07, 'popmax_population': 'nfe'},
            },
            'joint': {
                'ac': 6,
                'an': 1614148,
                'filters': [],
                'faf95': {'popmax': 8.94e-07, 'popmax_population': 'nfe'},
            },
        }
    }


def gnomad_response(payload: dict[str, object] | None = None) -> gnomad_pb2.DescribeVariantResponse:
    """A `Gnomad.DescribeVariant` response over `payload`, defaulting to `gnomad_payload()`."""
    response = gnomad_pb2.DescribeVariantResponse(provenance=[provenance('gnomAD GraphQL', 'gnomad_r4', 'GRCh38')])
    json_format.ParseDict(gnomad_payload() if payload is None else payload, response.raw)
    return response


def coding_span(
    transcript: str, start: int, end: int, *, start_offset: int = 0, end_offset: int = 0
) -> clinvar_pb2.CodingSpan:
    """A record's parsed c. span, in the encoding `SearchCodingSpan` takes for its own endpoints.

    A positive position is in the CDS and a negative one in the 5'UTR; the offsets place an endpoint
    in the flanking intron, as `CodingCoordinate.intron_offset` does.
    """
    return clinvar_pb2.CodingSpan(
        transcript=transcript,
        start=_coordinate(start, start_offset),
        end=_coordinate(end, end_offset),
    )


def _coordinate(position: int, intron_offset: int) -> clinvar_pb2.CodingCoordinate:
    region = clinvar_pb2.CODING_REGION_FIVE_PRIME_UTR if position < 0 else clinvar_pb2.CODING_REGION_CDS
    return clinvar_pb2.CodingCoordinate(region=region, position=abs(position), intron_offset=intron_offset)


def clinvar_record(
    clinvar_id: str,
    *,
    classification: str = 'Pathogenic',
    review_stars: int = 1,
    span: clinvar_pb2.CodingSpan | None = None,
    hgvs: str = '',
) -> clinvar_pb2.ClinVarRecord:
    """One record of a ClinVar pool, with the fields the SVCv4 readings take."""
    record = clinvar_pb2.ClinVarRecord(
        clinvar_id=clinvar_id, classification=classification, review_stars=review_stars, hgvs=hgvs
    )
    if span is not None:
        record.coding_span.CopyFrom(span)
    return record


def clinvar_describe_request(*, review_status_floor: int) -> clinvar_pb2.DescribeVariantRequest:
    """The request a gene pool was fetched with; its floor is the one the search term carried."""
    return clinvar_pb2.DescribeVariantRequest(
        vcv='VCV000000001', gene='GENEX', review_status_floor=review_status_floor, max_pool_records=500
    )


def clinvar_describe_response(
    records: Sequence[clinvar_pb2.ClinVarRecord], *, pool_truncated: bool = False, unparsed: Sequence[str] = ()
) -> clinvar_pb2.DescribeVariantResponse:
    """A `ClinVar.DescribeVariant` answer carrying `records` as the gene's pathogenic pool."""
    return clinvar_pb2.DescribeVariantResponse(
        classified_in_gene=records,
        total_in_gene=len(records),
        considered_in_gene=len(records),
        pool_truncated=pool_truncated,
        records_with_unparsed_hgvs=list(unparsed),
        esearch_term='(the property term issued)',
        provenance=[provenance('NCBI ClinVar', 'ClinVar 2026-08-16')],
    )


def vep_payload() -> dict[str, object]:
    """Ensembl VEP's answer, with a per-transcript consequence carrying each wire form's score.

    The paths `vep.proto` maps to MIS_PRD: the element of `transcript_consequences` whose
    `transcript_id` names the transcript, and the predictor's score on it — `am_pathogenicity` as a
    first-class VEP field, and the dbNSFP column the plugin serves resolved to one value.
    """
    return {
        'input': 'NM_000123.4:c.3496G>C',
        'assembly_name': 'GRCh38',
        'most_severe_consequence': 'missense_variant',
        'transcript_consequences': [
            {
                'transcript_id': 'NM_000123.4',
                'hgvsc': 'NM_000123.4:c.3496G>C',
                'hgvsp': 'NP_000114.1:p.Gly1166Arg',
                'exon': '25/45',
                'consequence_terms': ['missense_variant'],
                'BayesDel_noAF_score': 0.35,
                'am_pathogenicity': 0.9812,
            },
            {
                'transcript_id': 'NM_000456.2',
                'hgvsc': 'NM_000456.2:c.100G>C',
                'consequence_terms': ['missense_variant'],
                'BayesDel_noAF_score': 0.11,
            },
        ],
    }


def vep_response(payload: dict[str, object] | None = None) -> vep_pb2.AnnotateResponse:
    """A `Vep.Annotate` response over `payload`, defaulting to `vep_payload()`."""
    response = vep_pb2.AnnotateResponse(
        most_severe_consequence=evidence_pb2.CONSEQUENCE_MISSENSE,
        provenance=[provenance('Ensembl VEP REST', 'Ensembl 113', 'GRCh38')],
    )
    json_format.ParseDict(vep_payload() if payload is None else payload, response.raw)
    return response


def splice_deltas_response(
    *,
    spliceai: tuple[float, float] | None = (0.02, 0.87),
    pangolin: tuple[float, float] | None = (0.01, 0.79),
) -> splice_pb2.PredictDeltasResponse:
    """A `Splice.PredictDeltas` answer: each predictor's (gain, loss) pair, or None where it scored none.

    The rpc has already reduced each host's per-transcript deltas onto one gain and one loss on a
    shared orientation, so `pangolin_loss` is the positive magnitude of a `DS_SL` the host signs
    negative.
    """
    response = splice_pb2.PredictDeltasResponse(
        provenance=[
            provenance('Broad SpliceAI', 'SpliceAI 1.3.1'),
            provenance('Broad Pangolin', 'Pangolin 1.0.2'),
        ]
    )
    if spliceai is not None:
        response.spliceai_gain, response.spliceai_loss = spliceai
    if pangolin is not None:
        response.pangolin_gain, response.pangolin_loss = pangolin
    return response


def transcript_structure(
    *, exon_lengths: Sequence[int] = (200, 200, 200, 200, 200), cds_transcript_start: int = 51
) -> transcript_pb2.GetStructureResponse:
    """A `Transcript.GetStructure` answer: the exon table in n. coordinates, with c.1's n. position.

    The exons tile the mature transcript, which is what the 50-nt margin is measured over.
    """
    response = transcript_pb2.GetStructureResponse(
        transcript='NM_000123.4',
        gene='GENEX',
        genome_build='GRCh38',
        chromosome_accession='NC_000001.11',
        strand=1,
        mane_select=True,
        transcript_length=sum(exon_lengths),
        cds_transcript_start=cds_transcript_start,
        cds_transcript_end=sum(exon_lengths) - 50,
        coding_length=sum(exon_lengths) - 50 - cds_transcript_start + 1,
        provenance=[provenance('VariantValidator', 'VariantValidator 2.2.0', 'GRCh38')],
    )
    start = 1
    for number, length in enumerate(exon_lengths, start=1):
        response.exons.add(
            number=number,
            genomic_start=start * 10,
            genomic_end=(start + length - 1) * 10,
            transcript_start=start,
            transcript_end=start + length - 1,
            length=length,
            coding_length=length,
            frame_shift_if_skipped=length % 3,
        )
        start += length
    return response


def predicted_skip(
    *,
    product: splice_pb2.SpliceProduct = splice_pb2.SPLICE_PRODUCT_PREMATURE_STOP,
    nmd_predicted: bool = True,
    ptc_cds_position: int | None = 400,
    nt_upstream_of_last_junction: int | None = 260,
) -> splice_pb2.PredictedSkip:
    """One skip of a `Splice.PredictSkipOutcome` answer, over the aberrant transcript's own structure."""
    skip = splice_pb2.PredictedSkip(
        skipped_exons=[3], coding_nt_removed=200, frame_shift=2, product=product, nmd_predicted=nmd_predicted
    )
    if ptc_cds_position is not None:
        skip.ptc_cds_position = ptc_cds_position
        skip.ptc_codon = ptc_cds_position // 3
    if nt_upstream_of_last_junction is not None:
        skip.nt_upstream_of_last_junction = nt_upstream_of_last_junction
    return skip


def mavedb_response(
    *, oddspath_ratio: float | None = 24.5, acmg_criterion: str = 'PS3', acmg_strength: str = 'Strong'
) -> mavedb_pb2.DescribeVariantResponse:
    """A `MaveDb.DescribeVariant` answer: the deposit that scores the allele, with its calibration."""
    response = mavedb_pb2.DescribeVariantResponse(
        acmg_criterion=acmg_criterion,
        acmg_strength=acmg_strength,
        score=-2.13,
        provenance=[provenance('MaveDB', 'MaveDB 2026-07-01')],
    )
    if oddspath_ratio is not None:
        response.oddspath_ratio = oddspath_ratio
    json_format.ParseDict({'scoreSet': {'urn': 'urn:mavedb:00000001-a-1', 'title': 'a score set'}}, response.raw)
    return response
