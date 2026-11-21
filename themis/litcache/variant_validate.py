"""The validation step: weaver's verdict on every candidate in a mention set, each candidate's anchor, and the allele.

Deterministic and free (docs/design/variant-extraction.md). The anchor is computed here from the paper's text and the
stores, not taken from the agent, and the allele a reader sees is chosen here, so a weaver fix re-chooses it without
the agent running again.
"""

from __future__ import annotations

import collections
import dataclasses
import functools
import itertools
import re
from collections.abc import Callable, Sequence
from typing import NamedTuple

import weaver

from themis.litcache import variant_fit, variant_reference, variant_scan, variant_versions
from themis.litcache.models import variants_pb2

# The anchor and choice rules; a change gives every paper validation work.
VALIDATOR_VERSION = '12'

_ACCESSION = re.compile(r'\b((?:NM|NR|NP|XM|XR|XP|NC|NG|NT|NW)_\d+|ENS[TPG]\d{11}|LRG_\d+)(?:\.(\d+))?')
_BUILDS = {
    'GRCh38': ('grch38', 'hg38'),
    'GRCh37': ('grch37', 'hg19'),
}
# From most trusted to least; a candidate's rank is its anchor's position here.
_ANCHOR_ORDER = (
    variants_pb2.ANCHOR_STATED_ACCESSION,
    variants_pb2.ANCHOR_GENE_FIT,
    variants_pb2.ANCHOR_MANE,
    variants_pb2.ANCHOR_GENOMIC_STATED_BUILD,
    variants_pb2.ANCHOR_STATED_OTHER_VERSION,
    variants_pb2.ANCHOR_FLANKING,
    variants_pb2.ANCHOR_OTHER_TRANSCRIPT,
    variants_pb2.ANCHOR_GENOMIC_UNSTATED_BUILD,
    variants_pb2.ANCHOR_TIE_BREAK,
)
# The defaults a printed protein change overrules; every other anchor rests on something the paper states.
_OVERRULED_BY_PAIRING = frozenset({variants_pb2.ANCHOR_MANE, variants_pb2.ANCHOR_OTHER_TRANSCRIPT})
# A description that names no change, an unknown one, or a splicing effect of unknown outcome names no allele:
# c.842C=, p.(=), p.(?), r.spl?.
_NO_ALLELE = re.compile(r'^(?:[cgmnr]\.\d+[ACGTUacgtu]?=|[cgmnrp]\.\(?(?:[=?]|spl\?)\)?)$')
# A pass with nothing to check is chosen only on these.
_TRUSTED_FOR_UNCHECKED = (
    variants_pb2.ANCHOR_STATED_ACCESSION,
    variants_pb2.ANCHOR_GENE_FIT,
    variants_pb2.ANCHOR_MANE,
)


def _normalise(text: str) -> str:
    """Letters and digits alone, lowercased.

    A sentence the agent copied then matches the text whatever markup, spacing, hyphenation or punctuation the
    rendering put around its words.
    """
    return re.sub(r'[^0-9a-z]', '', text.lower())


def _occurs(sentence: str, normalised_text: str) -> bool:
    """Whether the text bears out a copied sentence; one with no letter or digit to match bears out nothing."""
    needle = _normalise(sentence)
    return bool(needle) and needle in normalised_text


@dataclasses.dataclass(frozen=True)
class _Stated:
    """The accessions and builds the paper states, where its text bears them out."""

    versions: dict[str, set[str | None]]  # accession base -> the versions printed (None: printed unversioned)
    builds: set[str]


def _stated(
    mention_set: variants_pb2.MentionSet,
    mention: variants_pb2.Mention,
    text: str,
    printed: Sequence[variant_scan.PrintedAccession],
) -> _Stated:
    """What the paper states for a mention, and the builds it declares.

    The accessions stated are every one the text prints, the mention's own, and those the agent declared from a
    sentence the text holds.
    """
    versions: dict[str, set[str | None]] = {}
    for accession in printed:
        stated = versions.setdefault(accession.base, set())
        stated.update(accession.versions)
        if accession.unversioned:
            stated.add(None)

    def add(found: str) -> None:
        for match in _ACCESSION.finditer(found):
            versions.setdefault(match[1], set()).add(match[2])

    add(mention.verbatim)
    for reference in mention_set.references:
        if _occurs(reference.sentence, text) and reference.accession in reference.sentence:
            add(reference.accession)
    builds = set()
    for declared in mention_set.builds:
        if not _occurs(declared.sentence, text):
            continue
        lowered = declared.build.lower()
        builds.update(build for build, names in _BUILDS.items() if any(n in lowered for n in names))
    return _Stated(versions, builds)


