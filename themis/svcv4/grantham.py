"""The Grantham distance matrix and the four summable MIS_INF sub-rules (SM6).

Grantham distance (Grantham 1974, PMID 4843792) is a fixed pairwise amino-acid dissimilarity based
on side-chain composition, polarity, and volume. SVCv4 uses it in the missense informative-variant
comparison: a distinct-amino-acid informative variant with a *smaller* Grantham distance than the
VBC (a more conservative change that is nonetheless pathogenic) is pathogenic evidence; a *larger*
distance that is benign is benignity evidence.

The embedded matrix is the canonical published integer table (symmetric, zero diagonal, 190
distinct pairs; minimum Leu-Ile 5, maximum Cys-Trp 215).
"""

from __future__ import annotations

import dataclasses
import decimal
import re

from themis.rpc import clinvar_pb2
from themis.svcv4 import clinvar_classification, reference, scoring

# Upper-triangular Grantham distances (Grantham 1974). Each row lists distances from its amino acid
# to every amino acid after it in the canonical order below; the lookup is symmetric.
_ORDER = 'SRLPTAVGIFYCHQNKDEMW'
# Membership must be tested against the set: `'SR' in _ORDER` is a substring test that admits any
# run of the canonical order.
_CODES = frozenset(_ORDER)
_ROWS: dict[str, dict[str, int]] = {
    'S': {
        'R': 110,
        'L': 145,
        'P': 74,
        'T': 58,
        'A': 99,
        'V': 124,
        'G': 56,
        'I': 142,
        'F': 155,
        'Y': 144,
        'C': 112,
        'H': 89,
        'Q': 68,
        'N': 46,
        'K': 121,
        'D': 65,
        'E': 80,
        'M': 135,
        'W': 177,
    },
    'R': {
        'L': 102,
        'P': 103,
        'T': 71,
        'A': 112,
        'V': 96,
        'G': 125,
        'I': 97,
        'F': 97,
        'Y': 77,
        'C': 180,
        'H': 29,
        'Q': 43,
        'N': 86,
        'K': 26,
        'D': 96,
        'E': 54,
        'M': 91,
        'W': 101,
    },
    'L': {
        'P': 98,
        'T': 92,
        'A': 96,
        'V': 32,
        'G': 138,
        'I': 5,
        'F': 22,
        'Y': 36,
        'C': 198,
        'H': 99,
        'Q': 113,
        'N': 153,
        'K': 107,
        'D': 172,
        'E': 138,
        'M': 15,
        'W': 61,
    },
    'P': {
        'T': 38,
        'A': 27,
        'V': 68,
        'G': 42,
        'I': 95,
        'F': 114,
        'Y': 110,
        'C': 169,
        'H': 77,
        'Q': 76,
        'N': 91,
        'K': 103,
        'D': 108,
        'E': 93,
        'M': 87,
        'W': 147,
    },
    'T': {
        'A': 58,
        'V': 69,
        'G': 59,
        'I': 89,
        'F': 103,
        'Y': 92,
        'C': 149,
        'H': 47,
        'Q': 42,
        'N': 65,
        'K': 78,
        'D': 85,
        'E': 65,
        'M': 81,
        'W': 128,
    },
    'A': {
        'V': 64,
        'G': 60,
        'I': 94,
        'F': 113,
        'Y': 112,
        'C': 195,
        'H': 86,
        'Q': 91,
        'N': 111,
        'K': 106,
        'D': 126,
        'E': 107,
        'M': 84,
        'W': 148,
    },
    'V': {
        'G': 109,
        'I': 29,
        'F': 50,
        'Y': 55,
        'C': 192,
        'H': 84,
        'Q': 96,
        'N': 133,
        'K': 97,
        'D': 152,
        'E': 121,
        'M': 21,
        'W': 88,
    },
    'G': {
        'I': 135,
        'F': 153,
        'Y': 147,
        'C': 159,
        'H': 98,
        'Q': 87,
        'N': 80,
        'K': 127,
        'D': 94,
        'E': 98,
        'M': 127,
        'W': 184,
    },
    'I': {'F': 21, 'Y': 33, 'C': 198, 'H': 94, 'Q': 109, 'N': 149, 'K': 102, 'D': 168, 'E': 134, 'M': 10, 'W': 61},
    'F': {'Y': 22, 'C': 205, 'H': 100, 'Q': 116, 'N': 158, 'K': 102, 'D': 177, 'E': 140, 'M': 28, 'W': 40},
    'Y': {'C': 194, 'H': 83, 'Q': 99, 'N': 143, 'K': 85, 'D': 160, 'E': 122, 'M': 36, 'W': 37},
    'C': {'H': 174, 'Q': 154, 'N': 139, 'K': 202, 'D': 154, 'E': 170, 'M': 196, 'W': 215},
    'H': {'Q': 24, 'N': 68, 'K': 32, 'D': 81, 'E': 40, 'M': 87, 'W': 115},
    'Q': {'N': 46, 'K': 53, 'D': 61, 'E': 29, 'M': 101, 'W': 130},
    'N': {'K': 94, 'D': 23, 'E': 42, 'M': 142, 'W': 174},
    'K': {'D': 101, 'E': 56, 'M': 95, 'W': 110},
    'D': {'E': 45, 'M': 160, 'W': 181},
    'E': {'M': 126, 'W': 152},
    'M': {'W': 67},
}


