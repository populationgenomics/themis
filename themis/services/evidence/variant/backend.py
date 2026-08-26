"""The variant interface's port and its adapters: the spine every other interface joins on."""

from __future__ import annotations

import abc
import asyncio
import itertools
from collections.abc import Mapping, Sequence
from typing import cast, override

import httpx

from themis.evidence.models import evidence_pb2
from themis.rpc import variant_pb2
from themis.services.evidence import errors, fixtures, hgvs, provenance
from themis.services.evidence.upstreams import allele_registry, variant_validator, vep

_SECTION = 'normalize'
SECTIONS = frozenset({_SECTION})


class VariantBackend(abc.ABC):
    """The canonicalisation port: the seeded or resolved variant, or `errors.UnknownVariantError`."""

    @abc.abstractmethod
    async def normalize(self, request: variant_pb2.NormalizeRequest) -> variant_pb2.NormalizeResponse: ...


class FixtureBackend(VariantBackend):
    """In-memory backend answering from a seeded `{variant: normalized}` table."""

    def __init__(self, normalize: Mapping[str, variant_pb2.NormalizeResponse]) -> None:
        self._normalize = normalize

    @override
    async def normalize(self, request: variant_pb2.NormalizeRequest) -> variant_pb2.NormalizeResponse:
        return fixtures.lookup(self._normalize, request.variant, kind='variant')


def fixture_backend_from_json(raw: str | None, *, var_name: str) -> FixtureBackend:
    """Build the offline backend from its fixture var, or `SystemExit`."""
    seeds = fixtures.sections_from_json(raw, var_name=var_name, sections=SECTIONS)
    return FixtureBackend(fixtures.table(seeds, _SECTION, variant_pb2.NormalizeResponse, var_name=var_name))


class LiveBackend(VariantBackend):
    """The deployed backend, chaining the Allele Registry, VariantValidator and VEP."""

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client

    @override
    async def normalize(self, request: variant_pb2.NormalizeRequest) -> variant_pb2.NormalizeResponse:
        at = provenance.utcnow()
        allele = await allele_registry.fetch_allele_registry(request.variant, http_client=self._http_client)
        # VariantValidator has no record of an Ensembl accession, so the canonical stays RefSeq.
        canonical = allele.canonical_refseq_hgvs or request.variant
        validator, annotation = await self._project_and_annotate(request, canonical)
        gene_symbol = annotation.gene_symbol or allele.gene
        raw: dict[str, object] = {
            'allele_registry': allele.raw,
            'variant_validator': validator.raw,
            'vep': annotation.raw,
        }
        if not annotation.hgnc_id:
            raw['note'] = (
                'VEP carried no hgnc_id for the canonical transcript; downstream reference-table lookups '
                'key on HGNC id and cannot resolve this variant'
            )
        return variant_pb2.NormalizeResponse(
            caid=allele.caid,
            gnomad_v4_id=allele.gnomad_v4_id or _vcf_to_gnomad_id(validator.grch38_vcf),
            gnomad_v2_id=allele.gnomad_v2_id or _vcf_to_gnomad_id(validator.grch37_vcf),
            consequence=cast('evidence_pb2.Consequence', annotation.most_severe_consequence),
            transcripts=_merged_projections(validator.transcripts, allele.transcripts),
            gene_symbol=gene_symbol,
            hgnc_id=annotation.hgnc_id,
            clinvar_variations=allele.clinvar_variations,
            clinvar_alleles=allele.clinvar_alleles,
            raw=provenance.struct(raw),
            provenance=[
                provenance.provenance(allele, at),
                provenance.provenance(validator, at),
                provenance.provenance(annotation, at),
            ],
        )

    async def _project_and_annotate(
        self, request: variant_pb2.NormalizeRequest, canonical: str
    ) -> tuple[variant_validator.VariantValidatorResult, vep.VepResult]:
        """Project the canonical HGVS onto transcripts and annotate it, concurrently.

        Both legs read only `canonical`, and neither reads the other, so awaiting them in turn stacks
        their per-call timeouts — VariantValidator alone self-extends to 60 s — against the servicer's
        ceiling for no reason. A failure in either cancels the other and is re-raised with the whole
        group as its cause, so a run that lost both still carries both.
        """
        try:
            async with asyncio.TaskGroup() as legs:
                projection = legs.create_task(
                    variant_validator.fetch_variant_validator(
                        request.genome_build, canonical, 'mane', http_client=self._http_client
                    )
                )
                annotation = legs.create_task(
                    vep.fetch_vep(canonical, [], request.genome_build, http_client=self._http_client)
                )
        except BaseExceptionGroup as failures:
            raise errors.first_failure(failures) from failures
        return projection.result(), annotation.result()


def _merged_projections(
    validated: Sequence[variant_pb2.TranscriptProjection], listed: Sequence[variant_pb2.TranscriptProjection]
) -> list[variant_pb2.TranscriptProjection]:
    """Every transcript the allele projects onto, from both sources, deduped on the unversioned accession.

    Neither source is the other's superset: VariantValidator projects the transcripts the request
    selected, and the Allele Registry lists every transcript allele it holds — including the Ensembl
    half of each MANE pair, which VariantValidator has no record of. The two can also name one
    transcript at different versions, so the key is the base; a projection both state keeps
    VariantValidator's, whose c./p. is the validated one, and names both sources.
    """
    merged: dict[str, variant_pb2.TranscriptProjection] = {}
    for projection in itertools.chain(validated, listed):
        base = hgvs.accession_base(projection.transcript)
        held = merged.get(base)
        if held is None:
            copied = variant_pb2.TranscriptProjection()
            copied.CopyFrom(projection)
            copied.accession_base = base
            merged[base] = copied
        else:
            # Materialised before extending: appending from a generator that reads `held.sources`
            # sees a different list per protobuf implementation.
            held.sources.extend([source for source in projection.sources if source not in held.sources])
    return list(merged.values())


def _vcf_to_gnomad_id(locus: variant_validator.VcfLocus | None) -> str:
    if locus is None:
        return ''
    return f'{locus.chrom.removeprefix("chr")}-{locus.pos}-{locus.ref}-{locus.alt}'
