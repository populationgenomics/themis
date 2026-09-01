"""The transcript interface's port and its adapters: the exon table, and the exon-relevance compose over it."""

from __future__ import annotations

import abc
import asyncio
import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from typing import override

import httpx2

from themis.evidence.models import evidence_pb2
from themis.rpc import transcript_pb2
from themis.services.evidence import errors, fixtures, hgvs, provenance
from themis.services.evidence.upstreams import clinvar, gnomad, gtex, transcript_structure

_STRUCTURE_SECTION = 'get_structure'
_RELEVANCE_SECTION = 'assess_exon_relevance'
SECTIONS = frozenset({_STRUCTURE_SECTION, _RELEVANCE_SECTION})

# AssessExonRelevance carries no genome build: gnomAD's pext regions and GTEx are GRCh38-only.
_GRCH38 = 'GRCh38'

# The star floor the P/LP density counts at. Fixed here rather than a request field because the
# density is one coarse burden number, and stated in the proto so it is not a hidden policy; the rpcs
# whose pool feeds a scored comparison take the floor from their caller instead.
_DENSITY_REVIEW_STATUS_FLOOR = 1

# And the bound on the records it counts, fixed for the same reason the floor is.
# `raw.clinvar_pool_truncated` says when it bound.
_DENSITY_POOL_LIMIT = 500

# The proto namespace each VariantValidator annotation set names its transcripts in.
_INVENTORY_NAMESPACES: Mapping[str, transcript_pb2.TranscriptNamespace] = {
    'refseq': transcript_pb2.TRANSCRIPT_NAMESPACE_REFSEQ,
    'ensembl': transcript_pb2.TRANSCRIPT_NAMESPACE_ENSEMBL,
}
# The namespace GTEx keys its expression rows by, so the only one an unmeasured transcript is a
# reportable absence in: a RefSeq accession has no GTEx row to be missing.
_EXPRESSION_NAMESPACE = transcript_pb2.TRANSCRIPT_NAMESPACE_ENSEMBL


class TranscriptBackend(abc.ABC):
    """The transcript port: the seeded or fetched exon table and exon signals."""

    @abc.abstractmethod
    async def get_structure(
        self, request: transcript_pb2.GetStructureRequest
    ) -> transcript_pb2.GetStructureResponse: ...

    @abc.abstractmethod
    async def assess_exon_relevance(
        self, request: transcript_pb2.AssessExonRelevanceRequest
    ) -> transcript_pb2.AssessExonRelevanceResponse: ...


def structure_key(request: transcript_pb2.GetStructureRequest) -> str:
    """The fixture key for one structure query: the queried position is part of it.

    The exon table is the same for every position, but the response's `position` is not — keying on
    the transcript alone would answer a position the seed was not built for.
    """
    base = f'{request.transcript}:{request.genome_build}'
    match request.WhichOneof('query_position'):
        case 'cds_position':
            return f'{base}:c:{request.cds_position}'
        case 'genomic_position':
            return f'{base}:g:{request.genomic_position}'
        case _:
            return base


class FixtureBackend(TranscriptBackend):
    """In-memory backend answering from the seeded per-rpc tables."""

    def __init__(
        self,
        get_structure: Mapping[str, transcript_pb2.GetStructureResponse],
        assess_exon_relevance: Mapping[str, transcript_pb2.AssessExonRelevanceResponse],
    ) -> None:
        self._get_structure = get_structure
        self._assess_exon_relevance = assess_exon_relevance

    @override
    async def get_structure(self, request: transcript_pb2.GetStructureRequest) -> transcript_pb2.GetStructureResponse:
        return fixtures.lookup(self._get_structure, structure_key(request), kind='transcript_structure')

    @override
    async def assess_exon_relevance(
        self, request: transcript_pb2.AssessExonRelevanceRequest
    ) -> transcript_pb2.AssessExonRelevanceResponse:
        key = f'{request.gene}:{request.transcript}:{request.exon}'
        return fixtures.lookup(self._assess_exon_relevance, key, kind='exon_relevance')


def fixture_backend_from_json(raw: str | None, *, var_name: str) -> FixtureBackend:
    """Build the offline backend from its fixture var, or `SystemExit`."""
    seeds = fixtures.sections_from_json(raw, var_name=var_name, sections=SECTIONS)
    return FixtureBackend(
        fixtures.table(seeds, _STRUCTURE_SECTION, transcript_pb2.GetStructureResponse, var_name=var_name),
        fixtures.table(seeds, _RELEVANCE_SECTION, transcript_pb2.AssessExonRelevanceResponse, var_name=var_name),
    )