def _anchor(
    candidate: variants_pb2.Candidate,
    accession: str,
    mention: variants_pb2.Mention,
    stated: _Stated,
    fitted: frozenset[str],
    lines: Sequence[str],
    references: variant_reference.References,
) -> variants_pb2.Anchor:
    base, _, version = accession.partition('.')
    stated_versions = dict(stated.versions)
    # The protein of a stated transcript is stated too: at its version, or at any version when the transcript's is
    # not printed.
    for transcript_base, printed in stated.versions.items():
        for v in printed:
            versions = references.held_versions(transcript_base) if v is None else [f'{transcript_base}.{v}']
            for transcript in versions:
                protein = references.protein_of(transcript)
                if protein is not None:
                    p_base, _, p_version = protein.partition('.')
                    stated_versions.setdefault(p_base, set()).add(None if v is None else p_version)
    versions_stated = stated_versions.get(base, set())
    if version in versions_stated or None in versions_stated:
        return variants_pb2.ANCHOR_STATED_ACCESSION
    if accession in fitted:
        return variants_pb2.ANCHOR_GENE_FIT
    if versions_stated:
        # A version the paper does not print that is MANE keeps MANE's rank, the better of the two.
        return variants_pb2.ANCHOR_MANE if references.is_mane(accession) else variants_pb2.ANCHOR_STATED_OTHER_VERSION
    if references.is_mane(accession):
        return variants_pb2.ANCHOR_MANE
    build = references.build_of(accession)
    if build is not None:
        return (
            variants_pb2.ANCHOR_GENOMIC_STATED_BUILD
            if build in stated.builds
            else variants_pb2.ANCHOR_GENOMIC_UNSTATED_BUILD
        )
    if candidate.HasField('flanking') and _flanking_holds(candidate.flanking, mention, lines, references):
        return variants_pb2.ANCHOR_FLANKING
    return variants_pb2.ANCHOR_OTHER_TRANSCRIPT


class MisplacedOccurrenceError(ValueError):
    """A mention set places an occurrence past the end of the text it was read from: so it will on every attempt."""


def _printed_at(occurrence: variants_pb2.Occurrence, lines: Sequence[str]) -> str:
    """The text an occurrence is printed in: its line and the next, where a string may run on; none for a page alone."""
    if not occurrence.HasField('line'):
        return ''
    if occurrence.line > len(lines):
        raise MisplacedOccurrenceError(
            f"an occurrence is on line {occurrence.line}, past the text's {len(lines)} lines"
        )
    return ' '.join(lines[occurrence.line - 1 : occurrence.line + 1])


def _occurrences(sequence: str, stretch: str) -> int:
    """How often `stretch` occurs in `sequence`, overlapping occurrences included (ATAT is twice in ATATAT)."""
    return len(re.findall(f'(?={re.escape(stretch)})', sequence))


def _flanking_holds(
    flanking: str, mention: variants_pb2.Mention, lines: Sequence[str], references: variant_reference.References
) -> bool:
    """Whether the stretch is printed where the mention is and matches the gene's transcripts once."""
    stretch = ''.join(flanking.upper().split())
    printed = ''.join(' '.join(_printed_at(o, lines) for o in mention.occurrences).upper().split())
    if not stretch or stretch not in printed or not mention.HasField('gene'):
        return False
    reverse = stretch.translate(str.maketrans('ACGT', 'TGCA'))[::-1]
    # RefSeq's and Ensembl's MANE Select are one sequence: counted once, as a palindromic stretch is.
    sequences = {references.sequence(t.accession, 1) for t in references.transcripts(mention.gene) if t.mane_select}
    hits = sum(_occurrences(s, stretch) + (_occurrences(s, reverse) if reverse != stretch else 0) for s in sequences)
    return hits == 1


ONE_LETTER = {
    'Ala': 'A',
    'Arg': 'R',
    'Asn': 'N',
    'Asp': 'D',
    'Cys': 'C',
    'Gln': 'Q',
    'Glu': 'E',
    'Gly': 'G',
    'His': 'H',
    'Ile': 'I',
    'Leu': 'L',
    'Lys': 'K',
    'Met': 'M',
    'Phe': 'F',
    'Pro': 'P',
    'Ser': 'S',
    'Thr': 'T',
    'Trp': 'W',
    'Tyr': 'Y',
    'Val': 'V',
    'Ter': '*',
}
_THREE_LETTER = {one: three for three, one in ONE_LETTER.items()}
_CODONS = {
    a + b + c: _THREE_LETTER.get(residue, residue)
    for (a, b, c), residue in zip(
        ((a, b, c) for a in 'TCAG' for b in 'TCAG' for c in 'TCAG'),
        'FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG',
        strict=True,
    )
}
_START_SUBSTITUTION = re.compile(r'^c\.([123])([ACGT])>([ACGT])$')


# A substitution as a mention prints it, and as a candidate states it: residue or base, position, replacement.
_PRINTED_RESIDUES = re.compile(r'(?<![A-Za-z])([A-Z][a-z]{2}|[A-Z])(\d+)([A-Z][a-z]{2}|[A-Z*])(?![a-z])')
_PRINTED_BASES = re.compile(r'(?<![\d+-])(\d+)([ACGT])>([ACGT])')
_STATED_RESIDUES = re.compile(r'^p\.\(?([A-Z][a-z]{2})(\d+)([A-Z][a-z]{2})\)?$')
_STATED_BASES = re.compile(r'^[cgmn]\.(\d+)([ACGT])>([ACGT])$')


def _one_letter(residue: str) -> str:
    return ONE_LETTER.get(residue, residue)


def _reverses_printed(hgvs: str, verbatim: str) -> str | None:
    """Why a candidate states its mention's printed substitution backwards, or None where it does not.

    The reverse change validates wherever the sequence holds the printed new residue (another version of the
    protein, say), but it is not the change the paper prints. Only an exact reversal at the printed position is
    caught: other differences have readings the text alone cannot rule out, such as C677T, a base change printed in
    the shape of a residue change.
    """
    change = hgvs.partition(':')[2]
    if stated_residues := _STATED_RESIDUES.match(change):
        position, ref, alt = stated_residues[2], _one_letter(stated_residues[1]), _one_letter(stated_residues[3])
        printed = {(m[2], _one_letter(m[1]), _one_letter(m[3])) for m in _PRINTED_RESIDUES.finditer(verbatim)}
    elif stated_bases := _STATED_BASES.match(change):
        position, ref, alt = stated_bases[1], stated_bases[2], stated_bases[3]
        printed = {(m[1], m[2], m[3]) for m in _PRINTED_BASES.finditer(verbatim)}
    else:
        return None
    if ref == alt or (position, alt, ref) not in printed or (position, ref, alt) in printed:
        return None
    return f'the mention prints {alt}{position}{ref}; the candidate states {ref}{position}{alt}'


