"""Tests for the transcript-structure adapter, against recorded fixtures via a mocked transport."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import itertools
import json
import pathlib
from collections.abc import Callable

import httpx
import pytest

from themis.rpc import transcript_pb2
from themis.services.evidence import errors
from themis.services.evidence.upstreams import transcript_structure

_FIXTURES = pathlib.Path(__file__).parent / 'fixtures'
# NF1 NM_001042492.3 on GRCh38: MANE Select, plus strand, 58 exons.
_NF1 = json.loads((_FIXTURES / 'transcript_structure.json').read_text())
# BRCA1 NM_007294.4 on GRCh38: the minus-strand case, where genomic coordinates descend as the
# transcript advances.
_BRCA1 = json.loads((_FIXTURES / 'transcript_structure_minus_strand.json').read_text())
_METADATA = {
    'metadata': {
        'variantvalidator_version': '4.0.1.dev7+gbdab9c72f',
        'vvdb_version': 'vvdb_2025_3',
        'vvta_version': 'vvta_2025_02',
    }
}


def _parsed(payload: object, transcript: str) -> transcript_structure.TranscriptStructureResult:
    return transcript_structure.parse_transcript_structure(
        payload, transcript=transcript, genome_build='GRCh38', dataset_versions=('vvta_2025_02',), query='q'
    )


def _nf1() -> transcript_structure.TranscriptStructureResult:
    return _parsed(_NF1, 'NM_001042492.3')


def _brca1() -> transcript_structure.TranscriptStructureResult:
    return _parsed(_BRCA1, 'NM_007294.4')


def _exon(result: transcript_structure.TranscriptStructureResult, number: int) -> transcript_pb2.Exon:
    return next(exon for exon in result.exons if exon.number == number)


def test_parses_the_transcripts_own_record_not_the_first_returned() -> None:
    result = _nf1()
    assert result.transcript == 'NM_001042492.3'
    assert result.gene == 'NF1'
    assert result.chromosome_accession == 'NC_000017.11'
    assert result.strand == 1
    assert result.mane_select
    assert not result.mane_plus_clinical


def test_nf1_exon_26_is_the_worked_case() -> None:
    """c.3496 is the last base of exon 26, and skipping the exon is out of frame."""
    exon = _exon(_nf1(), 26)
    assert (exon.genomic_start, exon.genomic_end) == (31232700, 31232881)
    assert (exon.cds_start, exon.cds_end) == (3315, 3496)
    assert exon.length == 182
    assert exon.frame_shift_if_skipped == 182 % 3 == 2


_Structure = Callable[[], transcript_structure.TranscriptStructureResult]


@pytest.mark.parametrize('parsed', [_nf1, _brca1])
def test_the_recorded_tables_satisfy_the_tiling_the_parser_enforces(parsed: _Structure) -> None:
    """Non-vacuous companion to the violating-input tests: the real payloads do parse and do tile."""
    result = parsed()
    assert [exon.number for exon in result.exons] == list(range(1, len(result.exons) + 1))
    boundaries = [(exon.transcript_start, exon.transcript_end) for exon in result.exons]
    assert boundaries[0][0] == 1
    assert boundaries[-1][1] == result.transcript_length
    assert all(prior_end + 1 == start for (_, prior_end), (start, _) in itertools.pairwise(boundaries))
    assert sum(exon.length for exon in result.exons) == result.transcript_length


@pytest.mark.parametrize('parsed', [_nf1, _brca1])
def test_the_coding_exons_tile_the_cds(parsed: _Structure) -> None:
    """The c. spans run 1..coding_length without gap or overlap, so a c. position lands in one exon."""
    result = parsed()
    coding = [exon for exon in result.exons if exon.coding_length]
    spans = [(exon.cds_start, exon.cds_end) for exon in coding]
    assert spans[0][0] == 1
    assert spans[-1][1] == result.coding_length
    assert all(prior_end + 1 == start for (_, prior_end), (start, _) in itertools.pairwise(spans))
    assert sum(exon.coding_length for exon in coding) == result.coding_length


@pytest.mark.parametrize('parsed', [_nf1, _brca1])
def test_the_cds_is_a_whole_number_of_codons(parsed: _Structure) -> None:
    assert parsed().coding_length % 3 == 0


@pytest.mark.parametrize(
    ('cds_position', 'exon', 'nt_from_exon_start', 'nt_to_exon_end'),
    [(3496, 26, 182, 1), (3495, 26, 181, 2), (3315, 26, 1, 182), (3314, 25, 117, 1)],
)
def test_a_cds_position_locates_its_exon_and_both_boundary_distances(
    cds_position: int, exon: int, nt_from_exon_start: int, nt_to_exon_end: int
) -> None:
    """nt_to_exon_end 1/2/3 are the exonic donor positions the splice-region judgement reads."""
    located = transcript_structure.position_in_transcript(_nf1(), cds_position=cds_position)
    assert located.exon == exon
    assert not located.intron
    assert located.nt_from_exon_start == nt_from_exon_start
    assert located.nt_to_exon_end == nt_to_exon_end
    assert located.cds_position == cds_position


def test_a_five_prime_utr_position_lands_in_an_exon_without_a_cds_coordinate() -> None:
    located = transcript_structure.position_in_transcript(_nf1(), cds_position=-20)
    assert located.exon == 1
    assert not located.HasField('cds_position')


def test_a_genomic_position_round_trips_to_its_cds_coordinate() -> None:
    located = transcript_structure.position_in_transcript(_nf1(), genomic_position=31232881)
    assert located.exon == 26
    assert located.cds_position == 3496
    assert located.nt_to_exon_end == 1


@pytest.mark.parametrize(
    ('genomic_position', 'nt_from_intron_start', 'nt_to_intron_end'),
    [(31232882, 1, 120), (31232883, 2, 119), (31233001, 120, 1)],
)
def test_an_intronic_genomic_position_reports_both_canonical_distances(
    genomic_position: int, nt_from_intron_start: int, nt_to_intron_end: int
) -> None:
    """+1/+2 and -1/-2 are the canonical-splice positions; the intron is numbered by its 5' exon."""
    located = transcript_structure.position_in_transcript(_nf1(), genomic_position=genomic_position)
    assert located.intron == 26
    assert not located.exon
    assert located.nt_from_intron_start == nt_from_intron_start
    assert located.nt_to_intron_end == nt_to_intron_end


