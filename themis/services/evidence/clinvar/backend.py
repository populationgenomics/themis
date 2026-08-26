"""The clinvar interface's port and its adapters."""

from __future__ import annotations

import abc
from collections.abc import Mapping
from typing import cast, override

import httpx

from themis.rpc import clinvar_pb2
from themis.services.evidence import fixtures, hgvs, provenance
from themis.services.evidence.upstreams import clinvar, transcript_structure

_VARIANT_SECTION = 'describe_variant'
_SPAN_SECTION = 'search_coding_span'
SECTIONS = frozenset({_VARIANT_SECTION, _SPAN_SECTION})

# SearchCodingSpan carries no genome build: the exon table it projects the c. span through is read on
# GRCh38, and ClinVar's own coordinates follow it.
_GRCH38 = 'GRCh38'


class ClinVarBackend(abc.ABC):
    """The ClinVar port: the seeded or fetched records, or `errors.UnknownVariantError`."""

    @abc.abstractmethod
    async def describe_variant(
        self, request: clinvar_pb2.DescribeVariantRequest
    ) -> clinvar_pb2.DescribeVariantResponse: ...

    @abc.abstractmethod
    async def search_coding_span(
        self, request: clinvar_pb2.SearchCodingSpanRequest
    ) -> clinvar_pb2.SearchCodingSpanResponse: ...


class FixtureBackend(ClinVarBackend):
    """In-memory backend answering from the seeded per-rpc tables."""

    def __init__(
        self,
        describe_variant: Mapping[str, clinvar_pb2.DescribeVariantResponse],
        search_coding_span: Mapping[str, clinvar_pb2.SearchCodingSpanResponse],
    ) -> None:
        self._describe_variant = describe_variant
        self._search_coding_span = search_coding_span

    @override
    async def describe_variant(
        self, request: clinvar_pb2.DescribeVariantRequest
    ) -> clinvar_pb2.DescribeVariantResponse:
        return fixtures.lookup(self._describe_variant, f'{request.vcv}:{request.gene}', kind='clinvar')

    @override
    async def search_coding_span(
        self, request: clinvar_pb2.SearchCodingSpanRequest
    ) -> clinvar_pb2.SearchCodingSpanResponse:
        key = f'{request.transcript}:{request.cds_start}:{request.cds_end}'
        return fixtures.lookup(self._search_coding_span, key, kind='clinvar_span')


def fixture_backend_from_json(raw: str | None, *, var_name: str) -> FixtureBackend:
    """Build the offline backend from its fixture var, or `SystemExit`."""
    seeds = fixtures.sections_from_json(raw, var_name=var_name, sections=SECTIONS)
    return FixtureBackend(
        fixtures.table(seeds, _VARIANT_SECTION, clinvar_pb2.DescribeVariantResponse, var_name=var_name),
        fixtures.table(seeds, _SPAN_SECTION, clinvar_pb2.SearchCodingSpanResponse, var_name=var_name),
    )