def _protein_change(variant: weaver.Variant) -> str:
    """A protein change in one spelling: three-letter residues, Ter for a stop, no brackets, no frameshift tail."""
    change = variant.format().partition(':')[2].replace('(', '').replace(')', '')
    change = re.sub(r'[A-Z*](?![a-z])', lambda m: _THREE_LETTER.get(m[0], m[0]), change)
    return re.sub(r'fs(Ter)?(\d+|\?)?$', 'fs', change)


@dataclasses.dataclass(frozen=True)
class Identity:
    """What an expression names, as weaver's GA4GH VRS identifiers: equal identifiers name one allele.

    Attributes:
        expression: The expression, compared as written when neither identifier could be made.
        allele: A nucleotide change's identifier on the genome it projects to, whichever build or locus that is: what
            two nucleotide changes are compared by.
        nucleotide: The same identifier where that genome is a GRCh38 primary-assembly chromosome, else None: what a
            verdict records, so one allele has one whatever build, transcript or version it was written on.
        protein: A protein change's identifier, on its protein's sequence, so proteins of one sequence share it; for
            a c. change, its predicted protein change's.
        protein_key: For a protein change VRS has no allele for (a frameshift), its protein's sequence digest and the
            change in one spelling.
        at_nucleotide: Whether the expression is a nucleotide change (c., g., n., m.) rather than a protein change.
    """

    expression: str
    allele: str | None = None
    nucleotide: str | None = None
    protein: str | None = None
    protein_key: str | None = None
    at_nucleotide: bool = False

    def same(self, other: Identity) -> bool:
        """Whether both name one allele, compared at the most specific level both reach.

        Two nucleotide changes are compared by their alleles alone: a shared protein change does not make them one.
        """
        if self.allele is not None and other.allele is not None:
            return self.allele == other.allele
        if self.at_nucleotide and other.at_nucleotide:
            return self.expression == other.expression
        mine, theirs = self.protein or self.protein_key, other.protein or other.protein_key
        if mine is not None and theirs is not None:
            return mine == theirs
        return self.expression == other.expression

    def key(self) -> str:
        """One string per allele: the most specific identity reached."""
        return self.nucleotide or self.allele or self.protein or self.protein_key or self.expression


def _protein_identity(
    variant: weaver.Variant, mapper: weaver.VariantMapper, references: variant_reference.References
) -> tuple[str | None, str | None]:
    try:
        return mapper.vrs_id(variant), None
    except weaver.HGVSError:
        digest = references.digest(variant.ac)
        return None, f'{digest}:{_protein_change(variant)}' if digest is not None else None


def _on_grch38(variant: weaver.Variant, mapper: weaver.VariantMapper, references: variant_reference.References) -> bool:
    """Whether a nucleotide change projects to a GRCh38 primary-assembly chromosome, where its identifier is made.

    A GRCh37 coordinate, or a transcript version placed only on a patch or alternate locus, does not: its identifier
    joins nothing written on GRCh38, and lifting over is not done.
    """
    kind = variant.coordinate_type
    try:
        if kind == 'c':
            genomic = mapper.c_to_g(variant)
        elif kind == 'n':
            genomic = mapper.n_to_g(variant)
        elif kind in ('g', 'm'):
            genomic = variant
        else:
            return False
    except (weaver.HGVSError, ValueError, KeyError):
        return False
    return genomic.ac.startswith('NC_') and references.build_of(genomic.ac) == 'GRCh38'


def identity(expression: str, references: variant_reference.References) -> Identity:
    """What an expression names, by weaver's VRS identifiers; an expression that does not resolve keeps only itself."""
    try:
        variant = weaver.parse(expression)
        mapper = references.mapper(variant.ac)
        if variant.coordinate_type == 'p':
            protein, key = _protein_identity(variant, mapper, references)
            return Identity(expression, protein=protein, protein_key=key)
        if variant.coordinate_type != 'c':
            allele = mapper.vrs_id(variant)
            nucleotide = allele if _on_grch38(variant, mapper, references) else None
            return Identity(expression, allele=allele, nucleotide=nucleotide, at_nucleotide=True)
    except (weaver.HGVSError, ValueError, KeyError):
        # ValueError: weaver's answer to a projection of the wrong kind of variant, documented on each projection.
        return Identity(expression)
    # A c. change's protein needs no genome: a transcript version placed only on an alternate locus, or nowhere,
    # still names its protein change.
    try:
        allele = mapper.vrs_id(variant)
    except (weaver.HGVSError, ValueError, KeyError):
        allele = None
    try:
        protein, key = _protein_identity(mapper.c_to_p(variant, None), mapper, references)
    except (weaver.HGVSError, ValueError, KeyError):
        protein = key = None
    if allele is None and protein is None and key is None:
        return Identity(expression, at_nucleotide=True)
    nucleotide = allele if allele is not None and _on_grch38(variant, mapper, references) else None
    return Identity(
        expression, allele=allele, nucleotide=nucleotide, protein=protein, protein_key=key, at_nucleotide=True
    )