class LiveBackend(TranscriptBackend):
    """The deployed backend, over VariantValidator, gnomAD, GTEx and NCBI ClinVar."""

    def __init__(self, http_client: httpx2.AsyncClient) -> None:
        self._http_client = http_client

    @override
    async def get_structure(self, request: transcript_pb2.GetStructureRequest) -> transcript_pb2.GetStructureResponse:
        at = provenance.utcnow()
        structure = await transcript_structure.fetch_transcript_structure(
            request.transcript, request.genome_build, http_client=self._http_client
        )
        response = transcript_pb2.GetStructureResponse(
            transcript=structure.transcript,
            gene=structure.gene,
            genome_build=request.genome_build,
            chromosome_accession=structure.chromosome_accession,
            strand=structure.strand,
            mane_select=structure.mane_select,
            mane_plus_clinical=structure.mane_plus_clinical,
            transcript_length=structure.transcript_length,
            cds_transcript_start=structure.cds_transcript_start,
            cds_transcript_end=structure.cds_transcript_end,
            coding_length=structure.coding_length,
            exons=structure.exons,
            raw=provenance.struct(structure.raw),
            provenance=[provenance.provenance(structure, at)],
        )
        located = _located_position(structure, request)
        if located is not None:
            response.position.CopyFrom(located)
        return response

    @override
    async def assess_exon_relevance(
        self, request: transcript_pb2.AssessExonRelevanceRequest
    ) -> transcript_pb2.AssessExonRelevanceResponse:
        at = provenance.utcnow()
        gene = await gnomad.fetch_gnomad_gene(request.gene, http_client=self._http_client)
        pool = await clinvar.fetch_gene_pool(
            request.gene,
            http_client=self._http_client,
            review_status_floor=_DENSITY_REVIEW_STATUS_FLOOR,
            limit=_DENSITY_POOL_LIMIT,
        )
        isoforms = await gtex.fetch_gtex_by_symbol(
            request.gene, tissues=list(request.tissues), http_client=self._http_client
        )
        # gnomAD keys pext by genomic interval, so each exon's span is what selects its regions.
        structure, annotation_sets = await asyncio.gather(
            transcript_structure.fetch_transcript_structure(request.transcript, _GRCH38, http_client=self._http_client),
            transcript_structure.fetch_gene_transcripts(request.gene, _GRCH38, http_client=self._http_client),
        )
        # Requested before the profile is built: an exon number the transcript lacks is the caller's
        # error, not a gap in the profile.
        exon = _exon_by_number(structure, request.exon)
        _require_one_gene(structure, annotation_sets)
        tissues = _pext_tissues(gene.pext_regions, request.tissues)
        inventory = _transcript_inventory(
            annotation_sets,
            exon=exon,
            chromosome_accession=structure.chromosome_accession,
            assessed_transcript=structure.transcript,
            isoforms=isoforms,
        )
        signals = transcript_pb2.AssessExonRelevanceResponse(
            in_mane_select=request.in_mane_select,
            in_mane_plus_clinical=request.in_mane_plus_clinical,
            pext=[_exon_pext(gene.pext_regions, each, tissues.columns) for each in structure.exons],
            clinvar_plp_density=len(pool.records),
            gtex_expression=[_expression(median) for median in isoforms.medians],
            transcript_inventory=inventory.entries,
            inventory_denominators=inventory.denominators,
            transcripts_without_structure=inventory.without_structure,
            transcripts_without_expression=inventory.without_expression,
            tissues_without_pext=tissues.without_pext,
            tissues_without_expression=isoforms.tissues_without_rows,
            raw=provenance.struct(
                {
                    'gnomad_gene': _gene_without_pext(gene.raw),
                    'pext_regions': [{'start': r.start, 'stop': r.stop, 'mean': r.mean} for r in gene.pext_regions],
                    'exon_genomic_start': exon.genomic_start,
                    'exon_genomic_end': exon.genomic_end,
                    'gtex_isoforms': _gtex_isoforms(isoforms, include_detail=request.include_gtex_detail),
                    'clinvar_plp_density_scope': 'gene',
                    'clinvar_total_in_gene': pool.total,
                    'clinvar_considered_in_gene': pool.considered,
                    'clinvar_pool_truncated': pool.truncated,
                }
            ),
            provenance=[
                provenance.provenance(gene, at),
                provenance.provenance(pool, at),
                provenance.provenance(isoforms, at),
                provenance.provenance(structure, at),
                *(provenance.provenance(annotation_set, at) for annotation_set in annotation_sets),
            ],
        )
        if gene.loeuf is not None:
            signals.loeuf = gene.loeuf
        if gene.mane_select is not None:
            signals.pext_mane_select.refseq = gene.mane_select.refseq
            signals.pext_mane_select.ensembl = gene.mane_select.ensembl
        return signals