def test_minus_strand_distances_are_measured_along_the_transcript_not_the_genome() -> None:
    """On the minus strand the exon's highest genomic coordinate is its first transcribed base."""
    exon = _exon(_brca1(), 2)
    first = transcript_structure.position_in_transcript(_brca1(), genomic_position=exon.genomic_end)
    last = transcript_structure.position_in_transcript(_brca1(), genomic_position=exon.genomic_start)
    assert (first.exon, first.nt_from_exon_start) == (2, 1)
    assert (last.exon, last.nt_to_exon_end) == (2, 1)
    donor = transcript_structure.position_in_transcript(_brca1(), genomic_position=exon.genomic_start - 1)
    assert (donor.intron, donor.nt_from_intron_start) == (2, 1)


def test_a_position_outside_the_transcript_is_an_invalid_request() -> None:
    with pytest.raises(errors.InvalidRequestError, match='outside'):
        transcript_structure.position_in_transcript(_nf1(), genomic_position=1)
    with pytest.raises(errors.InvalidRequestError, match='outside'):
        transcript_structure.position_in_transcript(_nf1(), cds_position=999999)


@pytest.mark.parametrize('kwargs', [{}, {'cds_position': 1, 'genomic_position': 31232881}])
def test_locating_takes_exactly_one_coordinate(kwargs: dict[str, int]) -> None:
    with pytest.raises(errors.InvalidRequestError, match='exactly one'):
        transcript_structure.position_in_transcript(_nf1(), **kwargs)


