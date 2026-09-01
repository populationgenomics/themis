"""VariantValidator gene2transcripts adapter: exon tables, and queries over them.

VariantValidator aligns each transcript to both assemblies and publishes the alignment as an exon
table — per exon the genomic span, the mature-transcript (n.) span, and the alignment cigar —
alongside the transcript's CDS bounds in n. coordinates. That pair is what turns a c. position into
an exon and a distance-to-splice-site, so this adapter derives each exon's c. span from it and
answers positional queries against the table.

Two projections of the one endpoint. ``fetch_transcript_structure`` answers a versioned accession
with that accession's full table, superseded versions included. ``fetch_gene_transcripts`` answers a
gene symbol with every transcript of its CURRENT set in both annotation namespaces, keeping the
genomic exon spans alone — the coordinates an exon-membership test is decided on, in whichever
namespace the transcript is named, with no crosswalk.

The endpoint carries no version metadata of its own, so the service's ``/hello/`` metadata (the
VariantValidator, VVDB, and VVTA alignment-database versions) is fetched alongside and stamped as
``dataset_versions``: an exon table is only reproducible against the alignment release it came from.

Genomic<->transcript projection is linear within an exon, which holds only for an ungapped alignment.
An exon whose cigar is anything but a whole-exon match raises rather than projecting through the gap.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import re
import urllib.parse
from collections.abc import Mapping, Sequence

import httpx2

from themis.evidence.models import evidence_pb2
from themis.rpc import transcript_pb2
from themis.services.evidence import errors, hgvs

_BASE_URL = 'https://rest.variantvalidator.org'
_SOURCE = 'VariantValidator gene2transcripts'
_TIMEOUT_SECONDS = 60.0
_VERSION_KEYS = ('variantvalidator_version', 'vvdb_version', 'vvta_version')
# The endpoint's two annotation sets, in the order an inventory reports them.
_ANNOTATION_SETS = ('refseq', 'ensembl')
# A whole-exon match, e.g. "182=". Anything else carries an alignment indel.
_UNGAPPED_CIGAR = re.compile(r'\d+=')


@dataclasses.dataclass(frozen=True)
class TranscriptStructureResult:
    """One transcript's exon table on one assembly, with its CDS bounds.

    Attributes:
        transcript: The versioned RefSeq accession the table is for.
        gene: The HGNC gene symbol.
        chromosome_accession: The RefSeq genomic accession the exon coordinates are on
            (e.g. ``NC_000017.11``).
        strand: ``1`` or ``-1``.
        mane_select: Whether the transcript is MANE Select.
        mane_plus_clinical: Whether it is MANE Plus Clinical.
        transcript_length: The mature transcript's length in nt.
        cds_transcript_start: The n. position of ``c.1``.
        cds_transcript_end: The n. position of the termination codon's last base.
        exons: One `Exon` per exon, in transcript order.
        exon_cigars: Exon number -> the alignment cigar. Only an ungapped exon projects a genomic
            coordinate linearly onto the transcript, and a balanced indel pair leaves the two spans
            the same length, so the cigar is what says whether it does.
        raw: The VariantValidator transcript record verbatim, for the proto `Struct`.
        source: Provenance source label.
        dataset_versions: The VariantValidator, VVDB and VVTA versions behind the alignment, one
            element each.
        query: The exact request URL issued, for replay.
    """

    transcript: str
    gene: str
    chromosome_accession: str
    strand: int
    mane_select: bool
    mane_plus_clinical: bool
    transcript_length: int
    cds_transcript_start: int
    cds_transcript_end: int
    exons: list[transcript_pb2.Exon]
    exon_cigars: Mapping[int, str]
    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str

    @property
    def coding_length(self) -> int:
        """CDS nt, termination codon included."""
        return self.cds_transcript_end - self.cds_transcript_start + 1


def _int(entry: Mapping[str, object], key: str, *, context: str) -> int:
    value = entry.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f'VariantValidator {context} has no integer {key!r}: {value!r}')
    return value


def _exon(entry: Mapping[str, object], *, cds_start: int, cds_end: int) -> transcript_pb2.Exon:
    transcript_start = _int(entry, 'transcript_start', context='exon')
    transcript_end = _int(entry, 'transcript_end', context='exon')
    genomic_start = _int(entry, 'genomic_start', context='exon')
    genomic_end = _int(entry, 'genomic_end', context='exon')
    if genomic_end < genomic_start:
        raise ValueError(f'VariantValidator exon span {genomic_start}-{genomic_end} descends')
    coding_first = max(transcript_start, cds_start)
    coding_last = min(transcript_end, cds_end)
    exon = transcript_pb2.Exon(
        number=_int(entry, 'exon_number', context='exon'),
        genomic_start=genomic_start,
        genomic_end=genomic_end,
        transcript_start=transcript_start,
        transcript_end=transcript_end,
        length=transcript_end - transcript_start + 1,
        coding_length=max(0, coding_last - coding_first + 1),
    )
    exon.frame_shift_if_skipped = exon.coding_length % 3
    if exon.coding_length:
        exon.cds_start = coding_first - cds_start + 1
        exon.cds_end = coding_last - cds_start + 1
    return exon


def _entry(payload: object, transcript: str) -> Mapping[str, object]:
    """The response's single gene object; the service reports an unresolvable query inside it."""
    entry = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(entry, Mapping):
        raise ValueError(f'VariantValidator gene2transcripts returned a non-object payload for {transcript!r}')
    # An accession or symbol the service cannot resolve comes back 200 with an `error` string.
    if (reported := entry.get('error')) is not None:
        raise errors.UnknownVariantError(f'VariantValidator does not recognise {transcript!r}: {reported}')
    return entry


