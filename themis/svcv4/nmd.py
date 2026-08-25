"""Nonsense-mediated decay (NMD) and non-stop decay (NSD) prediction from transcript geometry.

NMD selects the null (NUL_) versus coding (CDS_) path in every LoF variant type. SVCv4 predicts NMD
when the premature termination codon (PTC) lies at least 50 nt upstream of the last exon-exon
junction, and never in a single-exon gene (no junction downstream to trigger decay). NSD is the
mirror question for stop-lost / non-stop variants: with no in-frame stop before the polyA site, the
ribosome reads to the polyA and the transcript is degraded by non-stop decay.

These are geometry calculations only. The PTC / next-stop / polyA positions are inputs the caller
supplies; alt-start rescue and the downstream re-evaluation of a skipped exon's relevance are the
model's judgement, not decided here.

Two doors reach the geometry, and they are not interchangeable. `nmd_from_structure` applies the rule
to a PTC over a transcript's own exon table, and its whole difficulty is the coordinate conversion: a
PTC is named in c. coordinates and the junctions are n. positions, so the 5'UTR sits between them.
`nmd_from_skip` reads a determination `Splice.PredictSkipOutcome` already made — over the *aberrant*
transcript's exon structure, which is not the reference transcript and cannot be recovered from it.
"""

from __future__ import annotations

import dataclasses

from themis.rpc import splice_pb2, transcript_pb2
from themis.svcv4 import provenance

# SVCv4's NMD boundary: a PTC at least this many nt upstream of the last exon-exon junction is
# predicted to trigger NMD (SM8; the 50-55 nt rule, PMID 15040442).
NMD_UPSTREAM_NT = 50


def predicts_nmd(exon_lengths: tuple[int, ...], ptc_position: int) -> bool:
    """Predict whether a PTC triggers NMD by the 50-nt rule.

    Args:
        exon_lengths: The mature transcript's exon lengths (nt), 5'->3'. Its length is the exon
            count; a single-exon transcript never triggers NMD.
        ptc_position: The 1-based transcript position of the PTC's first base.

    Returns:
        True if NMD is predicted (multi-exon transcript and the PTC is at least `NMD_UPSTREAM_NT` nt
        upstream of the last exon-exon junction).

    Raises:
        ValueError: On an empty transcript, a non-positive exon length, or a PTC position outside
            the transcript.
    """
    if not exon_lengths:
        raise ValueError('transcript must have at least one exon')
    if any(length <= 0 for length in exon_lengths):
        raise ValueError('exon lengths must be positive')
    transcript_length = sum(exon_lengths)
    if not 1 <= ptc_position <= transcript_length:
        raise ValueError(f'PTC position {ptc_position} outside transcript of length {transcript_length}')
    margin = nt_upstream_of_last_junction(exon_lengths, ptc_position)
    return margin is not None and margin >= NMD_UPSTREAM_NT


def nt_upstream_of_last_junction(exon_lengths: tuple[int, ...], ptc_position: int) -> int | None:
    """The margin the 50-nt rule is applied to, or None for a single-exon transcript.

    Args:
        exon_lengths: The mature transcript's exon lengths (nt), 5'->3'.
        ptc_position: The 1-based transcript position of the PTC's first base.

    Returns:
        How far upstream of the last exon-exon junction the PTC lies, negative where it lies
        downstream of it; None where the transcript has one exon and so no junction.
    """
    if len(exon_lengths) == 1:
        return None
    return sum(exon_lengths[:-1]) - ptc_position  # the last penultimate-exon base, less the PTC


def predicts_nsd(next_inframe_stop: int | None, polya_position: int) -> bool:
    """Predict non-stop decay for a stop-lost / non-stop variant.

    NSD is predicted when no in-frame stop codon occurs before the polyA site, so translation
    proceeds into the polyA and the transcript is degraded.

    Args:
        next_inframe_stop: The 1-based transcript position of the next in-frame stop codon downstream
            of the lost stop, or None if the reading frame reaches the polyA with no in-frame stop.
        polya_position: The 1-based transcript position of the polyA site (its hexamer signal, 12-25
            nt upstream of the 3' end per SM16).

    Returns:
        True if NSD is predicted (no in-frame stop at or before the polyA site).

    Raises:
        ValueError: On a non-positive position.
    """
    if polya_position <= 0:
        raise ValueError(f'polyA position must be positive, got {polya_position}')
    if next_inframe_stop is not None and next_inframe_stop <= 0:
        raise ValueError(f'next in-frame stop position must be positive, got {next_inframe_stop}')
    return next_inframe_stop is None or next_inframe_stop > polya_position


@dataclasses.dataclass(frozen=True)
class NmdCall:
    """An NMD determination and the geometry behind it.

    Attributes:
        predicted: Whether the PTC is predicted to trigger NMD.
        nt_upstream_of_last_junction: The margin the 50-nt rule was applied to — how far upstream of
            the last exon-exon junction the PTC lies. Negative where it lies downstream of it, and
            None for a single-exon product, which has no junction and never triggers NMD.
        derivation: The margin and what it was measured over, for the audit trail.
        releases: The releases behind the structure the call was made over; empty for a call read off
            a skip, whose releases stay on the response the skip was chosen from.
    """

    predicted: bool
    nt_upstream_of_last_junction: int | None
    derivation: str
    releases: tuple[provenance.Release, ...] = ()


