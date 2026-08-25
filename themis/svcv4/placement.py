"""Placing a pool of ClinVar records against a coding span: which are in it, and which cannot be told.

The `*_INF` rules and SM18's §17 waiver both ask what else has been classified at one codon or in one
exon, and `ClinVar.SearchCodingSpan` answers with a pool that reaches past the question: a codon's
search runs on genomic coordinates, so it returns records at neighbouring positions, on other
transcripts of the gene, and in the flanking introns. Three properties of a c. coordinate decide
which of them the span actually holds, and each has cost a reading before:

  - **The transcript comes first.** A pool is not written against one transcript, and the same c.
    number is a different base on two of them. The accession is compared without its version run,
    since ClinVar indexes a record under whichever version its submitter wrote — the residual, and
    the reason each placed record is returned rather than a count, is that a version bump that moved
    the coding start moves the numbering with it.
  - **A position means nothing without its region.** HGVS restarts at 1 in each of the 5'UTR, the CDS
    and the 3'UTR, so `c.-25`, `c.25` and `c.*25` are three different bases; and inside the 5'UTR the
    numbers count *down* toward the coding start.
  - **An intron offset is not in the exon.** `c.3496+1` is the first intronic base after `c.3496`,
    not a second name for it, so only an offset of 0 places a base inside the exon. A record can
    still reach the exon by *spanning* an intron — a deletion beginning at `c.3496+1` and ending at
    `c.3600` covers exonic bases at both ends — which is why the test is over the record's whole
    extent rather than over its endpoints one at a time.
  - **An insertion's endpoints bracket it and it alters neither of them.** HGVS writes an insertion
    range exclusive, so `c.1107_1108insG` spans two bases and changes no base at all; read as an
    overlap it would enter every span either endpoint sits in. The coordinates alone cannot tell it
    from `c.1107_1108del`, which does change both, so the edit is read off the record's expression
    and an insertion is placed by whether the point it sits at is *interior* to the span.

What cannot be placed is carried through by name rather than dropped. A copy-number or repeat-notation
record has no coding span at all, and dropping one silently understates the exon it may well belong
to; whether it does is not a question the coordinates can settle.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable

from themis.rpc import clinvar_pb2
from themis.svcv4 import clinvar_classification, scoring

# The regions in transcript order, so a lexicographic comparison over (rank, position, offset) is a
# comparison along the transcript.
_REGION_RANK = {
    clinvar_pb2.CODING_REGION_FIVE_PRIME_UTR: 0,
    clinvar_pb2.CODING_REGION_CDS: 1,
    clinvar_pb2.CODING_REGION_THREE_PRIME_UTR: 2,
}

_INSERTION = re.compile(r'ins')
# A deletion-insertion in either spelling — `delins`, or `del` with the deleted bases written out —
# which deletes its endpoints rather than bracketing them.
_DELETION_INSERTION = re.compile(r'del[A-Za-z0-9]*ins')


@dataclasses.dataclass(frozen=True)
class Placement:
    """A ClinVar pool placed against one coding span of one transcript.

    Every record handed in comes back in exactly one group, so a caller reading a group knows what
    the rest of the pool became. `outside` and `other_transcript` are the two negatives kept apart:
    a record on another transcript was never comparable, where one on this transcript genuinely sits
    elsewhere in the coding sequence.

    Attributes:
        inside: The records the span holds — those covering at least one of its exonic bases, and
            an insertion whose point is interior to it.
        outside: The records on this transcript that the span does not hold.
        other_transcript: The records whose coordinates are numbered on another accession.
        unplaceable: The `clinvar_id` of every record of this pool that carried no coding span. The
            response's own `records_with_unparsed_hgvs` can name records outside the pool — it
            covers `this_variant` too — so these are that list narrowed to the pool, which is what
            makes the four groups reconcile against it.
    """

    inside: tuple[clinvar_pb2.ClinVarRecord, ...]
    outside: tuple[clinvar_pb2.ClinVarRecord, ...]
    other_transcript: tuple[clinvar_pb2.ClinVarRecord, ...]
    unplaceable: tuple[str, ...]


def _accession_base(accession: str) -> str:
    return accession.split('.', 1)[0]


def _ordinal(coordinate: clinvar_pb2.CodingCoordinate, record: str) -> tuple[int, int, int]:
    """One endpoint as (region rank, position ordered 5'->3', intron offset)."""
    rank = _REGION_RANK.get(coordinate.region)
    if rank is None:
        raise ValueError(f'{record} carries a coding span whose region is unstated, so it places no base')
    if coordinate.position <= 0:
        raise ValueError(f'{record} carries a coding position of {coordinate.position}; c. numbering has no 0')
    # The 5'UTR counts down toward the coding start, so its order is the negated position.
    five_prime = coordinate.region == clinvar_pb2.CODING_REGION_FIVE_PRIME_UTR
    return (rank, -coordinate.position if five_prime else coordinate.position, coordinate.intron_offset)


def _span_ordinals(start: int, end: int) -> tuple[int, int, int]:
    """The span as (region rank, first position, last position), both ordered 5'->3'.

    The two endpoints are held to one region: a 5'UTR position and a CDS one are numbered on
    different counters, so a span crossing the coding start states no single run of bases. Both
    callers stay inside one — a codon is three CDS positions, and an exon's coding span is what
    `Transcript.GetStructure` reports as `cds_start`/`cds_end`.
    """
    if start == 0 or end == 0:
        raise ValueError('c. numbering has no 0, so neither end of a span can be one')
    if (start < 0) != (end < 0):
        raise ValueError(
            f"the span c.{start} to c.{end} crosses the coding start; the 5'UTR and the CDS number their "
            'bases on separate counters, so one span cannot run across both'
        )
    if start > end:
        raise ValueError(f'the span c.{start} to c.{end} ends before it begins')
    return (0 if start < 0 else 1, start, end)


def _covers_an_exonic_base(span: clinvar_pb2.CodingSpan, record: str, rank: int, first: int, last: int) -> bool:
    """Whether the record's extent covers a base of the span at intron offset 0."""
    low, high = sorted((_ordinal(span.start, record), _ordinal(span.end, record)))
    # The positions the record covers at offset 0 in the span's own region, as a closed interval.
    lowest = first
    highest = last
    if low[0] > rank:
        return False
    if low[0] == rank:
        # An endpoint at a positive offset sits after its own base, so that base is not covered.
        lowest = max(lowest, low[1] + 1 if low[2] > 0 else low[1])
    if high[0] < rank:
        return False
    if high[0] == rank:
        highest = min(highest, high[1] - 1 if high[2] < 0 else high[1])
    return lowest <= highest