def _build_matrix() -> dict[frozenset[str], int]:
    matrix = {}
    for source, targets in _ROWS.items():
        for target, distance in targets.items():
            matrix[frozenset((source, target))] = distance
    return matrix


_MATRIX = _build_matrix()

# The three-letter spellings protein HGVS uses (`p.(Trp26Cys)`), onto the matrix's one-letter keys.
_THREE_LETTER: dict[str, str] = {
    'ALA': 'A',
    'ARG': 'R',
    'ASN': 'N',
    'ASP': 'D',
    'CYS': 'C',
    'GLN': 'Q',
    'GLU': 'E',
    'GLY': 'G',
    'HIS': 'H',
    'ILE': 'I',
    'LEU': 'L',
    'LYS': 'K',
    'MET': 'M',
    'PHE': 'F',
    'PRO': 'P',
    'SER': 'S',
    'THR': 'T',
    'TRP': 'W',
    'TYR': 'Y',
    'VAL': 'V',
}

# The classifications the MIS_INF sub-rules score; a 'VUS' informative variant is accepted but
# contributes 0, and any other token is a caller error.
_SCORED = ('P', 'LP', 'B', 'LB')


def _single_letter(code: str) -> str:
    """The single-letter form of a standard amino-acid code, one- or three-letter, any case.

    Raises:
        ValueError: If the code names none of the 20 standard amino acids.
    """
    normalised = code.strip().upper()
    if normalised in _CODES:
        return normalised
    resolved = _THREE_LETTER.get(normalised)
    if resolved is None:
        raise ValueError(
            f'not a standard amino-acid code: {code!r}; expected a one-letter (G) or three-letter (Gly) code'
        )
    return resolved


def distance(aa1: str, aa2: str) -> int:
    """Return the Grantham distance between two amino acids.

    Args:
        aa1: First amino acid, as a one-letter (`G`) or three-letter (`Gly`) code, any case.
        aa2: Second amino acid, in either form.

    Returns:
        The distance (0 when equal, symmetric otherwise).

    Raises:
        ValueError: If either code is not one of the 20 standard amino acids.
    """
    first, second = _single_letter(aa1), _single_letter(aa2)
    if first == second:
        return 0
    return _MATRIX[frozenset((first, second))]


# A protein HGVS substitution: an optional accession, `p.`, an optional predicted-form bracket, and
# one reference/codon/alternate triple. Anything else — a frameshift, an extension, a deletion, a
# synonymous `=` — fails to match, which is the point: those are not substitutions to compare.
_SUBSTITUTION = re.compile(
    r'(?:[^:\s]+:)?p\.(?P<predicted>\()?'
    r'(?P<reference>[A-Za-z]{3}|[A-Za-z])(?P<codon>\d+)(?P<variant>[A-Za-z]{3}|[A-Za-z]|\*)'
    r'(?(predicted)\))'
)

# The spellings HGVS gives a termination codon. A nonsense change is a substitution by grammar and
# not by Grantham: `_MATRIX` holds the 20 standard residues, so it is named as its own refusal
# rather than reaching the "not a standard amino acid" one.
_TERMINATION = frozenset({'TER', '*'})