def _exon_by_number(structure: transcript_structure.TranscriptStructureResult, number: int) -> transcript_pb2.Exon:
    """The exon the request names.

    Raises:
        errors.InvalidRequestError: If the transcript has no exon with that number — a caller reading
            a signal for the wrong exon is worse than being told the number is out of range.
    """
    for exon in structure.exons:
        if exon.number == number:
            return exon
    raise errors.InvalidRequestError(
        f'{structure.transcript} has {len(structure.exons)} exons; AssessExonRelevance was asked for exon {number}'
    )


def _covering(regions: Sequence[gnomad.PextRegion], exon: transcript_pb2.Exon) -> list[tuple[gnomad.PextRegion, int]]:
    """The pext regions overlapping the exon, each with the number of exonic bases it covers."""
    overlaps = []
    for region in regions:
        overlap = min(exon.genomic_end, region.stop) - max(exon.genomic_start, region.start) + 1
        if overlap > 0:
            overlaps.append((region, overlap))
    return overlaps


def _exon_pext(
    regions: Sequence[gnomad.PextRegion], exon: transcript_pb2.Exon, tissues: Mapping[str, str]
) -> transcript_pb2.ExonPext:
    """One exon's pext: its covering regions' values, weighted by how much of the exon each covers.

    `mean` is left unset when no region covers the exon — gnomAD holds no pext there, which is not a
    pext of 0 — and `covered_bases` says how much of the exon the value was read over, since gnomAD
    publishes pext across coding bases only.

    Args:
        regions: The gene's pext regions.
        exon: The exon whose genomic span selects them.
        tissues: The requested GTEx tissue ids that gnomAD carries pext for, each mapped to its
            gnomAD pext column; empty when the request named none.
    """
    covering = _covering(regions, exon)
    entry = transcript_pb2.ExonPext(exon=exon.number, exon_bases=exon.genomic_end - exon.genomic_start + 1)
    if not covering:
        return entry
    covered = sum(overlap for _, overlap in covering)
    entry.covered_bases = covered
    entry.mean = sum(region.mean * overlap for region, overlap in covering) / covered
    entry.tissues.extend(
        transcript_pb2.PextTissueValue(
            tissue=tissue,
            value=sum(region.tissues[column] * overlap for region, overlap in covering) / covered,
        )
        for tissue, column in tissues.items()
    )
    return entry


def _gene_without_pext(gene: Mapping[str, object]) -> dict[str, object]:
    """The gnomAD gene payload with the pext block dropped from `raw`.

    49 tissue values on each of a gene's regions is 175 kB on NF1, for numbers the typed profile and
    `raw.pext_regions` already carry at the granularity a caller reads them.
    """
    return {key: value for key, value in gene.items() if key != 'pext'}


@dataclasses.dataclass(frozen=True)
class _PextTissues:
    """The requested tissues, split by whether gnomAD carries a pext column for each.

    Attributes:
        columns: Requested GTEx tissue id -> the gnomAD pext column carrying it.
        without_pext: Requested tissues gnomAD holds no pext column for.
    """

    columns: dict[str, str]
    without_pext: list[str]


def _pext_tissues(regions: Sequence[gnomad.PextRegion], requested: Sequence[str]) -> _PextTissues:
    """Map the requested GTEx tissues onto gnomAD's pext columns, naming the ones it has none for."""
    if not regions:
        # No vocabulary to check against, and nothing is missing: gnomAD holding no pext for the gene
        # is every exon's unset mean, not a missing tissue.
        return _PextTissues(columns={}, without_pext=[])
    vocabulary = regions[0].tissues
    columns = {tissue: gnomad.pext_tissue_key(tissue) for tissue in requested}
    return _PextTissues(
        columns={tissue: column for tissue, column in columns.items() if column in vocabulary},
        without_pext=[tissue for tissue, column in columns.items() if column not in vocabulary],
    )