def _exon_lengths(structure: transcript_pb2.GetStructureResponse) -> tuple[int, ...]:
    """The mature transcript's exon lengths, checked against the length the table states for it."""
    lengths = tuple(exon.length for exon in structure.exons)
    if not lengths:
        raise ValueError(f'the exon table for {structure.transcript} carries no exon')
    if sum(lengths) != structure.transcript_length:
        raise ValueError(
            f'the exons of {structure.transcript} sum to {sum(lengths)} nt against a stated transcript length '
            f'of {structure.transcript_length}; the 50-nt margin is measured over the exon table, so a table '
            'that does not describe the transcript cannot be read'
        )
    return lengths


def nmd_from_structure(structure: transcript_pb2.GetStructureResponse, *, ptc_cds_position: int) -> NmdCall:
    """Apply the 50-nt rule to a PTC, over a transcript's own exon table.

    **The two coordinate systems are the whole of the difficulty.** A PTC is named in c. coordinates,
    which start at the initiation codon, while the exon table's junctions are n. positions, which
    start at the transcript's 5' end. `cds_transcript_start` is the n. position of c.1, so the PTC
    sits at `cds_transcript_start + ptc_cds_position - 1` — and skipping that conversion measures the
    margin from the wrong end of the 5'UTR, which on a long one flips the call.

    Args:
        structure: The transcript's exon table (`Transcript.GetStructure`).
        ptc_cds_position: The c. position of the PTC's first base, positive and inside the CDS.

    Returns:
        The `NmdCall`, stamped with the releases the exon table rests on.

    Raises:
        ValueError: If the table carries no exon or does not sum to the transcript length it states,
            if it states no position for c.1 — unset, that is 0, and the conversion silently becomes
            no conversion — if the PTC position is not a positive c. coordinate or falls outside the
            coding sequence, or if the response states no provenance.
    """
    if ptc_cds_position < 1:
        raise ValueError(f'a PTC sits at a positive c. position, got {ptc_cds_position}')
    lengths = _exon_lengths(structure)
    if structure.cds_transcript_start < 1:
        raise ValueError(
            f'the exon table for {structure.transcript} states no transcript position for c.1, so the PTC '
            'cannot be placed on the mature transcript the junctions are numbered over'
        )
    transcript_position = structure.cds_transcript_start + ptc_cds_position - 1
    if transcript_position > structure.cds_transcript_end:
        raise ValueError(
            f'c.{ptc_cds_position} is n.{transcript_position}, past the termination codon at '
            f'n.{structure.cds_transcript_end}; a stop beyond the reference one is not a premature stop'
        )
    predicted = predicts_nmd(lengths, transcript_position)
    margin = nt_upstream_of_last_junction(lengths, transcript_position)
    over = (
        'a single-exon transcript, which has no junction to trigger decay'
        if margin is None
        else f'{margin} nt upstream of the last of {len(lengths)} exons'
    )
    return NmdCall(
        predicted=predicted,
        nt_upstream_of_last_junction=margin,
        derivation=(
            f'c.{ptc_cds_position} is n.{transcript_position} on {structure.transcript} '
            f'(c.1 at n.{structure.cds_transcript_start}): {over}, against the {NMD_UPSTREAM_NT} nt rule'
        ),
        releases=provenance.releases_of(structure.provenance),
    )


def nmd_from_skip(skip: splice_pb2.PredictedSkip) -> NmdCall:
    """Read the NMD determination off one skip `Splice.PredictSkipOutcome` composed.

    The rpc excises the exon, translates the result and reads the PTC off the sequence, so its
    determination is made over the *aberrant* transcript's own exon structure — which is not the
    reference transcript `nmd_from_structure` reads, and cannot be recovered from it. Which of a
    response's skips the variant actually produces is the analyst's, so this takes the chosen one;
    the releases stay on the response it came from.

    Args:
        skip: One element of `PredictSkipOutcomeResponse.skips`.

    Returns:
        The `NmdCall`.

    Raises:
        ValueError: If the skip names no product, predicts NMD without a premature stop to trigger
            it or without a junction to trigger it at, or states a stop without the position it
            lands at — each is a response contradicting itself.
    """
    if skip.product == splice_pb2.SPLICE_PRODUCT_UNSPECIFIED:
        raise ValueError('the skip names no product, and the NMD call is made over what the transcript becomes')
    stop = skip.product == splice_pb2.SPLICE_PRODUCT_PREMATURE_STOP
    if skip.nmd_predicted and not stop:
        raise ValueError(
            f'the skip predicts NMD on a {splice_pb2.SpliceProduct.Name(skip.product)} product; only a '
            'premature stop triggers it'
        )
    if stop and not skip.HasField('ptc_cds_position'):
        raise ValueError('the skip states a premature stop and not where it lands')
    margin = skip.nt_upstream_of_last_junction if skip.HasField('nt_upstream_of_last_junction') else None
    if skip.nmd_predicted and margin is None:
        raise ValueError(
            'the skip predicts NMD over a single-exon product, which has no exon-exon junction to trigger it'
        )
    where = 'no exon-exon junction downstream' if margin is None else f'{margin} nt upstream of the last junction'
    return NmdCall(
        predicted=skip.nmd_predicted,
        nt_upstream_of_last_junction=margin,
        derivation=(
            f'skipping exon(s) {list(skip.skipped_exons)} gives a '
            f'{splice_pb2.SpliceProduct.Name(skip.product)} product: {where}, against the '
            f'{NMD_UPSTREAM_NT} nt rule over the aberrant transcript'
        ),
    )