class _Pick(NamedTuple):
    """A mention's choice: the candidate chosen, the level ones that left it ambiguous, whether it is a guess."""

    choice: variants_pb2.Choice
    chosen: int | None
    level: tuple[int, ...] = ()
    guessed: bool = False


def _choose_among(
    among: Sequence[int],
    candidates: Sequence[variants_pb2.Candidate],
    verdicts: Sequence[variants_pb2.CandidateVerdict],
    declares_numbering: bool,
    references: variant_reference.References,
    *,
    paired: bool = False,
) -> _Pick:
    """The choice among the candidates at `among`.

    `paired` when each predicts the protein change printed beside it, which lifts the veto on placed candidates.
    """

    def key(index: int) -> tuple[int, int, int]:
        candidate = candidates[index]
        follows = any(r.declared and r.kind in variant_fit.NUMBERING_REWRITES for r in candidate.rewrites)
        # Where a paper prints several transcripts of a gene and a change holds on more than one, MANE's is taken.
        mane = references.is_mane(weaver.parse(candidate.hgvs).ac)
        return (
            _ANCHOR_ORDER.index(verdicts[index].anchor),
            0 if follows or not declares_numbering else 1,
            0 if mane else 1,
        )

    validated = [
        i
        for i in among
        if verdicts[i].outcome == variants_pb2.OUTCOME_VALIDATED
        and not _NO_ALLELE.match(candidates[i].hgvs.partition(':')[2])
    ]
    vetoes = [
        i
        for i in validated
        if not paired
        and variant_fit.placed(candidates[i])
        and verdicts[i].anchor == variants_pb2.ANCHOR_OTHER_TRANSCRIPT
    ]
    eligible = [i for i in validated if i not in vetoes]
    if not eligible:
        eligible = [
            i
            for i in among
            if verdicts[i].outcome == variants_pb2.OUTCOME_NOTHING_TO_CHECK
            and verdicts[i].anchor in _TRUSTED_FOR_UNCHECKED
            and not variant_fit.placed(candidates[i])
            and not _NO_ALLELE.match(candidates[i].hgvs.partition(':')[2])
        ]
    if not eligible:
        return _Pick(variants_pb2.CHOICE_NONE, None)
    best = min(key(i) for i in eligible)
    level = [i for i in eligible if key(i) == best]
    if verdicts[level[0]].anchor == variants_pb2.ANCHOR_OTHER_TRANSCRIPT:
        level += vetoes
    # A change moved to a newer version names what its source names there: tied with it, the source yields to it.
    updated = {
        r.source for i in level for r in candidates[i].rewrites if r.kind == variants_pb2.REWRITE_KIND_VERSION_UPDATE
    }
    level = [i for i in level if candidates[i].hgvs not in updated]
    alleles = [identity(candidates[i].hgvs, references) for i in level]
    # Every pair, since `same` is not transitive: a p. agrees with two c. changes that differ from each other.
    if not all(a.same(b) for a, b in itertools.combinations(alleles, 2)):
        return _Pick(variants_pb2.CHOICE_AMBIGUOUS, None, tuple(level))
    # Of candidates naming one allele, one that reaches the nucleotide identity says the most about it.
    return _Pick(
        variants_pb2.CHOICE_CHOSEN,
        next((i for i, allele in zip(level, alleles, strict=True) if allele.nucleotide is not None), level[0]),
    )


def _new_start_residue(variant: weaver.Variant, references: variant_reference.References) -> str | None:
    """The residue a start codon reads after a substitution in it, or None for any other change."""
    match = _START_SUBSTITUTION.match(variant.format().partition(':')[2])
    cds = references.cds_start(variant.ac)
    if match is None or cds is None:
        return None
    codon = list(references.sequence(variant.ac, cds, cds + 2))
    position, ref, alt = int(match[1]) - 1, match[2], match[3]
    if codon[position] != ref:
        return None
    codon[position] = alt
    return _CODONS.get(''.join(codon))


def _predicted_changes(hgvs: str, references: variant_reference.References) -> frozenset[str]:
    """The spellings of a c. change's predicted protein change a paper prints, in `_protein_change`'s spelling.

    A start loss is predicted as p.Met1? but often printed with the residue its new codon reads (p.Met1Val for
    c.1A>G), so both spellings count; empty for anything that is not a c. change or does not project.
    """
    try:
        variant = weaver.parse(hgvs)
        if variant.coordinate_type != 'c':
            return frozenset()
        predicted = _protein_change(references.mapper(variant.ac).c_to_p(variant, None))
        if predicted != 'p.Met1?':
            return frozenset({predicted})
        residue = _new_start_residue(variant, references)
    except (weaver.HGVSError, ValueError, KeyError):
        return frozenset()
    return frozenset({predicted} if residue is None else {predicted, f'p.Met1{residue}'})


def _projects_to(hgvs: str, printed: frozenset[str], references: variant_reference.References) -> bool:
    """Whether a c. change's predicted protein change is one of the printed ones."""
    return bool(_predicted_changes(hgvs, references) & printed)