def test_c_numbering_has_no_zero() -> None:
    with pytest.raises(errors.InvalidRequestError, match='position 0'):
        transcript_structure.position_in_transcript(_nf1(), cds_position=0)


_Record = dict[str, object]


def _child(node: _Record, key: str) -> _Record:
    value = node[key]
    assert isinstance(value, dict)
    return value


def _exon_entries(record: _Record) -> list[_Record]:
    structure = _child(_child(record, 'genomic_spans'), 'NC_000017.11')['exon_structure']
    assert isinstance(structure, list)
    return structure


def _number(node: _Record, key: str) -> int:
    value = node[key]
    assert isinstance(value, int)
    return value


def _mutated(mutate: Callable[[_Record], None]) -> object:
    """The NF1 payload with one edit applied to its transcript record, for a violating-input test."""
    payload = copy.deepcopy(_NF1)
    mutate(_transcript_record(payload))
    return payload


def _transcript_record(payload: object) -> _Record:
    assert isinstance(payload, list)
    entry = payload[0]
    assert isinstance(entry, dict)
    records = entry['transcripts']
    assert isinstance(records, list)
    record = records[0]
    assert isinstance(record, dict)
    return record


def test_an_exon_table_that_does_not_tile_the_transcript_raises() -> None:
    """Every derived coordinate assumes the tiling; a gap yields a plausible but wrong answer."""

    def shift(record: _Record) -> None:
        exons = _exon_entries(record)
        for exon in exons:
            if _number(exon, 'exon_number') >= 20:
                exon['transcript_start'] = _number(exon, 'transcript_start') + 2
                exon['transcript_end'] = _number(exon, 'transcript_end') + 2
        exons[-1]['transcript_end'] = _number(exons[-1], 'transcript_end') - 2

    with pytest.raises(ValueError, match='exon 20 starts at'):
        _parsed(_mutated(shift), 'NM_001042492.3')


def test_an_exon_table_shorter_than_the_transcript_raises() -> None:
    def truncate(record: _Record) -> None:
        _exon_entries(record).pop()

    with pytest.raises(ValueError, match='but the transcript is 12373 nt'):
        _parsed(_mutated(truncate), 'NM_001042492.3')


def test_a_gap_in_the_exon_numbering_raises() -> None:
    def renumber(record: _Record) -> None:
        _exon_entries(record)[30]['exon_number'] = 99

    with pytest.raises(ValueError, match='numbering'):
        _parsed(_mutated(renumber), 'NM_001042492.3')


def test_a_gapped_exon_alignment_refuses_to_project_a_genomic_position() -> None:
    """A balanced indel pair leaves both spans the same length, so the cigar is the only tell."""

    def gap(record: _Record) -> None:
        _exon_entries(record)[25]['cigar'] = '100=1I1D81='

    result = _parsed(_mutated(gap), 'NM_001042492.3')
    with pytest.raises(ValueError, match='not an ungapped match'):
        transcript_structure.position_in_transcript(result, genomic_position=31232800)
    # The c. route reads the upstream's own transcript coordinates, so the gap does not affect it.
    assert transcript_structure.position_in_transcript(result, cds_position=3496).exon == 26


@pytest.mark.parametrize(
    ('key', 'match'),
    [('annotations', 'no annotations object'), ('length', 'length')],
)
def test_a_transcript_field_a_derived_answer_depends_on_is_required(key: str, match: str) -> None:
    """A swallowed MANE flag downgrades exon relevance to "Few"; a swallowed length breaks the tiling check."""

    def drop(record: _Record) -> None:
        record.pop(key)

    with pytest.raises(ValueError, match=match):
        _parsed(_mutated(drop), 'NM_001042492.3')


def test_a_missing_gene_symbol_is_required_rather_than_empty() -> None:
    payload = copy.deepcopy(_NF1)
    assert isinstance(payload[0], dict)
    payload[0].pop('current_symbol')
    with pytest.raises(ValueError, match='no current_symbol'):
        _parsed(payload, 'NM_001042492.3')