# HGVS operations whose three letters parse as a residue code would. They name an edit rather than a
# substitution, so they are refused as one rather than as an unrecognised amino acid.
_OPERATIONS = frozenset({'DEL', 'DUP', 'INS', 'EXT'})


@dataclasses.dataclass(frozen=True)
class Substitution:
    """One amino-acid substitution, parsed from a protein HGVS expression.

    Attributes:
        reference_aa: The reference residue, as a one-letter code.
        variant_aa: The residue it changes to, likewise.
        codon: The codon number the change is at.
        predicted: Whether the expression was the predicted form, `p.(Gly1166Arg)` — HGVS's mark for
            a protein consequence inferred from the coding change rather than observed.
    """

    reference_aa: str
    variant_aa: str
    codon: int
    predicted: bool

    @property
    def distance(self) -> int:
        """The substitution's Grantham distance."""
        return distance(self.reference_aa, self.variant_aa)


def substitution(hgvs_p: str) -> Substitution:
    """Parse a protein HGVS expression naming one amino-acid substitution.

    Takes both forms a caller holds: an observed change, with or without its protein accession
    (`NP_000123.1:p.Gly1166Arg`), and the predicted form `p.(Gly1166Arg)`.

    Args:
        hgvs_p: The protein HGVS expression.

    Returns:
        The `Substitution`.

    Raises:
        ValueError: If the expression is not a single amino-acid substitution — a frameshift, an
            extension, a deletion, a synonymous change or a nonsense one — or names a residue
            outside the 20 standard amino acids. Refused rather than read past, since every rule
            reading one of these compares two residues at one codon.
    """
    parsed = _SUBSTITUTION.fullmatch(hgvs_p.strip())
    if parsed is None:
        raise ValueError(
            f'{hgvs_p!r} is not a single amino-acid substitution; the missense comparisons take '
            'one, in the form NP_1.2:p.Gly1166Arg or the predicted p.(Gly1166Arg)'
        )
    if parsed.group('variant').upper() in _OPERATIONS:
        raise ValueError(
            f'{hgvs_p!r} names an edit rather than a single amino-acid substitution; the missense '
            'comparisons weigh substitutions'
        )
    if parsed.group('variant').upper() in _TERMINATION:
        raise ValueError(f'{hgvs_p!r} is a nonsense change; the missense comparisons weigh substitutions')
    return Substitution(
        reference_aa=_single_letter(parsed.group('reference')),
        variant_aa=_single_letter(parsed.group('variant')),
        codon=int(parsed.group('codon')),
        predicted=parsed.group('predicted') is not None,
    )


@dataclasses.dataclass(frozen=True)
class InformativeMissense:
    """One distinct missense informative variant, for the MIS_INF sub-rules.

    Attributes:
        classification: One of 'P', 'LP', 'B', 'LB' (classified under v4; see SM19 circularity /
            same-evidence prerequisites, which are the model's to enforce before passing it here).
        same_amino_acid: Whether it changes the same residue to the same amino acid as the VBC
            (distinct nucleotide, same codon result) — the same-codon sub-rules 1 and 4.
        grantham_distance: The variant's own normal-to-alt Grantham distance, required for the
            distinct-amino-acid sub-rules 2 and 3; ignored (may be None) when `same_amino_acid`.
    """

    classification: str
    same_amino_acid: bool
    grantham_distance: int | None = None


