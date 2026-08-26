"""The variant forms the evidence rpcs accept, the stripping that reaches them, and the c. parse.

Three accepted forms, because the upstreams behind the rpcs are keyed differently:
``accepted_transcript_hgvs`` is the narrow one (a versioned RefSeq transcript HGVS, which is all
VariantValidator serves), ``accepted_hgvs`` the wide one (any reference sequence Ensembl's VEP HGVS
endpoint resolves), and ``accepted_protein_hgvs`` the protein one (a curated RefSeq protein
accession, the kind the ClinGen Allele Registry registers a protein allele id for). The first two
strip the same ClinVar decorations, so a ``ClinVarAllele.preferred_name`` chains into either.

``coding_span`` reads the other direction: it takes an expression an upstream wrote and returns the
c. coordinates it names, so a caller placing records against an exon table joins on numbers rather
than on strings. It answers ``None`` for an expression naming no c. span at all — a genomic or
cytogenetic ClinVar title, an uncertain-boundary one — which is a state its callers must surface
rather than drop.

Held apart from ``servicer`` so the upstream adapters can hold callers to the same precondition
without importing the service that composes them — as with ``errors``, whose ``InvalidRequestError``
these raise so a malformed variant reads as ``INVALID_ARGUMENT`` wherever it is caught.
"""

from __future__ import annotations

import dataclasses
import re

from themis.rpc import clinvar_pb2
from themis.services.evidence import errors

# Every run is bounded so a pathological expression can neither reach an upstream nor fill a message:
# the digits of an accession, and the change, whose longest real form is a delins carrying an
# inserted sequence.
_DIGITS = r'\d{1,15}'
_MAX_CHANGE = 1000

# ClinVar decorates an expression with the GENE SYMBOL — `NM_1.2(GENE):c.3G>C (p.Ala1Gly)` — which is
# redundant with the accession and is dropped. A symbol is alphanumeric with hyphens and never holds
# an underscore, so this cannot swallow the parenthetical standard HGVS uses to qualify a GENOMIC
# reference with the transcript it is read through (`NG_1.1(NM_2.3):c.…`).
_GENE_DECORATION = r'(?:\([A-Za-z0-9-]+\))?'
# An accession's version run, e.g. the `.3` of `NM_001042492.3`.
_VERSION_SUFFIX = re.compile(r'\.\d+')
# That qualifier is part of the expression and is kept — but only where a genomic reference can
# appear. `NM_1.2(NM_3.4):c.…` is not HGVS at all, so a transcript reference takes none.
_TRANSCRIPT_QUALIFIER = rf'(?:\([NX][MR]_{_DIGITS}(?:\.\d+)?\))?'


def _expression(reference: str, coordinates: str, change: str, *, qualifier: str = '') -> re.Pattern[str]:
    """An HGVS expression over ``reference``, with ClinVar's decorations matched around it.

    Groups 1 and 2 are the expression with those decorations dropped; ``qualifier`` is kept inside
    group 1 where the reference admits one.
    """
    return re.compile(rf'({reference}{qualifier}){_GENE_DECORATION}(:[{coordinates}]\.{change})(?:\s*\(p\.[^)]*\))?')


# Short-form repeat notation (`c.219GCC[5]`) is HGVS both upstreams behind this form resolve, and
# VariantValidator answers it with the tract expanded against the reference — so the change the two
# forms take is the same, and only the reference sequence, the coordinate system and the qualifier
# separate them.
_TRANSCRIPT_HGVS = _expression(rf'N[MR]_{_DIGITS}\.\d+', 'cn', rf'\S{{1,{_MAX_CHANGE}}}?')