def test_a_mane_flag_that_is_not_a_boolean_raises() -> None:
    def stringify(record: _Record) -> None:
        _child(record, 'annotations')['mane_select'] = 'true'

    with pytest.raises(ValueError, match='no boolean'):
        _parsed(_mutated(stringify), 'NM_001042492.3')


def test_an_accession_the_service_does_not_hold_is_not_found() -> None:
    with pytest.raises(errors.UnknownVariantError, match='no transcript record'):
        _parsed(_NF1, 'NM_000267.3')


def test_an_accession_the_service_does_not_recognise_is_not_found() -> None:
    """gene2transcripts answers an unresolvable query 200 with an `error` string, not a 4xx."""
    payload = [{'error': 'No transcript definition for (tx_ac=NM_000000)', 'requested_symbol': 'NM_000000.1'}]
    with pytest.raises(errors.UnknownVariantError, match='does not recognise'):
        _parsed(payload, 'NM_000000.1')


def test_a_malformed_genomic_spans_stays_a_value_error() -> None:
    """A structurally wrong payload is an uncharacterised fault, not a settled "no such alignment"."""
    payload = [{'current_symbol': 'X', 'transcripts': [{'reference': 'NM_1.1', 'genomic_spans': 'oops'}]}]
    with pytest.raises(ValueError, match='no genomic_spans object'):
        _parsed(payload, 'NM_1.1')


def test_an_alignment_missing_for_the_build_is_not_found() -> None:
    payload = [{'current_symbol': 'NF1', 'transcripts': [{'reference': 'NM_1.1', 'genomic_spans': {}}]}]
    with pytest.raises(errors.UnknownVariantError, match='no single GRCh38 alignment'):
        _parsed(payload, 'NM_1.1')


def test_an_orientation_that_is_not_a_strand_raises() -> None:
    """Every genomic projection branches on the sign, so an unexpected value must not default to minus."""

    def unstrand(record: _Record) -> None:
        _child(_child(record, 'genomic_spans'), 'NC_000017.11')['orientation'] = 0

    with pytest.raises(ValueError, match='orientation 0'):
        _parsed(_mutated(unstrand), 'NM_001042492.3')


def test_cds_bounds_outside_the_transcript_raise() -> None:
    def overrun(record: _Record) -> None:
        record['coding_end'] = _number(record, 'length') + 1

    with pytest.raises(ValueError, match='is not inside'):
        _parsed(_mutated(overrun), 'NM_001042492.3')


def _fetch(handler: httpx.MockTransport) -> transcript_structure.TranscriptStructureResult:
    async def run() -> transcript_structure.TranscriptStructureResult:
        async with httpx.AsyncClient(transport=handler) as client:
            return await transcript_structure.fetch_transcript_structure('NM_001042492.3', 'GRCh38', http_client=client)

    return asyncio.run(run())


def _routed(request: httpx.Request) -> httpx.Response:
    if request.url.path == '/hello/':
        return httpx.Response(200, json=_METADATA)
    return httpx.Response(200, json=_NF1)


def test_fetch_stamps_each_alignment_release_as_one_dataset_versions_element() -> None:
    """The exon table is only reproducible against the alignment database it came from."""
    result = _fetch(httpx.MockTransport(_routed))
    assert 'vvta_2025_02' in result.dataset_versions
    assert 'vvdb_2025_3' in result.dataset_versions
    assert len(result.exons) == 58