def _transcript_record(entry: Mapping[str, object], transcript: str) -> Mapping[str, object]:
    """The requested accession's record, or a raise when the service holds none.

    The endpoint answers a transcript query with the whole gene's transcript list, so the record is
    selected by accession rather than taken from the head.
    """
    records = entry.get('transcripts')
    if not isinstance(records, Sequence) or isinstance(records, str):
        raise ValueError(f'VariantValidator gene2transcripts returned no transcripts list for {transcript!r}')
    for record in records:
        if isinstance(record, Mapping) and record.get('reference') == transcript:
            return record
    raise errors.UnknownVariantError(f'VariantValidator holds no transcript record for {transcript!r}')


def _genomic_span(record: Mapping[str, object], transcript: str, genome_build: str) -> tuple[str, Mapping[str, object]]:
    """The single genomic alignment in the record, keyed by its RefSeq chromosome accession.

    The request pins one assembly, so exactly one span is expected.
    """
    spans = record.get('genomic_spans')
    if not isinstance(spans, Mapping):
        raise ValueError(f'VariantValidator record for {transcript!r} has no genomic_spans object')
    if len(spans) != 1:
        raise errors.UnknownVariantError(
            f'VariantValidator has no single {genome_build} alignment for {transcript!r} (got {sorted(spans)})'
        )
    accession, span = next(iter(spans.items()))
    if not isinstance(accession, str) or not isinstance(span, Mapping):
        raise ValueError(f'VariantValidator genomic_spans entry for {transcript!r} is malformed')
    return accession, span


def _exon_entries(span: Mapping[str, object], transcript: str) -> list[Mapping[str, object]]:
    """The alignment's exon entries in transcript order, with the numbering checked for gaps."""
    structure = span.get('exon_structure')
    if not isinstance(structure, Sequence) or isinstance(structure, str) or not structure:
        raise ValueError(f'VariantValidator alignment for {transcript!r} carries no exon_structure')
    entries = [entry for entry in structure if isinstance(entry, Mapping)]
    if len(entries) != len(structure):
        raise ValueError(f'VariantValidator exon_structure for {transcript!r} has a non-object entry')
    ordered = sorted(entries, key=lambda entry: _int(entry, 'exon_number', context='exon'))
    numbers = [_int(entry, 'exon_number', context='exon') for entry in ordered]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f'VariantValidator exon numbering for {transcript!r} is not 1..{len(numbers)}: {numbers}')
    return ordered


