"""The validation step: weaver's verdict on every candidate in a mention set, each candidate's anchor, and the allele.

Deterministic and free (docs/design/variant-extraction.md). The anchor is computed here from the paper's text and the
stores, not taken from the agent, and the allele a reader sees is chosen here, so a weaver fix re-chooses it without
the agent running again.
"""

from __future__ import annotations

import dataclasses
import itertools
import re
from collections.abc import Sequence

import weaver

from themis.litcache import variant_fit, variant_reference, variant_scan, variant_versions
from themis.litcache.models import variants_pb2

# The anchor and choice rules; a change gives every paper validation work.
VALIDATOR_VERSION = '11'

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
)
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


def protein_change(variant: weaver.Variant) -> str:
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
    """

    expression: str
    allele: str | None = None
    nucleotide: str | None = None
    protein: str | None = None
    protein_key: str | None = None

    def same(self, other: Identity) -> bool:
        """Whether both name one allele, compared at the most specific level both reach."""
        if self.allele is not None and other.allele is not None:
            return self.allele == other.allele
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
        return None, f'{digest}:{protein_change(variant)}' if digest is not None else None


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
        allele = mapper.vrs_id(variant)
        nucleotide = allele if _on_grch38(variant, mapper, references) else None
        if variant.coordinate_type != 'c':
            return Identity(expression, allele=allele, nucleotide=nucleotide)
        protein, key = _protein_identity(mapper.c_to_p(variant, None), mapper, references)
        return Identity(expression, allele=allele, nucleotide=nucleotide, protein=protein, protein_key=key)
    except (weaver.HGVSError, ValueError, KeyError):
        # ValueError: weaver's answer to a projection of the wrong kind of variant, documented on each projection.
        return Identity(expression)


def _choose_among(
    among: Sequence[int],
    candidates: Sequence[variants_pb2.Candidate],
    verdicts: Sequence[variants_pb2.CandidateVerdict],
    declares_numbering: bool,
    references: variant_reference.References,
) -> tuple[variants_pb2.Choice, int | None]:
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

    eligible = [
        i
        for i in among
        if verdicts[i].outcome == variants_pb2.OUTCOME_VALIDATED
        and not _NO_ALLELE.match(candidates[i].hgvs.partition(':')[2])
    ]
    if not eligible:
        eligible = [
            i
            for i in among
            if verdicts[i].outcome == variants_pb2.OUTCOME_NOTHING_TO_CHECK
            and verdicts[i].anchor in _TRUSTED_FOR_UNCHECKED
            and not _NO_ALLELE.match(candidates[i].hgvs.partition(':')[2])
        ]
    if not eligible:
        return variants_pb2.CHOICE_NONE, None
    best = min(key(i) for i in eligible)
    level = [i for i in eligible if key(i) == best]
    # A change moved to a newer version names what its source names there: tied with it, the source yields to it.
    updated = {
        r.source for i in level for r in candidates[i].rewrites if r.kind == variants_pb2.REWRITE_KIND_VERSION_UPDATE
    }
    level = [i for i in level if candidates[i].hgvs not in updated]
    alleles = [identity(candidates[i].hgvs, references) for i in level]
    # Every pair, since `same` is not transitive: a p. agrees with two c. changes that differ from each other.
    if not all(a.same(b) for a, b in itertools.combinations(alleles, 2)):
        return variants_pb2.CHOICE_AMBIGUOUS, None
    # Of candidates naming one allele, one that reaches the nucleotide identity says the most about it.
    return variants_pb2.CHOICE_CHOSEN, next(
        (i for i, allele in zip(level, alleles, strict=True) if allele.nucleotide is not None), level[0]
    )


def _choose(
    candidates: Sequence[variants_pb2.Candidate],
    verdicts: Sequence[variants_pb2.CandidateVerdict],
    declares_numbering: bool,
    references: variant_reference.References,
) -> tuple[variants_pb2.Choice, int | None]:
    everything = range(len(candidates))
    choice = _choose_among(everything, candidates, verdicts, declares_numbering, references)
    if choice[0] == variants_pb2.CHOICE_CHOSEN:
        return choice
    # A version update only settles a choice: one it leaves unsettled (a c. change moved where its protein change
    # could not be, say) is made as though there were none.
    written = [
        i
        for i in everything
        if not any(r.kind == variants_pb2.REWRITE_KIND_VERSION_UPDATE for r in candidates[i].rewrites)
    ]
    return _choose_among(written, candidates, verdicts, declares_numbering, references)


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
        The mention set completed with the candidates derived from the agent's (version updates, and changes moved
        to a transcript the fit singles out); one verdict per mention, in its order; and how they were made.

    Raises:
        MisplacedOccurrenceError: An occurrence the validation reads is on a line past the end of the text.
    """
    printed = variant_scan.printed_accessions(text)
    lines = text.split('\n')
    text = _normalise(text)
    mention_set = variant_fit.without_derived(mention_set)
    fit = variant_fit.fit(mention_set, references)
    mention_set = variant_fit.complete(variant_versions.update(mention_set, references), fit, references)
    out = variants_pb2.Verdicts()
    for mention, fitted in zip(mention_set.mentions, fit.fitted, strict=True):
        stated = _stated(mention_set, mention, text, printed)
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
        choice, chosen = _choose(mention.candidates, verdict.candidates, declares_numbering, references)
        verdict.choice = choice
        if chosen is not None:
            verdict.chosen = chosen
            named = identity(mention.candidates[chosen].hgvs, references)
            if named.nucleotide is not None:
                verdict.nucleotide_vrs_id = named.nucleotide
            if named.protein is not None:
                verdict.protein_vrs_id = named.protein
    return mention_set, out, construction(references)