@pytest.mark.parametrize('metadata', [{}, {'metadata': {'vvdb_version': 'vvdb_2025_3'}}])
def test_fetch_raises_on_absent_or_partial_version_metadata(metadata: dict[str, object]) -> None:
    """A partial stamp is indistinguishable from a full one, so it must not pass as provenance."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=metadata if request.url.path == '/hello/' else _NF1)

    with pytest.raises(ValueError, match=r'metadata|vvta_version|variantvalidator_version'):
        _fetch(httpx.MockTransport(handler))


def test_fetch_raises_on_a_non_2xx() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        _fetch(httpx.MockTransport(lambda _: httpx.Response(500, json={})))


_GENE_REFSEQ = json.loads((_FIXTURES / 'gene_transcripts_refseq.json').read_text())
_GENE_ENSEMBL = json.loads((_FIXTURES / 'gene_transcripts_ensembl.json').read_text())
# The accession the recorded gene's transcripts align to; the inventory selects an alignment by it.
_CHROMOSOME = 'NC_000001.11'


def _gene_parsed(payload: object, annotation_set: str = 'refseq') -> transcript_structure.GeneTranscriptsResult:
    return transcript_structure.parse_gene_transcripts(
        payload, gene='PCSK9', annotation_set=annotation_set, dataset_versions=('vvta_2025_02',), query='q'
    )


def _mutated_gene(payload: object, mutate: Callable[[_Record], None], index: int = 0) -> object:
    """A gene-scoped payload with one edit applied to one of its transcript records."""
    copied = copy.deepcopy(payload)
    assert isinstance(copied, list)
    entry = copied[0]
    assert isinstance(entry, dict)
    records = entry['transcripts']
    assert isinstance(records, list)
    record = records[index]
    assert isinstance(record, dict)
    mutate(record)
    return copied


@pytest.mark.parametrize(
    ('payload', 'annotation_set', 'mane'),
    [(_GENE_REFSEQ, 'refseq', 'NM_174936.4'), (_GENE_ENSEMBL, 'ensembl', 'ENST00000302118.5')],
)
def test_both_recorded_annotation_sets_parse_into_an_inventory(payload: object, annotation_set: str, mane: str) -> None:
    """The Ensembl arm is what makes an isoform GTEx measures testable, so it is read, not assumed."""
    result = _gene_parsed(payload, annotation_set)
    assert result.gene == 'PCSK9'
    assert result.annotation_set == annotation_set
    assert not result.unreadable  # non-vacuous: the whole set parsed
    assert len(result.transcripts) > 1
    flagged = [t.accession for t in result.transcripts if t.mane_select]
    assert flagged == [mane]  # MANE Select is one transcript per namespace
    assert any(not t.coding for t in result.transcripts)  # coding status is read, not assumed
    for transcript in result.transcripts:
        assert transcript.exons_on(_CHROMOSOME)


def test_an_exon_numbering_gap_does_not_stop_a_record_being_classified() -> None:
    """The inventory reads genomic spans only; the 1..N invariant guards the c.-coordinate parse."""

    def renumber(record: _Record) -> None:
        exons = _child(_child(record, 'genomic_spans'), 'NC_000001.11')['exon_structure']
        assert isinstance(exons, list)
        for entry in exons:
            assert isinstance(entry, dict)
            entry['exon_number'] = 99

    result = _gene_parsed(_mutated_gene(_GENE_REFSEQ, renumber))
    assert len(result.transcripts) == len(_gene_parsed(_GENE_REFSEQ).transcripts)
    assert not result.unreadable


@pytest.mark.parametrize(
    'mutate',
    [
        pytest.param(lambda r: r.__setitem__('genomic_spans', {}), id='no-alignment'),
        pytest.param(lambda r: r.__setitem__('genomic_spans', 'not-an-object'), id='malformed-alignment'),
        pytest.param(lambda r: r.__setitem__('coding_end', None), id='half-a-cds-pair'),
        pytest.param(lambda r: r.__setitem__('annotations', {'mane_select': 'true'}), id='non-boolean-mane'),
        pytest.param(lambda r: r.__setitem__('annotations', {}), id='no-mane-flags'),
    ],
)
def test_a_record_a_field_of_which_is_unreadable_is_named_not_dropped_and_not_raised(
    mutate: Callable[[_Record], None],
) -> None:
    """The record is reported, so it neither fails the query nor passes for one lacking the exon."""
    whole = _gene_parsed(_GENE_REFSEQ)
    unreadable = _gene_parsed(_mutated_gene(_GENE_REFSEQ, mutate))
    assert len(unreadable.transcripts) == len(whole.transcripts) - 1
    assert unreadable.unreadable == [whole.transcripts[0].accession]


def test_a_record_aligned_to_several_accessions_keeps_them_all() -> None:
    """The caller selects the alignment its assessed interval is on; discarding the record loses it."""

    def duplicate(record: _Record) -> None:
        spans = _child(record, 'genomic_spans')
        spans['NC_000024.10'] = copy.deepcopy(spans['NC_000001.11'])

    transcript = _gene_parsed(_mutated_gene(_GENE_REFSEQ, duplicate)).transcripts[0]
    assert set(transcript.alignments) == {'NC_000001.11', 'NC_000024.10'}
    assert transcript.exons_on('NC_000001.11')
    assert transcript.exons_on('NC_000009.12') is None


def test_a_descending_exon_span_is_rejected() -> None:
    """Every interval test compares a start against a start, so a descending span answers plausibly."""
    with pytest.raises(ValueError, match='descends'):
        transcript_structure.ExonSpan(start=200, end=100)


def test_a_descending_recorded_span_makes_its_record_unreadable() -> None:
    def invert(record: _Record) -> None:
        exons = _child(_child(record, 'genomic_spans'), 'NC_000001.11')['exon_structure']
        assert isinstance(exons, list)
        first = exons[0]
        assert isinstance(first, dict)
        first['genomic_start'], first['genomic_end'] = first['genomic_end'], first['genomic_start']

    result = _gene_parsed(_mutated_gene(_GENE_REFSEQ, invert))
    assert result.unreadable == [_gene_parsed(_GENE_REFSEQ).transcripts[0].accession]


def test_a_record_without_an_accession_raises() -> None:
    """A record that cannot be named cannot be reported as unreadable either."""

    def anonymise(record: _Record) -> None:
        record.pop('reference')

    with pytest.raises(ValueError, match='no reference accession'):
        _gene_parsed(_mutated_gene(_GENE_REFSEQ, anonymise))


def test_an_unresolvable_symbol_is_not_an_empty_inventory() -> None:
    with pytest.raises(errors.UnknownVariantError):
        _gene_parsed([{'error': 'gene symbol not recognised'}])


def test_a_response_without_a_transcripts_list_raises() -> None:
    with pytest.raises(ValueError, match='no transcripts list'):
        _gene_parsed([{'current_symbol': 'PCSK9'}])


def _gene_routed(request: httpx.Request) -> httpx.Response:
    if request.url.path == '/hello/':
        return httpx.Response(200, json=_METADATA)
    return httpx.Response(200, json=_GENE_ENSEMBL if '/ensembl/' in str(request.url) else _GENE_REFSEQ)


def test_fetch_gene_transcripts_reads_both_annotation_sets() -> None:
    """Which set defines "the gene's transcripts" is the curator's question, so both are fetched."""

    async def run() -> list[transcript_structure.GeneTranscriptsResult]:
        transport = httpx.MockTransport(_gene_routed)
        async with httpx.AsyncClient(transport=transport) as client:
            return await transcript_structure.fetch_gene_transcripts('PCSK9', 'GRCh38', http_client=client)

    results = asyncio.run(run())
    assert [result.annotation_set for result in results] == ['refseq', 'ensembl']
    assert all('vvta_2025_02' in result.dataset_versions for result in results)
    # Each result is the arm its query names — not the other arm's payload under a relabelled field.
    assert [len(result.transcripts) for result in results] == [
        len(_gene_parsed(_GENE_REFSEQ).transcripts),
        len(_gene_parsed(_GENE_ENSEMBL, 'ensembl').transcripts),
    ]
    assert [result.query.rsplit('?', 1)[0].rsplit('/', 3)[-3:] for result in results] == [
        ['all', 'refseq', 'GRCh38'],
        ['all', 'ensembl', 'GRCh38'],
    ]