def _exon_membership(
    spans: Sequence[transcript_structure.ExonSpan], exon: transcript_pb2.Exon
) -> tuple[transcript_pb2.ExonMembership, list[transcript_structure.ExonSpan]]:
    """How one transcript's exon table relates to the assessed exon's genomic interval.

    Returns:
        The verdict, and the transcript's own exons overlapping the interval — empty for every
        verdict but `CARRIES_A_DIFFERENT_INTERVAL`.
    """
    overlapping = [span for span in spans if span.end >= exon.genomic_start and span.start <= exon.genomic_end]
    if overlapping:
        identical = (
            len(overlapping) == 1
            and overlapping[0].start == exon.genomic_start
            and overlapping[0].end == exon.genomic_end
        )
        if identical:
            return transcript_pb2.EXON_MEMBERSHIP_CARRIES_THE_EXON, []
        return transcript_pb2.EXON_MEMBERSHIP_CARRIES_A_DIFFERENT_INTERVAL, overlapping
    if min(span.start for span in spans) <= exon.genomic_start and max(span.end for span in spans) >= exon.genomic_end:
        return transcript_pb2.EXON_MEMBERSHIP_SPANS_BUT_SKIPS, []
    return transcript_pb2.EXON_MEMBERSHIP_DOES_NOT_REACH, []


def _expression(median: gtex.TissueMedian) -> transcript_pb2.TranscriptExpression:
    return transcript_pb2.TranscriptExpression(
        transcript=median.transcript, tissue=median.tissue, median_tpm=median.median, transcript_base=median.base
    )


def _expression_by_base(medians: Iterable[gtex.TissueMedian]) -> dict[str, list[transcript_pb2.TranscriptExpression]]:
    """The expression rows grouped by unversioned accession — the key the inventory joins on."""
    grouped: dict[str, list[transcript_pb2.TranscriptExpression]] = {}
    for median in medians:
        grouped.setdefault(median.base, []).append(_expression(median))
    return grouped


def _expression_owner(
    records: Sequence[transcript_structure.GeneTranscript], rows: Sequence[transcript_pb2.TranscriptExpression]
) -> str | None:
    """Which of the records sharing one unversioned accession the rows belong to, if it is decidable.

    One record for the base is the ordinary case. Where an annotation set holds several versions of
    one accession, the rows belong to exactly one of them, so they go to the version GTEx names and
    to no record at all otherwise — attaching them to each would report one measurement as several.
    """
    if len(records) == 1:
        return records[0].accession
    versions = {row.transcript for row in rows}
    matching = [record.accession for record in records if record.accession in versions]
    return matching[0] if len(matching) == 1 else None


@dataclasses.dataclass(frozen=True)
class _TranscriptInventory:
    """The gene's transcripts against the assessed exon, with what each half of the join missed.

    Attributes:
        entries: One entry per transcript a verdict was computed for, annotation sets in order.
        denominators: One per annotation set read, so a grouping of `entries` has a stated size.
        without_structure: GTEx transcript ids whose rows reached no entry.
        without_expression: Ensembl-named entries carrying no expression row.
    """

    entries: list[transcript_pb2.TranscriptExonMembership]
    denominators: list[transcript_pb2.TranscriptInventoryDenominator]
    without_structure: list[str]
    without_expression: list[str]


def _inventory_entry(
    record: transcript_structure.GeneTranscript,
    spans: Sequence[transcript_structure.ExonSpan],
    *,
    namespace: transcript_pb2.TranscriptNamespace,
    exon: transcript_pb2.Exon,
    rows: Sequence[transcript_pb2.TranscriptExpression],
    assessed_base: str,
) -> transcript_pb2.TranscriptExonMembership:
    membership, overlapping = _exon_membership(spans, exon)
    return transcript_pb2.TranscriptExonMembership(
        transcript=record.accession,
        accession_base=record.base,
        namespace=namespace,
        mane_select=record.mane_select,
        mane_plus_clinical=record.mane_plus_clinical,
        coding=record.coding,
        membership=membership,
        expression=rows,
        assessed_transcript=record.base == assessed_base,
        overlapping_exons=[evidence_pb2.GenomicSpan(start=span.start, end=span.end) for span in overlapping],
    )


def _require_one_gene(
    structure: transcript_structure.TranscriptStructureResult,
    annotation_sets: Sequence[transcript_structure.GeneTranscriptsResult],
) -> None:
    """Hold the request's gene and transcript to the same gene.

    Both sides normalise a previous symbol onto the current one, so a disagreement is the request
    pairing a transcript with another gene's symbol. Answered rather than refused, it would return
    the named gene's transcripts, none of which reaches the assessed exon.

    Raises:
        errors.InvalidRequestError: If an annotation set is for a different gene than the transcript.
    """
    for annotation_set in annotation_sets:
        if annotation_set.gene != structure.gene:
            raise errors.InvalidRequestError(
                f'AssessExonRelevance: the request names gene {annotation_set.gene!r} and transcript '
                f'{structure.transcript!r}, which VariantValidator holds for {structure.gene!r}'
            )