def _line_partners(
    mention_set: variants_pb2.MentionSet, references: variant_reference.References
) -> list[frozenset[int]]:
    """Per mention, the other mentions of its gene (by current symbol) printed on a line it is printed on.

    A table row or a sentence that prints a change at both levels (c.409C>T, p.Arg137Trp) is recorded as two
    mentions; the line they share is what pairs them. A mention without a known gene pairs with none.
    """
    keys: list[set[tuple[str, int]]] = []
    by_key: dict[tuple[str, int], list[int]] = {}
    for i, mention in enumerate(mention_set.mentions):
        symbol = references.current_symbol(mention.gene) if mention.HasField('gene') else None
        found = set() if symbol is None else {(symbol, o.line) for o in mention.occurrences if o.HasField('line')}
        keys.append(found)
        for key in found:
            by_key.setdefault(key, []).append(i)
    return [frozenset(j for key in found for j in by_key[key] if j != i) for i, found in enumerate(keys)]


def _protein_sources(mention: variants_pb2.Mention) -> list[tuple[variants_pb2.Candidate, str]]:
    """A mention's p. candidates, each with its change in `_protein_change`'s spelling; none it states backwards."""
    found = []
    for candidate in mention.candidates:
        try:
            variant = weaver.parse(candidate.hgvs)
        except weaver.HGVSError:
            continue
        if variant.coordinate_type == 'p' and _reverses_printed(candidate.hgvs, mention.verbatim) is None:
            found.append((candidate, _protein_change(variant)))
    return found


def _printed_proteins(mention: variants_pb2.Mention) -> frozenset[str]:
    """The protein changes a mention's candidates write, in `_protein_change`'s spelling."""
    return frozenset(change for _, change in _protein_sources(mention))


def _only_protein(mention: variants_pb2.Mention) -> bool:
    """Whether every candidate of a mention that parses is a p. change: a mention a pairing may re-choose."""
    for candidate in mention.candidates:
        try:
            if weaver.parse(candidate.hgvs).coordinate_type != 'p':
                return False
        except weaver.HGVSError:
            continue
    return True


def _record_choice(
    verdict: variants_pb2.MentionVerdict,
    mention: variants_pb2.Mention,
    chosen: int,
    references: variant_reference.References,
    *,
    guessed: bool,
) -> None:
    """Choose a candidate, replacing any earlier choice.

    A guess is marked on the chosen candidate alone, and only where it sits on a transcript the paper does not name:
    a candidate on a sequence the paper names keeps its own anchor.
    """
    if verdict.HasField('chosen') and verdict.candidates[verdict.chosen].anchor == variants_pb2.ANCHOR_TIE_BREAK:
        verdict.candidates[verdict.chosen].anchor = variants_pb2.ANCHOR_OTHER_TRANSCRIPT
    if guessed and verdict.candidates[chosen].anchor == variants_pb2.ANCHOR_OTHER_TRANSCRIPT:
        verdict.candidates[chosen].anchor = variants_pb2.ANCHOR_TIE_BREAK
    verdict.choice = variants_pb2.CHOICE_CHOSEN
    verdict.chosen = chosen
    verdict.ClearField('nucleotide_vrs_id')
    verdict.ClearField('protein_vrs_id')
    named = identity(mention.candidates[chosen].hgvs, references)
    if named.nucleotide is not None:
        verdict.nucleotide_vrs_id = named.nucleotide
    if named.protein is not None:
        verdict.protein_vrs_id = named.protein


def _guessed(verdict: variants_pb2.MentionVerdict) -> bool:
    """Whether a mention's choice is a guess: what it settles is a guess too."""
    return verdict.HasField('chosen') and verdict.candidates[verdict.chosen].anchor == variants_pb2.ANCHOR_TIE_BREAK


def _rank(verdict: variants_pb2.MentionVerdict) -> int:
    """How firm a mention's choice is: its anchor's place in the order, past the end where nothing is chosen."""
    if verdict.choice != variants_pb2.CHOICE_CHOSEN:
        return len(_ANCHOR_ORDER)
    return _ANCHOR_ORDER.index(verdict.candidates[verdict.chosen].anchor)


@dataclasses.dataclass(frozen=True)
class _Half:
    """One mention of a printed c./p. pair: its candidates, its verdict, and how a candidate added to it is anchored."""

    mention: variants_pb2.Mention
    verdict: variants_pb2.MentionVerdict
    anchor_of: Callable[[str], variants_pb2.Anchor]

    def chosen(self) -> str | None:
        return self.mention.candidates[self.verdict.chosen].hgvs if self.verdict.HasField('chosen') else None


def _move_to(
    half: _Half, hgvs: str, source: variants_pb2.Candidate, note: str, references: variant_reference.References
) -> int | None:
    """The index of a candidate holding `hgvs` that validates, adding it moved from `source` if the mention lacks it.

    A move that states the mention's printed substitution backwards is not made.
    """
    for k, candidate in enumerate(half.mention.candidates):
        if candidate.hgvs == hgvs:
            return k if half.verdict.candidates[k].outcome == variants_pb2.OUTCOME_VALIDATED else None
    if _reverses_printed(hgvs, half.mention.verbatim) is not None:
        return None
    check = references.check(hgvs)
    if check.outcome != variants_pb2.OUTCOME_VALIDATED:
        return None
    moved = half.mention.candidates.add(hgvs=hgvs, rewrites=source.rewrites)
    moved.rewrites.add(kind=variants_pb2.REWRITE_KIND_PRINTED_PARTNER, note=note, source=source.hgvs)
    half.verdict.candidates.add(outcome=check.outcome, anchor=half.anchor_of(hgvs), detail=check.detail)
    return len(half.mention.candidates) - 1