def _tiling(exons: Sequence[transcript_pb2.Exon], transcript_length: int, transcript: str) -> list[transcript_pb2.Exon]:
    """The exon table, checked to tile the mature transcript exactly.

    Every derived answer assumes it: a c. position is located by exon n. span, the aberrant
    transcript of a skip is built by excising one, and the NMD margin sums exon lengths. A table with
    a gap, an overlap, or a short tail yields a plausible but wrong coordinate at each of those.
    """
    if exons[0].transcript_start != 1:
        raise ValueError(f'{transcript} exon 1 starts at n.{exons[0].transcript_start}, expected n.1')
    for prior, exon in itertools.pairwise(exons):
        if prior.transcript_end + 1 != exon.transcript_start:
            raise ValueError(
                f'{transcript} exon {exon.number} starts at n.{exon.transcript_start}, '
                f'but exon {prior.number} ends at n.{prior.transcript_end}'
            )
    if exons[-1].transcript_end != transcript_length:
        raise ValueError(
            f'{transcript} exons span n.1-{exons[-1].transcript_end} but the transcript is {transcript_length} nt'
        )
    return list(exons)


def _cds_bounds(record: Mapping[str, object], transcript: str, transcript_length: int) -> tuple[int, int]:
    start = _int(record, 'coding_start', context=f'transcript {transcript!r}')
    end = _int(record, 'coding_end', context=f'transcript {transcript!r}')
    if not 1 <= start <= end <= transcript_length:
        raise ValueError(f'{transcript} CDS n.{start}-{end} is not inside the {transcript_length} nt transcript')
    return start, end


def _mane_flags(record: Mapping[str, object], transcript: str) -> tuple[bool, bool]:
    """The two MANE flags, both required: an absent one would read as "not MANE" (ExonRelevance "Few")."""
    annotations = record.get('annotations')
    if not isinstance(annotations, Mapping):
        raise ValueError(f'VariantValidator record for {transcript!r} has no annotations object')
    flags = []
    for key in ('mane_select', 'mane_plus_clinical'):
        value = annotations.get(key)
        if not isinstance(value, bool):
            raise ValueError(f'VariantValidator annotations for {transcript!r} has no boolean {key!r}: {value!r}')
        flags.append(value)
    return flags[0], flags[1]


def _strand(span: Mapping[str, object], transcript: str) -> int:
    """The alignment orientation, constrained: every genomic projection branches on its sign."""
    orientation = _int(span, 'orientation', context='genomic span')
    if orientation not in (1, -1):
        raise ValueError(f'VariantValidator alignment for {transcript!r} has orientation {orientation}, expected 1/-1')
    return orientation


def _dataset_versions(metadata: object) -> tuple[str, ...]:
    """All three upstream versions, one element each; a missing one raises rather than shortening the list."""
    if not isinstance(metadata, Mapping):
        raise ValueError('VariantValidator /hello/ returned no metadata object')
    versions = []
    for key in _VERSION_KEYS:
        value = metadata.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f'VariantValidator /hello/ metadata has no {key!r}: {value!r}')
        versions.append(value)
    return tuple(versions)


def parse_transcript_structure(
    payload: object, *, transcript: str, genome_build: str, dataset_versions: tuple[str, ...], query: str
) -> TranscriptStructureResult:
    """Parse a gene2transcripts response into the exon table for one transcript.

    Args:
        payload: The decoded gene2transcripts response.
        transcript: The versioned RefSeq accession requested — the record is selected by it.
        genome_build: The assembly requested, for the error message when no alignment is present.
        dataset_versions: The upstream versions from `/hello/`, carried into provenance.
        query: The exact request URL issued, carried into provenance for replay.

    Returns:
        The parsed `TranscriptStructureResult`.

    Raises:
        errors.UnknownVariantError: If the service does not recognise the accession, holds no record
            for it, or has no alignment for the requested assembly.
        ValueError: If the record is structurally malformed, carries no CDS bounds (a non-coding
            transcript, whose c. coordinates do not exist), or its exon table does not tile the
            transcript.
    """
    entry = _entry(payload, transcript)
    record = _transcript_record(entry, transcript)
    accession, span = _genomic_span(record, transcript, genome_build)
    transcript_length = _int(record, 'length', context=f'transcript {transcript!r}')
    cds_start, cds_end = _cds_bounds(record, transcript, transcript_length)
    mane_select, mane_plus_clinical = _mane_flags(record, transcript)
    entries = _exon_entries(span, transcript)
    exons = [_exon(exon, cds_start=cds_start, cds_end=cds_end) for exon in entries]
    return TranscriptStructureResult(
        transcript=transcript,
        gene=_gene(entry, transcript),
        chromosome_accession=accession,
        strand=_strand(span, transcript),
        mane_select=mane_select,
        mane_plus_clinical=mane_plus_clinical,
        transcript_length=transcript_length,
        cds_transcript_start=cds_start,
        cds_transcript_end=cds_end,
        exons=_tiling(exons, transcript_length, transcript),
        exon_cigars={
            _int(entry, 'exon_number', context='exon'): cigar
            for entry in entries
            if isinstance(cigar := entry.get('cigar'), str)
        },
        raw=dict(record),
        source=_SOURCE,
        dataset_versions=dataset_versions,
        query=query,
    )