# A reference sequence that names the assembly its coordinates belong to. A GENOMIC accession names
# it through its version (NC_000017.10 is GRCh37, .11 is GRCh38), so there a version is required. A
# transcript or protein accession carries sequence-relative coordinates, which are assembly-free, so
# its version is optional. What the list excludes is the bare chromosome name — see the design doc.
_ASSEMBLY_NAMING_REFERENCE = (
    rf'(?:N[CGTW]_{_DIGITS}\.\d+|[NX][MRP]_{_DIGITS}(?:\.\d+)?|ENS[TP]{_DIGITS}(?:\.\d+)?|LRG_{_DIGITS}(?:[tp]\d+)?)'
)
# Nothing but length is excluded from the change here: it goes into a URL path under `quote(safe='')`.
_HGVS = _expression(_ASSEMBLY_NAMING_REFERENCE, 'cgmnopr', rf'\S{{1,{_MAX_CHANGE}}}?', qualifier=_TRANSCRIPT_QUALIFIER)

# A versioned, curated RefSeq protein accession. Versioned because the Allele Registry answers an
# unversioned one with a 500 ("Unknown reference"), which would reach a caller as a retried fault
# rather than as the request error it is. `XP_` (RefSeq predicted) and Ensembl `ENSP` accessions are
# excluded because the registry answers both with a blank node rather than an allele id — an
# expression addressing nothing downstream, which likewise has to read as a request this rpc does
# not take.
_PROTEIN_REFERENCE = rf'NP_{_DIGITS}\.\d+'
_PROTEIN_HGVS = re.compile(rf'({_PROTEIN_REFERENCE}){_GENE_DECORATION}:p\.(\S{{1,{_MAX_CHANGE}}})')


def _observed_change(change: str) -> str | None:
    """A protein change with HGVS's predicted-consequence parentheses removed, or `None` if malformed.

    `p.(Arg175His)` and `p.Arg175His` name the same allele. The wrapper records that the protein change
    was *inferred* from the coding change rather than observed on the protein — a claim about the
    evidence, not about the allele — and the sources keyed on protein alleles carry the bare form.
    Only a pair enclosing the whole change is that wrapper: `p.Arg175_Gly176ins(5)` keeps its own,
    and so does a change whose first parenthesis closes before the end. An unbalanced change is not a
    change at all, and is refused here rather than passed on for an upstream to reject.
    """
    depth = 0
    encloses = change.startswith('(')
    for index, char in enumerate(change):
        depth += (char == '(') - (char == ')')
        if depth < 0:
            return None
        if depth == 0 and index < len(change) - 1:
            encloses = False
    if depth != 0:
        return None
    return change[1:-1] if encloses else change


def accepted_transcript_hgvs(subject: str, variant: str) -> str:
    """The variant stripped to the bare versioned RefSeq transcript HGVS its upstreams take.

    Args:
        subject: What to name as the rejecter in the message (an rpc, or the upstream).
        variant: The variant, in any of the renderings ClinVar wraps one in.

    Returns:
        ``NM_001042492.3:c.3496G>C`` — accession, version, and change, nothing else.

    Raises:
        errors.InvalidRequestError: If ``variant`` is not a versioned RefSeq transcript HGVS.
    """
    matched = _TRANSCRIPT_HGVS.fullmatch(variant)
    if matched is None:
        raise errors.InvalidRequestError(
            f'{subject} takes a versioned RefSeq transcript HGVS, e.g. NM_001042492.3:c.3496G>C; got {variant!r}'
        )
    return f'{matched[1]}{matched[2]}'


def accepted_hgvs(subject: str, variant: str) -> str:
    """The variant stripped to a bare HGVS expression over a reference that names its assembly.

    Wider than :func:`accepted_transcript_hgvs` in both the reference sequences it takes (Ensembl
    accessions, proteins, genomic accessions, LRG ids) and the coordinate systems (g./m./p./r./o.
    beside c./n.), because the VEP HGVS endpoint resolves all of them.

    Args:
        subject: What to name as the rejecter in the message (an rpc, or the upstream).
        variant: The variant, in any of the renderings ClinVar wraps one in.

    Returns:
        The bare expression — reference sequence, coordinate system, and change.

    Raises:
        errors.InvalidRequestError: If ``variant`` is not one. A bare chromosome name and a
            positional id are both refused, and for the same reason: neither names the assembly its
            coordinates belong to.
    """
    matched = _HGVS.fullmatch(variant)
    if matched is None:
        raise errors.InvalidRequestError(
            f'{subject} takes an HGVS expression over a reference sequence that names its assembly — a '
            f'transcript or protein accession, or a versioned genomic one, e.g. NM_001042492.3:c.3496G>C '
            f'or NC_000017.11:g.31232881G>C; got {variant!r}'
        )
    return f'{matched[1]}{matched[2]}'