def _transcript_inventory(
    annotation_sets: Sequence[transcript_structure.GeneTranscriptsResult],
    *,
    exon: transcript_pb2.Exon,
    chromosome_accession: str,
    assessed_transcript: str,
    isoforms: gtex.GtexResult,
) -> _TranscriptInventory:
    """Classify every transcript of the gene against the assessed exon, in both namespaces.

    The verdict is an interval test on genomic coordinates, so an Ensembl model is decided in its own
    namespace and needs no RefSeq pairing. Expression rides along where GTEx keys a row by the
    transcript's unversioned accession; the two directions of partial coverage are named rather than
    left as empty lists.

    Args:
        annotation_sets: One result per annotation set, in the order the entries are reported.
        exon: The assessed exon, whose genomic span every verdict is decided against.
        chromosome_accession: The genomic accession that span is on; a record with no alignment there
            has no comparable interval.
        assessed_transcript: The requested accession, matched on its base to mark its own entry.
        isoforms: The gene's GTEx expression.
    """
    expression = _expression_by_base(isoforms.medians)
    assessed_base = hgvs.accession_base(assessed_transcript)
    entries: list[transcript_pb2.TranscriptExonMembership] = []
    denominators: list[transcript_pb2.TranscriptInventoryDenominator] = []
    without_expression: list[str] = []
    attached: set[str] = set()
    for result in annotation_sets:
        namespace = _INVENTORY_NAMESPACES[result.annotation_set]
        not_classified = list(result.unreadable)
        by_base: dict[str, list[transcript_structure.GeneTranscript]] = {}
        for record in result.transcripts:
            by_base.setdefault(record.base, []).append(record)
        considered = 0
        for record in result.transcripts:
            spans = record.exons_on(chromosome_accession)
            if spans is None:
                not_classified.append(record.accession)
                continue
            rows = expression.get(record.base, [])
            owner = _expression_owner(by_base[record.base], rows) if rows else None
            entries.append(
                _inventory_entry(
                    record,
                    spans,
                    namespace=namespace,
                    exon=exon,
                    rows=rows if owner == record.accession else (),
                    assessed_base=assessed_base,
                )
            )
            considered += 1
            if owner == record.accession:
                attached.add(record.base)
            elif namespace == _EXPRESSION_NAMESPACE:
                without_expression.append(record.accession)
        denominators.append(
            transcript_pb2.TranscriptInventoryDenominator(
                namespace=namespace,
                transcripts_considered=considered,
                transcripts_not_classified=not_classified,
            )
        )
    return _TranscriptInventory(
        entries=entries,
        denominators=denominators,
        # Keyed on the rows an entry actually carries, not on the bases classified: a row whose
        # owner among several same-base records is undecidable reaches no entry, and would
        # otherwise be named in neither direction.
        without_structure=[
            transcript for transcript in isoforms.transcript_ids if hgvs.accession_base(transcript) not in attached
        ],
        without_expression=without_expression,
    )


def _gtex_isoforms(isoforms: gtex.GtexResult, *, include_detail: bool) -> dict[str, object]:
    """The GTEx grid for ``raw``: the rows themselves, or the count withheld in their place.

    ``tissues_without_rows`` rides along either way: a requested tissue GTEx returned nothing for is
    missing from ``gtex_expression``, which would otherwise read as "not expressed there".
    """
    detail: dict[str, object] = {'tissues_without_rows': isoforms.tissues_without_rows}
    if include_detail:
        detail['rows'] = isoforms.rows
    else:
        detail['rows_withheld'] = len(isoforms.rows)
    return detail


def _located_position(
    structure: transcript_structure.TranscriptStructureResult, request: transcript_pb2.GetStructureRequest
) -> transcript_pb2.TranscriptPosition | None:
    """The queried position located in the exon table, or None when the request carried none."""
    match request.WhichOneof('query_position'):
        case 'cds_position':
            return transcript_structure.position_in_transcript(structure, cds_position=request.cds_position)
        case 'genomic_position':
            return transcript_structure.position_in_transcript(structure, genomic_position=request.genomic_position)
        case _:
            return None