def _gene(entry: Mapping[str, object], transcript: str) -> str:
    symbol = entry.get('current_symbol')
    if not isinstance(symbol, str) or not symbol:
        raise ValueError(f'VariantValidator gene2transcripts response for {transcript!r} has no current_symbol')
    return symbol


def _exon_containing_transcript_position(result: TranscriptStructureResult, position: int) -> transcript_pb2.Exon:
    for exon in result.exons:
        if exon.transcript_start <= position <= exon.transcript_end:
            return exon
    raise errors.InvalidRequestError(
        f'transcript position {position} is outside {result.transcript} (1-{result.transcript_length})'
    )


def _cds_to_transcript_position(result: TranscriptStructureResult, cds_position: int) -> int:
    """The n. position of a c. coordinate. c. numbering skips 0: c.-1 is the base before c.1."""
    if cds_position == 0:
        raise errors.InvalidRequestError('c. numbering has no position 0')
    offset = cds_position - 1 if cds_position > 0 else cds_position
    return result.cds_transcript_start + offset


def _cds_position(result: TranscriptStructureResult, transcript_position: int) -> int | None:
    if not result.cds_transcript_start <= transcript_position <= result.cds_transcript_end:
        return None
    return transcript_position - result.cds_transcript_start + 1


def _exonic(
    result: TranscriptStructureResult, exon: transcript_pb2.Exon, position: int
) -> transcript_pb2.TranscriptPosition:
    located = transcript_pb2.TranscriptPosition(
        exon=exon.number,
        nt_from_exon_start=position - exon.transcript_start + 1,
        nt_to_exon_end=exon.transcript_end - position + 1,
    )
    cds_position = _cds_position(result, position)
    if cds_position is not None:
        located.cds_position = cds_position
    return located


def _transcript_position_of_genomic(result: TranscriptStructureResult, exon: transcript_pb2.Exon, genomic: int) -> int:
    """Project a genomic coordinate inside `exon` onto its n. coordinate.

    Raises:
        ValueError: If the exon's alignment is gapped, so the projection is not linear. A balanced
            indel pair leaves the two spans the same length, so the cigar is the check, not the
            lengths.
    """
    cigar = result.exon_cigars.get(exon.number)
    if cigar is None or _UNGAPPED_CIGAR.fullmatch(cigar) is None:
        raise ValueError(
            f'{result.transcript} exon {exon.number} has cigar {cigar!r}, not an ungapped match; '
            'a genomic position inside it does not project linearly onto the transcript'
        )
    if result.strand == 1:
        return exon.transcript_start + (genomic - exon.genomic_start)
    return exon.transcript_start + (exon.genomic_end - genomic)