def _p_follows_c(c: _Half, p: _Half, references: variant_reference.References) -> bool:
    """Choose the p. half on the protein the c. half's chosen transcript encodes; whether it could be."""
    chosen = c.chosen()
    if chosen is None:
        return False
    transcript = chosen.partition(':')[0]
    protein = references.protein_of(transcript)
    predicted = _predicted_changes(chosen, references)
    sources = [candidate for candidate, change in _protein_sources(p.mention) if change in predicted]
    if protein is None or not sources:
        return False
    current = p.chosen()
    if current is not None and current.startswith(f'{protein}:'):
        k: int | None = p.verdict.chosen
    else:
        source = sources[0]
        k = _move_to(
            p,
            f'{protein}:{source.hgvs.partition(":")[2]}',
            source,
            f'moved from {source.hgvs.partition(":")[0]} to {protein}, the protein of {transcript}, which the c. '
            'change printed beside it is on',
            references,
        )
    if k is None:
        return False
    _record_choice(p.verdict, p.mention, k, references, guessed=_guessed(c.verdict))
    return True


def _c_source(c: _Half) -> variants_pb2.Candidate | None:
    """The c. candidate whose change a move carries: the chosen one, else the agent's first c. candidate."""
    if c.verdict.HasField('chosen'):
        return c.mention.candidates[c.verdict.chosen]
    for candidate in c.mention.candidates:
        if any(r.kind in variant_fit.DERIVED_REWRITES for r in candidate.rewrites):
            continue
        try:
            if weaver.parse(candidate.hgvs).coordinate_type == 'c':
                return candidate
        except weaver.HGVSError:
            continue
    return None


def _c_follows_p(c: _Half, p: _Half, references: variant_reference.References) -> bool:
    """Choose the c. half on a transcript, at any held version, encoding the protein the p. half is chosen on."""
    chosen, source = p.chosen(), _c_source(c)
    gene = references.current_symbol(c.mention.gene) if c.mention.HasField('gene') else None
    if chosen is None or source is None or gene is None:
        return False
    protein, printed = chosen.partition(':')[0], frozenset({_protein_change(weaver.parse(chosen))})
    change = source.hgvs.partition(':')[2]
    current = c.chosen()
    if current is not None and references.protein_of(current.partition(':')[0]) == protein:
        _record_choice(c.verdict, c.mention, c.verdict.chosen, references, guessed=_guessed(p.verdict))
        return True
    versions = (v for t in references.transcripts(gene) for v in references.held_versions(t.accession) or [t.accession])
    for transcript in dict.fromkeys(versions):
        hgvs = f'{transcript}:{change}'
        if references.protein_of(transcript) != protein or not _projects_to(hgvs, printed, references):
            continue
        k = _move_to(
            c,
            hgvs,
            source,
            f'moved from {source.hgvs.partition(":")[0]} to {transcript}, which encodes {protein}, which the p. '
            'change printed beside it is on',
            references,
        )
        if k is not None:
            _record_choice(c.verdict, c.mention, k, references, guessed=_guessed(p.verdict))
            return True
    return False


def _settle_pair(c: _Half, p: _Half, references: variant_reference.References) -> None:
    """Place both halves of a printed c./p. pair on one transcript and its protein, as the firmer half has it.

    A p. half chosen on a default sequence (MANE's protein, an unnamed one) that cannot take the c. change follows the
    c. half instead: the default protein can hold the printed residue through another codon.
    """
    if _rank(c.verdict) <= _rank(p.verdict):
        _p_follows_c(c, p, references)
        return
    if _c_follows_p(c, p, references):
        return
    if p.verdict.candidates[p.verdict.chosen].anchor in _OVERRULED_BY_PAIRING:
        _p_follows_c(c, p, references)


def _settle_pairs(
    mention_set: variants_pb2.MentionSet,
    verdicts: variants_pb2.Verdicts,
    partners: Sequence[frozenset[int]],
    anchor_of: Callable[[int, str], variants_pb2.Anchor],
    references: variant_reference.References,
) -> None:
    """Place each change printed at both levels on one transcript and its protein.

    A table row or a sentence printing a change at both levels (c.409C>T, p.Arg137Trp) is two mentions, paired by the
    line they share and by the c. change predicting the p. one. Each half is chosen alone first; then the half on
    firmer ground settles the other, whose change is moved onto the matching sequence where it has no candidate there,
    and chosen there only if it holds. A firmer p. half also settles a c. half left unchosen. A pair the halves do not
    support is left as each half chose, and a partner holding a nucleotide candidate is never re-chosen as a p. half.
    """
    mentions, out = mention_set.mentions, verdicts.mentions
    for i, c_mention in enumerate(mentions):
        if _only_protein(c_mention):
            continue
        chosen = c_mention.candidates[out[i].chosen].hgvs if out[i].HasField('chosen') else None
        if chosen is not None and weaver.parse(chosen).coordinate_type != 'c':
            continue
        predicted = _predicted_changes(chosen, references) if chosen is not None else frozenset()
        for j in sorted(partners[i]):
            if not _only_protein(mentions[j]):
                continue
            if chosen is None and not out[j].HasField('chosen'):
                continue
            if chosen is not None and not predicted & _printed_proteins(mentions[j]):
                continue
            c = _Half(c_mention, out[i], functools.partial(anchor_of, i))
            p = _Half(mentions[j], out[j], functools.partial(anchor_of, j))
            _settle_pair(c, p, references)
            chosen = c_mention.candidates[out[i].chosen].hgvs if out[i].HasField('chosen') else None
            predicted = _predicted_changes(chosen, references) if chosen is not None else frozenset()