@pytest.mark.parametrize('structure', [_nf1, _brca1], ids=['plus_strand', 'minus_strand'])
def test_a_c_range_projects_onto_an_ascending_genomic_interval(
    structure: Callable[[], transcript_structure.TranscriptStructureResult],
) -> None:
    """The interval a span census searches. Ascending on both strands, and it holds each endpoint.

    On the minus strand the genomic coordinate descends as the transcript advances, so an interval
    built by taking `start`'s coordinate first would be empty and the census would come back saying
    ClinVar holds nothing at the codon.
    """
    result = structure()
    span = transcript_structure.genomic_span_of_cds_range(result, 100, 102)
    endpoints = {
        transcript_structure.position_in_transcript(result, genomic_position=coordinate).cds_position
        for coordinate in (span.start, span.end)
    }
    assert endpoints == {100, 102}


def test_a_single_base_c_range_is_a_one_base_interval() -> None:
    span = transcript_structure.genomic_span_of_cds_range(_nf1(), 3496, 3496)
    assert span.start == span.end


def test_a_c_range_crossing_an_exon_boundary_covers_the_intron_between() -> None:
    """A superset of the bases named, never a subset — and the caller is told what was searched."""
    result = _nf1()
    boundary = next(exon for exon in result.exons if exon.HasField('cds_end') and exon.number == 2)
    span = transcript_structure.genomic_span_of_cds_range(result, boundary.cds_end, boundary.cds_end + 1)
    assert span.end - span.start > 1