def _intronic(result: TranscriptStructureResult, genomic: int) -> transcript_pb2.TranscriptPosition:
    """Locate a genomic position in the intron between two consecutive exons.

    Raises:
        errors.InvalidRequestError: If the position lies outside the transcript's genomic span.
    """
    for upstream, downstream in itertools.pairwise(result.exons):
        if result.strand == 1:
            from_start, to_end = genomic - upstream.genomic_end, downstream.genomic_start - genomic
        else:
            from_start, to_end = upstream.genomic_start - genomic, genomic - downstream.genomic_end
        if from_start >= 1 and to_end >= 1:
            return transcript_pb2.TranscriptPosition(
                intron=upstream.number, nt_from_intron_start=from_start, nt_to_intron_end=to_end
            )
    span = (result.exons[0].genomic_start, result.exons[-1].genomic_end)
    raise errors.InvalidRequestError(
        f'genomic position {genomic} is outside {result.transcript} on {result.chromosome_accession} '
        f'({min(span)}-{max(span)})'
    )


def position_in_transcript(
    result: TranscriptStructureResult, *, cds_position: int | None = None, genomic_position: int | None = None
) -> transcript_pb2.TranscriptPosition:
    """Locate one position in the exon table.

    Exactly one coordinate is taken. A c. position is exonic by construction; a genomic position may
    fall in an intron, in which case the flanking intron and its two boundary distances are returned.

    Args:
        result: The transcript's parsed exon table.
        cds_position: A c. coordinate (negative = 5'UTR).
        genomic_position: A 1-based coordinate on the table's assembly.

    Returns:
        The `TranscriptPosition`: exon-relative distances, or intron-relative ones.

    Raises:
        errors.InvalidRequestError: If neither or both coordinates are given, or the position lies
            outside the transcript.
        ValueError: If a genomic position falls inside a gapped exon alignment.
    """
    if cds_position is not None and genomic_position is None:
        transcript_position = _cds_to_transcript_position(result, cds_position)
        return _exonic(result, _exon_containing_transcript_position(result, transcript_position), transcript_position)
    if genomic_position is not None and cds_position is None:
        for exon in result.exons:
            if exon.genomic_start <= genomic_position <= exon.genomic_end:
                return _exonic(result, exon, _transcript_position_of_genomic(result, exon, genomic_position))
        return _intronic(result, genomic_position)
    raise errors.InvalidRequestError('locating a position takes exactly one of cds_position / genomic_position')


def _genomic_of_transcript_position(result: TranscriptStructureResult, exon: transcript_pb2.Exon, position: int) -> int:
    """Project an n. coordinate inside `exon` onto its genomic coordinate.

    Raises:
        ValueError: If the exon's alignment is gapped, so the projection is not linear — the mirror
            of `_transcript_position_of_genomic`, and gapped for the same reason.
    """
    cigar = result.exon_cigars.get(exon.number)
    if cigar is None or _UNGAPPED_CIGAR.fullmatch(cigar) is None:
        raise ValueError(
            f'{result.transcript} exon {exon.number} has cigar {cigar!r}, not an ungapped match; '
            'a transcript position inside it does not project linearly onto the genome'
        )
    offset = position - exon.transcript_start
    return exon.genomic_start + offset if result.strand == 1 else exon.genomic_end - offset


def genomic_span_of_cds_range(result: TranscriptStructureResult, start: int, end: int) -> evidence_pb2.GenomicSpan:
    """The genomic interval a c. range covers, ascending on both strands.

    The two endpoints are projected exon by exon and the interval spans them, so a range crossing an
    exon boundary yields one interval that also covers the intervening intron. That is a superset of
    the exonic bases named, never a subset, and a caller placing what falls inside it reads each
    record's own coordinates.

    Args:
        result: The transcript's parsed exon table.
        start: The range's first c. coordinate (negative = 5'UTR).
        end: Its last; plain integer order is transcript order across the two representable regions,
            since every 5'UTR coordinate is negative and every CDS one positive.

    Returns:
        The `GenomicSpan`, `start <= end` whichever strand the transcript is on.

    Raises:
        errors.InvalidRequestError: If either coordinate is 0, if `end` precedes `start`, if either
            lies past the termination codon, or if either lies outside the transcript.
        ValueError: If an endpoint falls inside a gapped exon alignment.
    """
    if end < start:
        raise errors.InvalidRequestError(f'a c. range ascends: got c.{start} to c.{end}')
    # A positive number past the CDS is a 3'UTR base, which HGVS numbers `c.*n` and this signed form
    # cannot express — so it would silently answer about a region the caller did not name.
    if end > result.coding_length:
        raise errors.InvalidRequestError(
            f'c.{end} is past the termination codon of {result.transcript} (CDS 1-{result.coding_length}); '
            "a 3'UTR base is numbered c.*n, which this coordinate form cannot carry"
        )
    projected = [
        _genomic_of_transcript_position(
            result,
            _exon_containing_transcript_position(result, transcript_position),
            transcript_position,
        )
        for transcript_position in (
            _cds_to_transcript_position(result, start),
            _cds_to_transcript_position(result, end),
        )
    ]
    return evidence_pb2.GenomicSpan(start=min(projected), end=max(projected))