def _choose_evidenced(
    candidates: Sequence[variants_pb2.Candidate],
    verdicts: Sequence[variants_pb2.CandidateVerdict],
    declares_numbering: bool,
    references: variant_reference.References,
    printed_proteins: frozenset[str],
) -> _Pick:
    everything = range(len(candidates))
    choice = _choose_among(everything, candidates, verdicts, declares_numbering, references)
    overrulable = choice.choice != variants_pb2.CHOICE_CHOSEN or (
        choice.chosen is not None
        and verdicts[choice.chosen].anchor in _OVERRULED_BY_PAIRING
        and not _projects_to(candidates[choice.chosen].hgvs, printed_proteins, references)
    )
    if printed_proteins and overrulable:
        paired = [i for i in everything if _projects_to(candidates[i].hgvs, printed_proteins, references)]
        if paired:
            narrowed = _choose_among(paired, candidates, verdicts, declares_numbering, references, paired=True)
            # An ambiguous narrowing is kept: the guess is made among what the printed protein change leaves.
            if narrowed.choice != variants_pb2.CHOICE_NONE:
                return narrowed
    if choice.choice == variants_pb2.CHOICE_CHOSEN:
        return choice
    # A version update only settles a choice: one it leaves unsettled (a c. change moved where its protein change
    # could not be, say) is made as though there were none.
    written = [
        i
        for i in everything
        if not any(r.kind == variants_pb2.REWRITE_KIND_VERSION_UPDATE for r in candidates[i].rewrites)
    ]
    without_updates = _choose_among(written, candidates, verdicts, declares_numbering, references)
    if choice.choice == variants_pb2.CHOICE_AMBIGUOUS and without_updates.choice == variants_pb2.CHOICE_AMBIGUOUS:
        return choice
    return without_updates


def _coding_length(hgvs: str, references: variant_reference.References) -> int:
    """The residues the sequence a candidate is on codes for: a protein's length, a transcript's coding sequence's."""
    variant = weaver.parse(hgvs)
    if variant.coordinate_type == 'p':
        return len(references.sequence(variant.ac, 1))
    length = references.cds_length(variant.ac)
    if length is None:
        raise ValueError(f'{variant.ac} has no coding sequence')
    return length // 3


def _source_rank(hgvs: str) -> int:
    """Curated RefSeq first, then predicted RefSeq, then everything else (Ensembl)."""
    accession = hgvs.partition(':')[0]
    if accession.startswith(('NM_', 'NP_')):
        return 0
    if accession.startswith(('XM_', 'XP_')):
        return 1
    return 2


def _accession_order(hgvs: str) -> tuple[str, int]:
    base, _, version = hgvs.partition(':')[0].partition('.')
    return base, int(version or 0)


def _agreeing_coding_changes(
    level: Sequence[int], candidates: Sequence[variants_pb2.Candidate], references: variant_reference.References
) -> list[int] | None:
    """The level's candidates a guess is among: all of one coordinate type, else the c. ones its p. ones agree with.

    None where the level holds another coordinate type, or p. candidates writing more than one change.
    """
    by_kind: dict[str, list[int]] = collections.defaultdict(list)
    for i in level:
        by_kind[weaver.parse(candidates[i].hgvs).coordinate_type].append(i)
    if set(by_kind) in ({'c'}, {'p'}):
        return list(level)
    if set(by_kind) != {'c', 'p'} or len({candidates[i].hgvs.partition(':')[2] for i in by_kind['p']}) != 1:
        return None
    printed = _protein_change(weaver.parse(candidates[by_kind['p'][0]].hgvs))
    proteins = {candidates[i].hgvs.partition(':')[0] for i in by_kind['p']}
    return [
        i
        for i in by_kind['c']
        if references.protein_of(candidates[i].hgvs.partition(':')[0]) in proteins
        and _projects_to(candidates[i].hgvs, frozenset({printed}), references)
    ]


def _tie_break(
    level: Sequence[int],
    candidates: Sequence[variants_pb2.Candidate],
    verdicts: Sequence[variants_pb2.CandidateVerdict],
    references: variant_reference.References,
) -> int | None:
    """The level candidate a guess takes: RefSeq first, then the longest coding sequence, then the lowest accession.

    A guess is made only among candidates writing one c. change, or one p. change, each on a different transcript (or
    protein) of the gene that the paper does not name, versions of one counting as one, and at least one of them the
    agent's own; otherwise None. Disagreeing candidates on a sequence the paper names, on one sequence, or writing
    different changes are a disagreement in what the paper prints, not in which transcript it numbered against.

    Where the level holds both, the mention prints a c. change with its protein change: the guess is among the c.
    candidates whose transcript's protein carries the p. change and which predict it there.
    """
    if len(level) < 2 or any(verdicts[i].anchor != variants_pb2.ANCHOR_OTHER_TRANSCRIPT for i in level):
        return None
    group = _agreeing_coding_changes(level, candidates, references)
    if group is None or all(variant_fit.placed(candidates[i]) for i in group):
        return None
    parsed = [weaver.parse(candidates[i].hgvs) for i in group]
    changes = {candidates[i].hgvs.partition(':')[2] for i in group}
    if len(changes) != 1 or len({v.ac.partition('.')[0] for v in parsed}) != len(group):
        return None
    return min(
        group,
        key=lambda i: (
            _source_rank(candidates[i].hgvs),
            -_coding_length(candidates[i].hgvs, references),
            _accession_order(candidates[i].hgvs),
        ),
    )