class LiveBackend(ClinVarBackend):
    """The deployed backend, over NCBI E-utilities."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client

    @override
    async def describe_variant(
        self, request: clinvar_pb2.DescribeVariantRequest
    ) -> clinvar_pb2.DescribeVariantResponse:
        """Two lookups, one per concern: the named variation's archive, and the gene's pathogenic pool.

        Issued in turn rather than concurrently: keyless E-utilities allows 3 req/s, and the pool
        alone walks several pages under that budget.
        """
        at = provenance.utcnow()
        archive = (
            await clinvar.fetch_variant_archive(request.vcv, http_client=self._http_client) if request.vcv else None
        )
        pool = await clinvar.fetch_gene_pool(
            request.gene,
            http_client=self._http_client,
            review_status_floor=request.review_status_floor,
            limit=request.max_pool_records,
        )
        returned = [*pool.records, *([archive.record] if archive is not None else [])]
        response = clinvar_pb2.DescribeVariantResponse(
            classified_in_gene=[_record(r) for r in pool.records],
            total_in_gene=pool.total,
            considered_in_gene=pool.considered,
            pool_truncated=pool.truncated,
            records_with_unparsed_hgvs=list(dict.fromkeys(r.clinvar_id for r in returned if r.coding_span is None)),
            esearch_term=pool.query,
            provenance=[
                *([provenance.provenance(archive, at)] if archive is not None else []),
                provenance.provenance(pool, at),
            ],
        )
        if archive is not None:
            response.this_variant.CopyFrom(_record(archive.record))
            response.variation_archive.CopyFrom(archive.variation_archive)
        return response

    @override
    async def search_coding_span(
        self, request: clinvar_pb2.SearchCodingSpanRequest
    ) -> clinvar_pb2.SearchCodingSpanResponse:
        at = provenance.utcnow()
        structure = await transcript_structure.fetch_transcript_structure(
            request.transcript, _GRCH38, http_client=self._http_client
        )
        span = transcript_structure.genomic_span_of_cds_range(structure, request.cds_start, request.cds_end)
        found = await clinvar.fetch_span_records(
            structure.gene, span.start, span.end, http_client=self._http_client, limit=request.max_records
        )
        return clinvar_pb2.SearchCodingSpanResponse(
            records=[_record(r) for r in found.records],
            total_in_span=found.total,
            considered_in_span=found.considered,
            span_truncated=found.truncated,
            records_with_unparsed_hgvs=list(
                dict.fromkeys(r.clinvar_id for r in found.records if r.coding_span is None)
            ),
            transcript=structure.transcript,
            gene=structure.gene,
            chromosome_accession=structure.chromosome_accession,
            searched_span=span,
            esearch_term=found.query,
            variantvalidator_transcript=provenance.struct(structure.raw),
            provenance=[provenance.provenance(structure, at), provenance.provenance(found, at)],
        )


def _zygosity_count(zygosity_count: clinvar.ClinvarZygosityCountData) -> clinvar_pb2.ClinVarZygosityCount:
    proto = clinvar_pb2.ClinVarZygosityCount(zygosity=zygosity_count.zygosity)
    if zygosity_count.count is not None:
        proto.count = zygosity_count.count
    return proto


def _observation(observation: clinvar.ClinvarObservationData) -> clinvar_pb2.ClinVarObservation:
    proto = clinvar_pb2.ClinVarObservation(
        origin=observation.origin,
        affected_status=observation.affected_status,
        zygosities=[_zygosity_count(z) for z in observation.zygosities],
        age=observation.age,
        sex=observation.sex,
        collection_method=observation.collection_method,
        descriptions=observation.descriptions,
        traits=observation.traits,
        pubmed_ids=observation.pubmed_ids,
    )
    if observation.variant_alleles is not None:
        proto.variant_alleles = observation.variant_alleles
    return proto


def _submission(submission: clinvar.ClinvarSubmissionData) -> clinvar_pb2.ClinVarSubmission:
    return clinvar_pb2.ClinVarSubmission(
        scv=submission.scv,
        submitter=submission.submitter,
        organization_category=submission.organization_category,
        classification=submission.classification,
        review_status=submission.review_status,
        date_evaluated=submission.date_evaluated,
        assertion_method=submission.assertion_method,
        mode_of_inheritance=submission.mode_of_inheritance,
        comment=submission.comment,
        conditions=submission.conditions,
        pubmed_ids=submission.pubmed_ids,
        erepo_url=submission.erepo_url,
        observations=[_observation(o) for o in submission.observations],
    )


def _coding_coordinate(coordinate: hgvs.CodingCoordinate) -> clinvar_pb2.CodingCoordinate:
    return clinvar_pb2.CodingCoordinate(
        region=cast('clinvar_pb2.CodingRegion', coordinate.region),
        position=coordinate.position,
        intron_offset=coordinate.intron_offset,
    )


def _record(record: clinvar.ClinvarRecordData) -> clinvar_pb2.ClinVarRecord:
    message = clinvar_pb2.ClinVarRecord(
        clinvar_id=record.clinvar_id,
        hgvs=record.hgvs,
        classification=record.classification,
        review_stars=record.review_stars,
        conditions=record.conditions,
        submissions=[_submission(s) for s in record.submissions],
        review_status=record.review_status,
    )
    if record.coding_span is not None:
        message.coding_span.CopyFrom(
            clinvar_pb2.CodingSpan(
                transcript=record.coding_span.transcript,
                start=_coding_coordinate(record.coding_span.start),
                end=_coding_coordinate(record.coding_span.end),
            )
        )
    return message