async def fetch_transcript_structure(
    transcript: str, genome_build: str, *, http_client: httpx2.AsyncClient
) -> TranscriptStructureResult:
    """Fetch one RefSeq transcript's exon table on one assembly.

    Args:
        transcript: The versioned RefSeq accession (e.g. ``NM_001042492.3``). It scopes both the gene
            query and the returned transcript list, so the response carries that transcript alone.
        genome_build: ``GRCh38`` or ``GRCh37``.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The parsed `TranscriptStructureResult`.

    Raises:
        errors.InvalidRequestError: If VariantValidator refuses the accession (a non-429 4xx). An
            accession it cannot resolve comes back 200 with an ``error`` string instead.
        errors.UnknownVariantError: If VariantValidator holds no record for the accession or no
            alignment on the assembly.
        httpx2.HTTPStatusError: If either request returns a 429 or a 5xx, or if `/hello/` — which
            carries no caller input, so its failure is never a refusal — returns any non-2xx.
        ValueError: If a response is structurally malformed.
    """
    quoted = urllib.parse.quote(transcript, safe='')
    url = f'{_BASE_URL}/VariantValidator/tools/gene2transcripts_v2/{quoted}/{quoted}/refseq/{genome_build}'
    params = {'content-type': 'application/json', 'show_exon_info': 'true'}
    structure, metadata = await asyncio.gather(
        http_client.get(url, params=params, headers={'Accept': 'application/json'}, timeout=_TIMEOUT_SECONDS),
        http_client.get(f'{_BASE_URL}/hello/', headers={'Accept': 'application/json'}, timeout=_TIMEOUT_SECONDS),
    )
    errors.raise_for_status(structure, upstream=_SOURCE, subject=f'{transcript!r} on {genome_build}')
    metadata.raise_for_status()
    return parse_transcript_structure(
        structure.json(),
        transcript=transcript,
        genome_build=genome_build,
        dataset_versions=_dataset_versions(metadata.json().get('metadata')),
        query=str(structure.request.url),
    )


@dataclasses.dataclass(frozen=True)
class ExonSpan:
    """One exon's genomic interval, ascending on both strands (as VariantValidator publishes it).

    Raises:
        ValueError: If the interval descends. Every interval test here compares a `start` against a
            `start`, so a descending one returns a plausible verdict rather than failing.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f'exon span {self.start}-{self.end} descends')


@dataclasses.dataclass(frozen=True)
class GeneTranscript:
    """One transcript of a gene's annotation set, reduced to what an exon-membership test reads.

    The genomic exon spans alone: no n./c. arithmetic, and no exon numbering, so an Ensembl model the
    RefSeq-only spine cannot take is still decidable and a record whose numbering the c.-coordinate
    parse would reject is still classified.

    Attributes:
        accession: The versioned accession, as its annotation set states it.
        mane_select: Whether this set flags the transcript MANE Select.
        mane_plus_clinical: Whether it flags it MANE Plus Clinical.
        coding: Whether the record carries CDS bounds.
        alignments: Genomic accession -> that alignment's exon spans, in transcript order. A record
            can align to several (a chromosome and a patch scaffold); the caller selects the one its
            assessed interval is on. Never empty, and no value is empty.
    """

    accession: str
    mane_select: bool
    mane_plus_clinical: bool
    coding: bool
    alignments: Mapping[str, list[ExonSpan]]

    @property
    def base(self) -> str:
        """The accession without its version."""
        return hgvs.accession_base(self.accession)

    def exons_on(self, chromosome_accession: str) -> list[ExonSpan] | None:
        """This transcript's exon spans on one genomic accession, or `None` if it has no alignment there."""
        return self.alignments.get(chromosome_accession)