@dataclasses.dataclass(frozen=True)
class CodingCoordinate:
    """One endpoint of a c. coordinate: which region numbers it, the number, and the intron offset.

    HGVS numbers three regions separately and restarts at 1 in each, so the number alone places
    nothing — ``c.-25``, ``c.25`` and ``c.*25`` are three different bases.

    Attributes:
        region: A ``clinvar_pb2.CodingRegion`` value.
        position: The number within ``region``, always positive (the ``-`` of a 5'UTR coordinate is
            the region, not a sign).
        intron_offset: 0 for an exonic base; ``+n`` counts from the preceding exon's last base and
            ``-n`` from the following exon's first, so ``c.3496+1`` is ``+1`` and ``c.3497-2`` is
            ``-2``.
    """

    region: int
    position: int
    intron_offset: int


@dataclasses.dataclass(frozen=True)
class CodingSpan:
    """The transcript-relative span an HGVS c. expression names.

    Attributes:
        transcript: The accession the coordinates are numbered on, as the upstream wrote it. A pool
            of ClinVar records is not written against one transcript, so a caller placing them in an
            exon table must compare this before it compares coordinates.
        start: The span's first coordinate.
        end: Its last; equal to ``start`` for a single-base change. A deletion can cross an exon
            boundary, so the two ends do not always fall in the same exon.
    """

    transcript: str
    start: CodingCoordinate
    end: CodingCoordinate


# One c. coordinate: an optional region marker, the number, an optional intron offset. The `-` of a
# 5'UTR coordinate and the `-` of an acceptor-side offset are the same character in different
# positions, which is why the two are matched separately rather than as one signed integer. The
# number refuses to end mid-run, so a coordinate longer than any transcript is unparsed, not truncated.
_COORDINATE = r'(?P<{name}_region>[-*])?(?P<{name}_position>[1-9]\d{{0,8}})(?!\d)(?P<{name}_offset>[+-]\d{{1,9}})?'
# The change the coordinates carry. It excludes the separators ClinVar joins two alleles with — the
# `, ` and ` AND ` of a genotype title, the `;` inside `c.[…;…]` — and the brackets of short-form
# repeat notation (`c.571ATG[1]`), which numbers only the tract's first base and leaves its extent to
# the reference. The whole expression is fullmatched, so each is unparsed rather than read as its
# first allele or as one base.
_CHANGE = rf'[^\s,;:\[\]]{{1,{_MAX_CHANGE}}}'
_CODING_SPAN = re.compile(
    rf'(?P<accession>[NX][MR]_{_DIGITS}(?:\.\d+)?){_GENE_DECORATION}:c\.'
    rf'{_COORDINATE.format(name="start")}(?:_{_COORDINATE.format(name="end")})?'
    rf'{_CHANGE}(?:\s*\(p\.[^)]*\))?'
)

_REGION_BY_MARKER = {
    None: clinvar_pb2.CODING_REGION_CDS,
    '-': clinvar_pb2.CODING_REGION_FIVE_PRIME_UTR,
    '*': clinvar_pb2.CODING_REGION_THREE_PRIME_UTR,
}


def _coordinate(matched: re.Match[str], name: str) -> CodingCoordinate:
    offset = matched[f'{name}_offset']
    return CodingCoordinate(
        region=_REGION_BY_MARKER[matched[f'{name}_region']],
        position=int(matched[f'{name}_position']),
        intron_offset=int(offset) if offset else 0,
    )


