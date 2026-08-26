"""Predicted structural consequence of a lost splice site: the aberrant transcript's frame and PTC.

Given the exon whose donor or acceptor is lost, this builds the skipped transcript from the exon
table and the RefSeq sequence, translates it from the initiation codon, and reports where the product
terminates. Whether the site is lost at all is not decided here (that is the Splice rpc's predictors),
nor is whether the aberrant product is clinically relevant.

The premature-termination call is read off the sequence, not inferred from the frame shift: an
out-of-frame skip does not fix where the next stop codon falls, and an in-frame skip can still
introduce one at the new exon junction. The 50-nt NMD determination itself is `themis.svcv4.nmd`.

Adjacent-pair skips ride alongside the single-exon skip when the single skip shifts the frame — the
frame-restoring alternative an analyst weighs; which one the variant produces is their call.
"""

from __future__ import annotations

from collections.abc import Sequence

from themis.rpc import splice_pb2
from themis.services.evidence import errors
from themis.services.evidence.upstreams import transcript_sequence, transcript_structure
from themis.svcv4 import nmd

_STOP_CODONS = frozenset({'TAA', 'TAG', 'TGA'})
_CODON_NT = 3

# A skipped span as the reference transcript's inclusive n. bounds.
type _Span = tuple[int, int]


def _spans(structure: transcript_structure.TranscriptStructureResult, exon_numbers: Sequence[int]) -> list[_Span]:
    by_number = {exon.number: exon for exon in structure.exons}
    return sorted((by_number[n].transcript_start, by_number[n].transcript_end) for n in exon_numbers)


def _product_sequence(sequence: str, spans: Sequence[_Span]) -> str:
    """The mature sequence with each span excised (spans are ascending and non-overlapping)."""
    product = sequence
    for start, end in reversed(spans):
        product = product[: start - 1] + product[end:]
    return product


def _product_position(spans: Sequence[_Span], position: int) -> int:
    """A reference n. position in product coordinates; a removed one collapses onto its span's start."""
    removed = 0
    for start, end in spans:
        if end < position:
            removed += end - start + 1
        elif start <= position:
            removed += position - start
    return position - removed


def _survives(spans: Sequence[_Span], position: int) -> bool:
    return not any(start <= position <= end for start, end in spans)


def _first_stop(product: str, orf_start: int) -> int | None:
    """The product position of the first in-frame stop codon's first base, or None if none occurs."""
    for start in range(orf_start, len(product) - _CODON_NT + 2, _CODON_NT):
        if product[start - 1 : start - 1 + _CODON_NT] in _STOP_CODONS:
            return start
    return None


def _classify(stop: int | None, reference_stop_anchor: int, *, reference_stop_intact: bool) -> splice_pb2.SpliceProduct:
    if stop is None:
        return splice_pb2.SPLICE_PRODUCT_NO_TERMINATION
    if stop < reference_stop_anchor:
        return splice_pb2.SPLICE_PRODUCT_PREMATURE_STOP
    if stop == reference_stop_anchor and reference_stop_intact:
        return splice_pb2.SPLICE_PRODUCT_INFRAME_DELETION
    return splice_pb2.SPLICE_PRODUCT_EXTENDED_TERMINATION


def _skip(
    structure: transcript_structure.TranscriptStructureResult, sequence: str, exon_numbers: Sequence[int]
) -> splice_pb2.PredictedSkip:
    skipped = set(exon_numbers)
    by_number = {exon.number: exon for exon in structure.exons}
    spans = _spans(structure, exon_numbers)
    coding_nt_removed = sum(by_number[n].coding_length for n in skipped)
    skip = splice_pb2.PredictedSkip(
        skipped_exons=sorted(skipped),
        coding_nt_removed=coding_nt_removed,
        frame_shift=coding_nt_removed % _CODON_NT,
    )
    initiation = range(structure.cds_transcript_start, structure.cds_transcript_start + _CODON_NT)
    if not all(_survives(spans, position) for position in initiation):
        skip.product = splice_pb2.SPLICE_PRODUCT_START_LOST
        return skip

    product = _product_sequence(sequence, spans)
    orf_start = _product_position(spans, structure.cds_transcript_start)
    stop = _first_stop(product, orf_start)
    reference_stop = range(structure.cds_transcript_end - _CODON_NT + 1, structure.cds_transcript_end + 1)
    skip.product = _classify(
        stop,
        _product_position(spans, structure.cds_transcript_end - _CODON_NT + 1),
        reference_stop_intact=all(_survives(spans, position) for position in reference_stop),
    )
    if skip.product != splice_pb2.SPLICE_PRODUCT_PREMATURE_STOP or stop is None:
        return skip

    product_exon_lengths = tuple(exon.length for exon in structure.exons if exon.number not in skipped)
    skip.ptc_cds_position = stop - orf_start + 1
    skip.ptc_codon = (skip.ptc_cds_position + _CODON_NT - 1) // _CODON_NT
    if len(product_exon_lengths) > 1:
        skip.nt_upstream_of_last_junction = sum(product_exon_lengths[:-1]) - stop
    skip.nmd_predicted = nmd.predicts_nmd(product_exon_lengths, stop)
    return skip


def _adjacent_pairs(affected_exon: int, exon_count: int) -> list[tuple[int, ...]]:
    pairs = []
    if affected_exon > 1:
        pairs.append((affected_exon - 1, affected_exon))
    if affected_exon < exon_count:
        pairs.append((affected_exon, affected_exon + 1))
    return pairs


def predict_skips(
    structure: transcript_structure.TranscriptStructureResult,
    sequence: transcript_sequence.TranscriptSequenceResult,
    *,
    affected_exon: int,
) -> list[splice_pb2.PredictedSkip]:
    """Predict the transcript(s) that result from losing one exon's splice site.

    Args:
        structure: The transcript's exon table (both coordinate systems + the CDS bounds).
        sequence: The same transcript's mature sequence — the same accession, so the exon table's n.
            coordinates index it.
        affected_exon: The exon whose donor or acceptor is lost.

    Returns:
        The single-exon skip, followed by the adjacent-pair skips when the single skip shifts the
        reading frame.

    Raises:
        errors.InvalidRequestError: If `affected_exon` is not an exon of the transcript.
        ValueError: If the sequence's length disagrees with the exon table's (the two upstreams
            describe different records, so the n. coordinates would index the wrong bases).
    """
    exon_count = len(structure.exons)
    if not 1 <= affected_exon <= exon_count:
        raise errors.InvalidRequestError(f'{structure.transcript} has exons 1-{exon_count}; got exon {affected_exon}')
    if len(sequence.sequence) != structure.transcript_length:
        raise ValueError(
            f'{sequence.accession} is {len(sequence.sequence)} nt but the {structure.transcript} exon table '
            f'spans {structure.transcript_length} nt'
        )
    single = _skip(structure, sequence.sequence, (affected_exon,))
    if not single.frame_shift:
        return [single]
    return [single, *(_skip(structure, sequence.sequence, pair) for pair in _adjacent_pairs(affected_exon, exon_count))]