@dataclasses.dataclass(frozen=True)
class GeneTranscriptsResult:
    """One annotation set's transcripts for one gene, with the records it could not read.

    Attributes:
        gene: The gene's current HGNC symbol, as VariantValidator resolved the queried one.
        annotation_set: ``refseq`` or ``ensembl``.
        transcripts: One `GeneTranscript` per record whose identity, flags and alignment all parsed.
        unreadable: The accessions of the records where one of those did not — no alignment, MANE
            flags that are not booleans, one CDS bound of two, an unusable exon table. Reported
            rather than raised on, so one such record does not decide the whole query.
        source: Provenance source label.
        dataset_versions: The VariantValidator, VVDB and VVTA versions behind the alignments, one
            element each.
        query: The exact request URL issued, for replay.
    """

    gene: str
    annotation_set: str
    transcripts: list[GeneTranscript]
    unreadable: list[str]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def _accession(record: Mapping[str, object], gene: str) -> str:
    """The record's accession; without it the record cannot even be named as unreadable."""
    accession = record.get('reference')
    if not isinstance(accession, str) or not accession:
        raise ValueError(f'VariantValidator gene2transcripts record for {gene!r} has no reference accession')
    return accession


def _coding(record: Mapping[str, object]) -> bool | None:
    """Whether the record carries CDS bounds; `None` when it states one bound of two."""
    start, end = record.get('coding_start'), record.get('coding_end')
    # `bool` is an `int` subclass, and `True` is not a coordinate.
    if isinstance(start, int) and isinstance(end, int) and not isinstance(start, bool) and not isinstance(end, bool):
        return True
    if start is None and end is None:
        return False
    return None


def _optional_mane_flags(record: Mapping[str, object]) -> tuple[bool, bool] | None:
    """The two MANE flags, or `None` when either is absent or not a boolean."""
    annotations = record.get('annotations')
    if not isinstance(annotations, Mapping):
        return None
    flags = [annotations.get(key) for key in ('mane_select', 'mane_plus_clinical')]
    if not all(isinstance(flag, bool) for flag in flags):
        return None
    return bool(flags[0]), bool(flags[1])


def _exon_spans(span: object) -> list[ExonSpan] | None:
    """One alignment's exon spans, or `None` when the alignment carries no usable exon table."""
    if not isinstance(span, Mapping):
        return None
    structure = span.get('exon_structure')
    if not isinstance(structure, Sequence) or isinstance(structure, str) or not structure:
        return None
    spans = []
    for entry in structure:
        if not isinstance(entry, Mapping):
            return None
        start, end = entry.get('genomic_start'), entry.get('genomic_end')
        # `bool` is an `int` subclass, and `True` is not a coordinate.
        if not isinstance(start, int) or not isinstance(end, int) or isinstance(start, bool) or isinstance(end, bool):
            return None
        if end < start:
            return None
        spans.append(ExonSpan(start=start, end=end))
    return spans


def _alignments(record: Mapping[str, object]) -> dict[str, list[ExonSpan]] | None:
    """Every genomic alignment the record carries, or `None` when none of them is usable."""
    spans = record.get('genomic_spans')
    if not isinstance(spans, Mapping):
        return None
    usable = {
        accession: exons
        for accession, span in spans.items()
        if isinstance(accession, str) and (exons := _exon_spans(span)) is not None
    }
    return usable or None


def _gene_transcript(record: Mapping[str, object], gene: str) -> tuple[str, GeneTranscript | None]:
    """One record as a `GeneTranscript`, or its accession alone when a field it needs is unreadable."""
    accession = _accession(record, gene)
    flags = _optional_mane_flags(record)
    coding = _coding(record)
    alignments = _alignments(record)
    if flags is None or coding is None or alignments is None:
        return accession, None
    return accession, GeneTranscript(
        accession=accession,
        mane_select=flags[0],
        mane_plus_clinical=flags[1],
        coding=coding,
        alignments=alignments,
    )