def _choose(
    candidates: Sequence[variants_pb2.Candidate],
    verdicts: Sequence[variants_pb2.CandidateVerdict],
    declares_numbering: bool,
    references: variant_reference.References,
    printed_proteins: frozenset[str],
) -> _Pick:
    """The choice the evidence makes, or failing it a guess among unnamed transcripts."""
    pick = _choose_evidenced(candidates, verdicts, declares_numbering, references, printed_proteins)
    if pick.choice != variants_pb2.CHOICE_AMBIGUOUS:
        return pick
    guess = _tie_break(pick.level, candidates, verdicts, references)
    if guess is None:
        return pick
    return _Pick(variants_pb2.CHOICE_CHOSEN, guess, pick.level, guessed=True)


def _drop_unchosen_placements(mention: variants_pb2.Mention, verdict: variants_pb2.MentionVerdict) -> None:
    """Keep, of the candidates `spread` placed, only a chosen one: the rest a rerun reproduces."""
    chosen = verdict.chosen if verdict.HasField('chosen') else None
    keep = [k for k, candidate in enumerate(mention.candidates) if not variant_fit.placed(candidate) or k == chosen]
    if len(keep) == len(mention.candidates):
        return
    candidates = [mention.candidates[k] for k in keep]
    verdicts = [verdict.candidates[k] for k in keep]
    del mention.candidates[:]
    mention.candidates.extend(candidates)
    del verdict.candidates[:]
    verdict.candidates.extend(verdicts)
    if chosen is not None:
        verdict.chosen = keep.index(chosen)


def construction(references: variant_reference.References) -> variants_pb2.VariantValidationConstruction:
    """How `validate` makes verdicts over `references`: the record's construction, and what makes older ones stale."""
    return variants_pb2.VariantValidationConstruction(
        weaver_version=references.weaver_version(), stores=references.versions(), validator_version=VALIDATOR_VERSION
    )


def validate(
    mention_set: variants_pb2.MentionSet, *, text: str, references: variant_reference.References
) -> tuple[variants_pb2.MentionSet, variants_pb2.Verdicts, variants_pb2.VariantValidationConstruction]:
    """Weaver's verdicts on a mention set.

    Args:
        mention_set: What the extraction step recorded, perhaps completed by an earlier validation.
        text: The paper's text the mention set was read from: a declared accession or build is stated only where its
            declaring sentence occurs here, and an occurrence's line is a line of it.
        references: The reference stores.

    Returns:
        The mention set completed with the candidates derived from the agent's (version updates, changes moved to a
        transcript the fit singles out or to the printed partner's sequence, and a chosen change placed on every
        transcript of its gene); one verdict per mention, in its order; and how they were made.

    Raises:
        MisplacedOccurrenceError: An occurrence the validation reads is on a line past the end of the text.
    """
    printed = variant_scan.printed_accessions(text)
    lines = text.split('\n')
    text = _normalise(text)
    mention_set = variant_fit.without_derived(mention_set)
    fit = variant_fit.fit(mention_set, references)
    mention_set = variant_fit.complete(variant_versions.update(mention_set, references), fit, references)
    mention_set = variant_fit.spread(mention_set, fit, references)
    out = variants_pb2.Verdicts()
    partners = _line_partners(mention_set, references)
    proteins = [_printed_proteins(m) for m in mention_set.mentions]
    stateds = [_stated(mention_set, mention, text, printed) for mention in mention_set.mentions]
    for index, (mention, fitted) in enumerate(zip(mention_set.mentions, fit.fitted, strict=True)):
        stated = stateds[index]
        verdict = out.mentions.add()
        for candidate in mention.candidates:
            check = references.check(candidate.hgvs)
            anchor = variants_pb2.ANCHOR_UNSPECIFIED
            if check.outcome != variants_pb2.OUTCOME_UNPARSEABLE:
                accession = weaver.parse(candidate.hgvs).ac
                anchor = _anchor(candidate, accession, mention, stated, fitted, lines, references)
            outcome, detail = check.outcome, check.detail
            reversal = _reverses_printed(candidate.hgvs, mention.verbatim)
            if reversal is not None:
                outcome, detail = variants_pb2.OUTCOME_REVERSES_PRINTED, reversal
            verdict.candidates.add(outcome=outcome, anchor=anchor, detail=detail)
        declares_numbering = any(
            not c.HasField('gene') or (mention.HasField('gene') and c.gene == mention.gene)
            for c in mention_set.conventions
            if _occurs(c.sentence, text)
        )
        pick = _choose(
            mention.candidates,
            verdict.candidates,
            declares_numbering,
            references,
            frozenset(c for j in partners[index] for c in proteins[j]),
        )
        verdict.choice = pick.choice
        if pick.chosen is not None:
            _record_choice(verdict, mention, pick.chosen, references, guessed=pick.guessed)

    def anchor_of(index: int, hgvs: str) -> variants_pb2.Anchor:
        mention, candidate = mention_set.mentions[index], variants_pb2.Candidate(hgvs=hgvs)
        return _anchor(candidate, weaver.parse(hgvs).ac, mention, stateds[index], fit.fitted[index], lines, references)

    _settle_pairs(mention_set, out, partners, anchor_of, references)
    for mention, verdict in zip(mention_set.mentions, out.mentions, strict=True):
        _drop_unchosen_placements(mention, verdict)
    return mention_set, out, construction(references)