def mis_inf_points(
    ref: reference.Reference,
    vbc_grantham: int,
    informatives: tuple[InformativeMissense, ...],
    *,
    motif_qualifies: bool = False,
) -> decimal.Decimal:
    """Sum the four MIS_INF sub-rules plus the motif rule, capped to the MIS_INF range (SM6).

    The sub-rules (each over distinct variants; observation count is irrelevant):
      1. same-amino-acid P/LP: +4.0 first P, +2.0 each additional (same-codon strong weights);
      2. distinct-amino-acid P/LP with Grantham <= the VBC's: +2.0 first P, +1.0 each additional;
      3. distinct-amino-acid B/LB with Grantham >= the VBC's: -2.0 first B, -1.0 each additional;
      4. same-amino-acid B/LB: -4.0 first B, -2.0 each additional.
    A distinct-amino-acid variant that fails its Grantham comparison contributes nothing. The motif
    rule adds +2.0 once when a robustly-deleterious motif qualifies and there is no other
    informative variant (voided by any benign informative variant).

    Args:
        ref: The loaded reference (supplies the MIS_INF cap).
        vbc_grantham: The VBC's own normal-to-alt Grantham distance, for sub-rules 2 and 3.
        informatives: The distinct missense informative variants.
        motif_qualifies: Whether the residue sits in a robustly-deleterious motif (SM7).

    Returns:
        The summed, capped MIS_INF points.

    Raises:
        ValueError: If a distinct-amino-acid informative variant omits its Grantham distance, or a
            classification token is unrecognised.
    """
    for variant in informatives:
        if variant.classification not in _SCORED and variant.classification != 'VUS':
            raise ValueError(f'unrecognised informative-variant classification {variant.classification!r}')

    # A VUS informative variant scores 0 (SM6's explicit "VUS = 0.0" row); it is neither summed nor
    # does it void the motif rule. Same-amino-acid variants use the strong same-codon weights; a
    # distinct-amino-acid variant only counts if it passes its Grantham comparison against the VBC.
    same_aa = tuple(v.classification for v in informatives if v.same_amino_acid and v.classification in _SCORED)
    distinct_qualifying = []
    for variant in informatives:
        if variant.same_amino_acid or variant.classification not in _SCORED:
            continue
        if variant.grantham_distance is None:
            raise ValueError('distinct-amino-acid informative variant requires a Grantham distance')
        pathogenic = variant.classification in ('P', 'LP')
        benign = variant.classification in ('B', 'LB')
        if (pathogenic and variant.grantham_distance <= vbc_grantham) or (
            benign and variant.grantham_distance >= vbc_grantham
        ):
            distinct_qualifying.append(variant.classification)

    points = scoring.informative_points(same_aa, strong=True) + scoring.informative_points(tuple(distinct_qualifying))

    any_informative = any(v.classification in _SCORED for v in informatives)
    if motif_qualifies and not any_informative:
        points += decimal.Decimal(2)

    spec = ref.code('MIS_INF')
    return scoring.clamp(points, spec.low, spec.high)


def informative_from_record(
    record: clinvar_pb2.ClinVarRecord, *, protein_change: Substitution, vbc: Substitution
) -> InformativeMissense:
    """Read one candidate informative missense variant off a ClinVar record (SM6).

    What it derives is the two things the sub-rules key on and a record cannot state: whether the
    variant changes the codon to the same residue as the VBC, and its own Grantham distance. What it
    does not do is **select**: which candidates carry distinct, non-circular evidence is SM19's
    judgement and stays the analyst's, so this reads whichever record it is handed.

    The record's protein change is a caller input rather than a field, because `ClinVarRecord.hgvs`
    is the coding expression: the residue it produces is read off an annotation of that transcript,
    not off the record.

    Args:
        record: The ClinVar record, for its aggregate `classification`.
        protein_change: The substitution the record's variant makes.
        vbc: The substitution the variant being classified makes.

    Returns:
        The `InformativeMissense` for `grantham.mis_inf_points`.

    Raises:
        ValueError: If the two substitutions sit at different codons, or state different reference
            residues at the same one — SM6's sub-rules all compare within one codon, so neither is a
            variant these rules weigh — or if the record's classification is one they do not score.
    """
    if protein_change.codon != vbc.codon:
        raise ValueError(
            f'the record changes codon {protein_change.codon} and the VBC codon {vbc.codon}; every MIS_INF '
            'sub-rule compares within one codon, so this record is not an informative variant of it'
        )
    if protein_change.reference_aa != vbc.reference_aa:
        raise ValueError(
            f'codon {vbc.codon} is {vbc.reference_aa} in the VBC and {protein_change.reference_aa} in the '
            'record; the two expressions are numbered on different proteins'
        )
    return InformativeMissense(
        classification=clinvar_classification.informative_class(record.classification),
        same_amino_acid=protein_change.variant_aa == vbc.variant_aa,
        grantham_distance=protein_change.distance,
    )