def parse_gene_transcripts(
    payload: object, *, gene: str, annotation_set: str, dataset_versions: tuple[str, ...], query: str
) -> GeneTranscriptsResult:
    """Parse a gene-scoped gene2transcripts response into one annotation set's transcripts.

    Args:
        payload: The decoded gene2transcripts response.
        gene: The HGNC symbol requested, for the error messages.
        annotation_set: ``refseq`` or ``ensembl`` — which set the response is.
        dataset_versions: The upstream versions from `/hello/`, carried into provenance.
        query: The exact request URL issued, carried into provenance for replay.

    Returns:
        The parsed `GeneTranscriptsResult`.

    Raises:
        errors.UnknownVariantError: If VariantValidator does not recognise the symbol.
        ValueError: If the response itself is malformed — no transcripts list, a non-object record,
            or a record with no accession. A record that is well-formed but carries a field this
            parse cannot read is reported in ``unreadable`` instead.
    """
    entry = _entry(payload, gene)
    records = entry.get('transcripts')
    if not isinstance(records, Sequence) or isinstance(records, str):
        raise ValueError(f'VariantValidator gene2transcripts returned no transcripts list for {gene!r}')
    transcripts: list[GeneTranscript] = []
    unreadable: list[str] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError(f'VariantValidator gene2transcripts for {gene!r} has a non-object transcript record')
        accession, parsed = _gene_transcript(record, gene)
        if parsed is None:
            unreadable.append(accession)
        else:
            transcripts.append(parsed)
    return GeneTranscriptsResult(
        gene=_gene(entry, gene),
        annotation_set=annotation_set,
        transcripts=transcripts,
        unreadable=unreadable,
        source=_SOURCE,
        dataset_versions=dataset_versions,
        query=query,
    )


async def fetch_gene_transcripts(
    gene: str, genome_build: str, *, http_client: httpx2.AsyncClient
) -> list[GeneTranscriptsResult]:
    """Fetch every transcript VariantValidator holds for a gene, one result per annotation set.

    Both sets are fetched: the RefSeq one is the spine's own namespace, and the Ensembl one is where
    an isoform GTEx measures but RefSeq does not curate becomes testable.

    Args:
        gene: HGNC gene symbol.
        genome_build: ``GRCh38`` or ``GRCh37`` — the assembly the exon spans are on.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        One `GeneTranscriptsResult` per annotation set, RefSeq first.

    Raises:
        errors.InvalidRequestError: If VariantValidator refuses the symbol (a non-429 4xx). A symbol
            it cannot resolve comes back 200 with an ``error`` string instead.
        errors.UnknownVariantError: If VariantValidator does not recognise the symbol.
        httpx2.HTTPStatusError: If a request returns a 429 or a 5xx, or if `/hello/` — which carries
            no caller input, so its failure is never a refusal — returns any non-2xx.
        ValueError: If a response is structurally malformed.
    """
    params = {'content-type': 'application/json', 'show_exon_info': 'true'}
    quoted = urllib.parse.quote(gene, safe='')
    urls = [
        f'{_BASE_URL}/VariantValidator/tools/gene2transcripts_v2/{quoted}/all/{annotation_set}/{genome_build}'
        for annotation_set in _ANNOTATION_SETS
    ]
    metadata, *responses = await asyncio.gather(
        http_client.get(f'{_BASE_URL}/hello/', headers={'Accept': 'application/json'}, timeout=_TIMEOUT_SECONDS),
        *(
            http_client.get(url, params=params, headers={'Accept': 'application/json'}, timeout=_TIMEOUT_SECONDS)
            for url in urls
        ),
    )
    for annotation_set, response in zip(_ANNOTATION_SETS, responses, strict=True):
        errors.raise_for_status(response, upstream=_SOURCE, subject=f'{gene!r} {annotation_set} on {genome_build}')
    metadata.raise_for_status()
    dataset_versions = _dataset_versions(metadata.json().get('metadata'))
    return [
        parse_gene_transcripts(
            response.json(),
            gene=gene,
            annotation_set=annotation_set,
            dataset_versions=dataset_versions,
            query=str(response.request.url),
        )
        for annotation_set, response in zip(_ANNOTATION_SETS, responses, strict=True)
    ]