def _brackets_an_insertion(record: clinvar_pb2.ClinVarRecord) -> bool:
    """Whether the record's span brackets an insertion point rather than covering its endpoints.

    Raises:
        ValueError: If the record carries a coding span and no expression it was parsed from, since
            `c.1107_1108insG` and `c.1107_1108del` share a span and are held by different spans.
    """
    if not record.hgvs.strip():
        raise ValueError(
            f'{record.clinvar_id} carries a coding span and no expression it was parsed from, so whether '
            'its endpoints bracket an insertion point or cover bases cannot be told'
        )
    return _INSERTION.search(record.hgvs) is not None and _DELETION_INSERTION.search(record.hgvs) is None


def _brackets_a_point_inside(span: clinvar_pb2.CodingSpan, record: str, rank: int, first: int, last: int) -> bool:
    """Whether an insertion sits between two exonic bases the span itself holds."""
    return all(
        ordinal[0] == rank and ordinal[2] == 0 and first <= ordinal[1] <= last
        for ordinal in (_ordinal(span.start, record), _ordinal(span.end, record))
    )


def records_in_span(
    records: Iterable[clinvar_pb2.ClinVarRecord],
    *,
    transcript: str,
    start: int,
    end: int,
    unparsed: Iterable[str],
) -> Placement:
    """Split a ClinVar pool by whether each record falls inside one coding span.

    Args:
        records: The pool — `SearchCodingSpanResponse.records`, or `classified_in_gene`.
        transcript: The accession the span's coordinates are numbered on; a record is compared
            against it without its version run (module docstring).
        start: The span's first c. coordinate, in the encoding `ClinVar.SearchCodingSpan` takes:
            positive is the CDS, negative the 5'UTR. Both ends must be in one region.
        end: Its last, inclusive. A codon is its three bases; an exon is its `cds_start`/`cds_end`.
        unparsed: The `clinvar_id` of every record whose expression named no coding span, from the
            response's `records_with_unparsed_hgvs`. Read as the cross-check it is: what comes back
            is the pool's own share of it.

    Returns:
        The `Placement`.

    Raises:
        ValueError: If the span is malformed, if a record carries a coordinate that places no base,
            if a record carries a coding span and no expression it was parsed from, or if a record
            carries no coding span and is not among `unparsed` — that pairing is the response's own
            account of what it could not place, and a record missing from it would vanish from the
            tally unnoticed.
    """
    rank, first, last = _span_ordinals(start, end)
    reported = set(unparsed)
    unplaceable: list[str] = []
    inside: list[clinvar_pb2.ClinVarRecord] = []
    outside: list[clinvar_pb2.ClinVarRecord] = []
    other: list[clinvar_pb2.ClinVarRecord] = []
    for record in records:
        if not record.HasField('coding_span'):
            if record.clinvar_id not in reported:
                raise ValueError(
                    f'{record.clinvar_id} carries no coding span and is not among the records the response '
                    'reported as unplaceable; one of the two is wrong, and dropping it understates the span'
                )
            unplaceable.append(record.clinvar_id)
        elif _accession_base(record.coding_span.transcript) != _accession_base(transcript):
            other.append(record)
        elif _held(record, rank, first, last):
            inside.append(record)
        else:
            outside.append(record)
    return Placement(
        inside=tuple(inside),
        outside=tuple(outside),
        other_transcript=tuple(other),
        unplaceable=tuple(unplaceable),
    )