@pytest.mark.parametrize(('start', 'end'), [(102, 100), (0, 100), (100, 0)])
def test_a_c_range_that_names_no_span_is_refused(start: int, end: int) -> None:
    """A descending range and a c.0 endpoint would each search an interval the caller did not name."""
    with pytest.raises(errors.InvalidRequestError):
        transcript_structure.genomic_span_of_cds_range(_nf1(), start, end)


def test_a_c_position_past_the_termination_codon_is_refused() -> None:
    """It is a 3'UTR base, which HGVS numbers c.*n; read as a CDS number it answers about the 3'UTR.

    The dangerous direction: an agent that miscomputes a codon from a protein position gets a
    populated census about a region it did not name, and scores an informative variant off it.
    """
    result = _nf1()
    with pytest.raises(errors.InvalidRequestError, match='past the termination codon'):
        transcript_structure.genomic_span_of_cds_range(result, result.coding_length, result.coding_length + 1)


def test_a_c_position_before_the_transcript_is_refused() -> None:
    with pytest.raises(errors.InvalidRequestError, match='outside'):
        transcript_structure.genomic_span_of_cds_range(_nf1(), -10_000_000, 100)


def _requested_paths(accession: str, gene: str) -> list[str]:
    """The wire-form URL paths both fetches build, for identifiers that would restructure an unencoded path.

    `url.path` is percent-decoded, so it reads the same whether or not the segment was encoded; `raw_path`
    is what actually goes on the wire.
    """
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode().split('?')[0])
        if request.url.path == '/hello/':
            return httpx.Response(200, json=_METADATA)
        return httpx.Response(200, json=_NF1 if accession in request.url.path else _GENE_REFSEQ)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
            for call in (
                transcript_structure.fetch_transcript_structure(accession, 'GRCh38', http_client=client),
                transcript_structure.fetch_gene_transcripts(gene, 'GRCh38', http_client=client),
            ):
                with contextlib.suppress(errors.UnknownVariantError, ValueError):
                    await call

    asyncio.run(run())
    return seen


def test_an_identifier_cannot_restructure_the_request_path() -> None:
    """Both identifiers reach here as the caller wrote them, and the sandbox agent is one of the callers.

    A `/` or a `..` left unencoded moves the query to a different endpoint of the same host, so the segment
    has to survive as one segment whatever it contains.
    """
    paths = _requested_paths('NM_1.1/../../hello', 'PCSK9/..%2f..')
    assert paths, 'no request was issued'
    for path in paths:
        assert '/..' not in path, f'an identifier escaped its path segment: {path}'
    # The endpoint each query names is still gene2transcripts_v2 (or the unparameterised /hello/ metadata call).
    assert all(path == '/hello/' or 'gene2transcripts_v2' in path for path in paths), paths