def coding_span(expression: str) -> CodingSpan | None:
    """The c. span an HGVS expression names, or ``None`` when it names none.

    Args:
        expression: An HGVS expression as an upstream wrote it, gene decoration and protein suffix
            included (``NM_000267.3(NF1):c.1521_1523delCTT (p.Phe508del)``).

    Returns:
        The ``CodingSpan``, or ``None`` where the expression does not name exactly one CERTAIN c.
        span: a genomic or cytogenetic rendering (ClinVar titles a copy-number record ``GRCh38/hg38
        17q11.2(chr17:31...)x1``), an uncertain-boundary one (``c.(?_-33)_(*1_?)del``), short-form
        repeat notation (``c.571ATG[1]``, whose tract extent is reference-dependent and so does not
        follow from the string), a coordinate system other than c., or a haplotype/genotype title
        naming SEVERAL alleles — reading one of those as its first allele, or as its first base,
        would place a record confidently in the wrong exon, which is worse than not placing it. The
        distinction matters to the caller either way: ``None`` is a record that cannot be placed, not
        a record that sits nowhere.
    """
    matched = _CODING_SPAN.fullmatch(expression.strip())
    if matched is None:
        return None
    start = _coordinate(matched, 'start')
    end = _coordinate(matched, 'end') if matched['end_position'] else start
    return CodingSpan(transcript=matched['accession'], start=start, end=end)


def accession_base(accession: str) -> str:
    """The accession with its version run removed, e.g. ``NM_001042492.3`` -> ``NM_001042492``.

    The key a join across annotation releases runs on: GTEx's GENCODE snapshot and
    VariantValidator's alignment release name the same transcript at different versions, so an
    exact-string join drops real matches.

    Args:
        accession: A versioned or unversioned transcript accession.

    Returns:
        The accession with the first ``.<digits>`` run dropped and anything after it kept, so a
        GENCODE pseudoautosomal duplicate (``ENST….2_PAR_Y``) keeps the suffix that distinguishes it
        from the X copy. The argument unchanged when it carries no version.
    """
    return _VERSION_SUFFIX.sub('', accession, count=1)


def accepted_protein_hgvs(subject: str, variant: str) -> str:
    """The variant stripped to the bare RefSeq protein HGVS, stated as an observed change.

    Both renderings of one protein allele are accepted and answer alike: the predicted form
    VariantValidator emits for a coding change (`NP_000537.3:p.(Arg175His)`) and the bare form the
    Allele Registry and the protein-keyed sources carry (`NP_000537.3:p.Arg175His`). The parentheses
    are dropped rather than passed on — the registry rejects them outright, and a caller holding the
    predicted form holds it because it is what `Resolve` returned.

    Args:
        subject: What to name as the rejecter in the message (an rpc, or the upstream).
        variant: The protein expression, in either rendering, gene decoration tolerated.

    Returns:
        ``NP_000537.3:p.Arg175His`` — accession, `p.`, and the change, nothing else.

    Raises:
        errors.InvalidRequestError: If `variant` is not a versioned, curated RefSeq protein HGVS. A
            bare change with no accession is refused (it names no sequence to read the position
            against), as are the predicted `XP_` and Ensembl `ENSP` accessions (the Allele Registry
            registers no allele id for either).
    """
    matched = _PROTEIN_HGVS.fullmatch(variant)
    change = _observed_change(matched[2]) if matched is not None else ''
    if matched is None or not change:
        raise errors.InvalidRequestError(
            f'{subject} takes a curated RefSeq protein HGVS, e.g. NP_001035957.1:p.Gly1166Arg or the '
            f'predicted-consequence form NP_001035957.1:p.(Gly1166Arg); got {variant!r}'
        )
    return f'{matched[1]}:p.{change}'