def _held(record: clinvar_pb2.ClinVarRecord, rank: int, first: int, last: int) -> bool:
    """Whether the span holds the record: an insertion by its point, everything else by its extent."""
    if _brackets_an_insertion(record):
        return _brackets_a_point_inside(record.coding_span, record.clinvar_id, rank, first, last)
    return _covers_an_exonic_base(record.coding_span, record.clinvar_id, rank, first, last)


def waiving_variant(
    record: clinvar_pb2.ClinVarRecord,
    *,
    basis: scoring.PathogenicVariantBasis,
    evidence: str = '',
) -> scoring.ExonPathogenicVariant:
    """Read one of SM18 §17's established pathogenic variants off a ClinVar record.

    §17 rests the waiver on variants classified **P** — not P/LP — so an aggregate ClinVar computes
    over submitters split between the two rungs reaches `ExonPathogenicVariant` as the LP it resolves
    to (`clinvar_classification.informative_class`) and is refused there. Whether the variant lies in
    the exon is not decided here either: `records_in_span` places it, and which placed records the
    waiver rests on is the analyst's.

    Args:
        record: The ClinVar record, for its identity, classification and review stars.
        basis: Which of §17's two grounds establishes the classification.
        evidence: What establishes it, required for a `WELL_ESTABLISHED` claim.

    Returns:
        The `scoring.ExonPathogenicVariant` the waiver names.

    Raises:
        ValueError: If the record names itself neither by accession nor by expression, if its
            classification is not P, or if the basis claimed is not backed by the record's review
            status.
    """
    name = ' '.join(part for part in (record.clinvar_id, record.hgvs) if part)
    if not name:
        raise ValueError('a waiving variant must be named, and this record carries neither an accession nor an HGVS')
    return scoring.ExonPathogenicVariant(
        variant=name,
        classification=clinvar_classification.informative_class(record.classification),
        basis=basis,
        review_stars=record.review_stars,
        evidence=evidence,
    )
