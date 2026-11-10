"""Which of a gene's transcripts a paper's mentions of the gene fit, in the paper's own numbering.

A variant named against a gene symbol does not say which transcript the paper numbered against. Each transcript is a
hypothesis the paper's mentions can test: a stated base or residue holds on it or not. One mention fitting one
transcript proves little (an exonic substitution passes on an arbitrary transcript a good share of the time), but
the chance that a wrong transcript fits falls with every further mention it holds, so every version the stores hold
is tried.

Where the mentions single transcripts out, a mention the agent wrote on none of them gains its change moved onto the
preferred one, so the choice can take the transcript the paper numbered against, not only those the agent named.

Only candidates without a numbering rewrite are tried: an offset or a legacy-numbering rewrite is the agent's
answer to the question this asks of the printed numbering.
"""

from __future__ import annotations

import collections
import dataclasses
from collections.abc import Callable, Iterable, Sequence

import weaver

from themis.litcache import variant_reference
from themis.litcache.models import variants_pb2

# The rewrites that renumber a printed change; a candidate made with one is not the paper's own numbering.
NUMBERING_REWRITES = frozenset({variants_pb2.REWRITE_KIND_NUMBERING})
# The rewrites only the validation step makes.
DERIVED_REWRITES = frozenset({variants_pb2.REWRITE_KIND_TRANSCRIPT_FIT, variants_pb2.REWRITE_KIND_VERSION_UPDATE})
# Mentions that must hold on every transcript singled out, counting only those holding on some transcript of the gene:
# one holding on none (a misprint, say) says nothing of which the paper numbered against.
_MIN_CHECKABLE = 3


@dataclasses.dataclass(frozen=True)
class _Sequence:
    transcript: str
    protein: str | None


@dataclasses.dataclass(frozen=True)
class Fit:
    """Which transcripts a mention set's genes fit.

    Attributes:
        fitted: Parallel to the mention set's mentions, the transcripts its gene's mentions single out, and their
            proteins.
        preferred: Parallel to the mention set's mentions, the transcript singled out that is preferred (MANE, then
            RefSeq, then the newest version), or None where none is.
    """

    fitted: list[frozenset[str]]
    preferred: list[str | None]


def _accession(candidate: variants_pb2.Candidate) -> str:
    return candidate.hgvs.partition(':')[0]


def _sources(candidates: Iterable[variants_pb2.Candidate]) -> list[tuple[variants_pb2.Candidate, str, str]]:
    """The agent's c. and p. changes in the paper's numbering: each as candidate, coordinate type, part after ':'."""
    found = []
    for candidate in candidates:
        if any(r.kind in NUMBERING_REWRITES or r.kind in DERIVED_REWRITES for r in candidate.rewrites):
            continue
        try:
            kind = weaver.parse(candidate.hgvs).coordinate_type
        except weaver.HGVSError:
            continue
        if kind in ('c', 'p'):
            found.append((candidate, kind, candidate.hgvs.partition(':')[2]))
    return found


def _changes(mention: variants_pb2.Mention) -> list[tuple[str, str]]:
    """The mention's c. and p. changes in the paper's numbering, each as its coordinate type and the part after ':'."""
    return list(dict.fromkeys((kind, change) for _, kind, change in _sources(mention.candidates)))


def _sequences(gene: str, references: variant_reference.References) -> list[_Sequence]:
    """Every version the stores hold of every current transcript of the gene, with the protein each encodes."""
    found = []
    for t in references.transcripts(gene):
        base = t.accession.partition('.')[0]
        for transcript in references.held_versions(base) or [t.accession]:
            found.append(_Sequence(transcript, references.protein_of(transcript)))
    return list(dict.fromkeys(found))


def _outcome(
    changes: Iterable[tuple[str, str]], sequence: _Sequence, check: Callable[[str], variants_pb2.Outcome.ValueType]
) -> str:
    """'holds' if a change validates on the sequence, 'fails' if one is contradicted and none holds, else 'silent'."""
    contradicted = False
    for kind, change in changes:
        on = sequence.transcript if kind == 'c' else sequence.protein
        if on is None:
            continue
        outcome = check(f'{on}:{change}')
        if outcome == variants_pb2.OUTCOME_VALIDATED:
            return 'holds'
        contradicted |= outcome == variants_pb2.OUTCOME_REFERENCE_DISAGREES
    return 'fails' if contradicted else 'silent'


def _single_out(
    sequences: Sequence[_Sequence], outcomes: dict[tuple[int, str], str], indices: Sequence[int]
) -> list[_Sequence]:
    """The sequences holding every checkable mention, of at least `_MIN_CHECKABLE`, while another fails one."""
    checkable = [i for i in indices if any(outcomes[i, s.transcript] == 'holds' for s in sequences)]
    if len(checkable) < _MIN_CHECKABLE:
        return []
    fitted = [s for s in sequences if all(outcomes[i, s.transcript] == 'holds' for i in checkable)]
    others = [s for s in sequences if s not in fitted]
    if not fitted or not any(outcomes[i, s.transcript] == 'fails' for s in others for i in checkable):
        return []
    return fitted


def _preference(sequence: _Sequence, references: variant_reference.References) -> tuple[bool, bool, str, int]:
    """MANE first, then RefSeq, then the newest version."""
    base, _, version = sequence.transcript.partition('.')
    return (not references.is_mane(sequence.transcript), base.startswith('ENS'), base, -int(version or 0))


def fit(mention_set: variants_pb2.MentionSet, references: variant_reference.References) -> Fit:
    """Fit each gene's transcripts to the paper's human mentions of the gene.

    Candidates the validation step derived are not tried: they repeat a change the agent wrote.

    Args:
        mention_set: What the extraction step recorded.
        references: The reference stores.

    Returns:
        Per mention, the transcripts its gene's mentions single out, and the preferred one.
    """
    checked: dict[str, variants_pb2.Outcome.ValueType] = {}

    def check(expression: str) -> variants_pb2.Outcome.ValueType:
        if expression not in checked:
            checked[expression] = references.check(expression).outcome
        return checked[expression]

    by_gene: dict[str, list[int]] = collections.defaultdict(list)
    for index, mention in enumerate(mention_set.mentions):
        if not mention.HasField('gene') or mention.known_species != variants_pb2.SPECIES_HUMAN:
            continue
        gene = references.current_symbol(mention.gene)
        if gene is not None:
            by_gene[gene].append(index)
    fitted: list[frozenset[str]] = [frozenset() for _ in mention_set.mentions]
    preferred: list[str | None] = [None for _ in mention_set.mentions]
    for gene, indices in by_gene.items():
        changes = {i: _changes(mention_set.mentions[i]) for i in indices}
        sequences = _sequences(gene, references)
        outcomes = {(i, s.transcript): _outcome(changes[i], s, check) for s in sequences for i in indices}
        singled = sorted(_single_out(sequences, outcomes, indices), key=lambda s: _preference(s, references))
        accessions = frozenset(a for s in singled for a in (s.transcript, s.protein) if a is not None)
        for i in indices:
            fitted[i] = accessions
            preferred[i] = singled[0].transcript if singled else None
    return Fit(fitted, preferred)


def complete(
    mention_set: variants_pb2.MentionSet, result: Fit, references: variant_reference.References
) -> variants_pb2.MentionSet:
    """The mention set with each mention's changes moved onto the transcript its gene's mentions single out.

    A mention gains candidates only where the agent wrote none on a transcript singled out or its protein: one per
    change in the paper's own numbering, on the preferred transcript (a c. change) or its protein (a p. change), each
    marked with a transcript-fit rewrite. A candidate derived otherwise (a version update) that is on one counts
    as one the agent wrote there.

    Args:
        mention_set: The agent's candidates, perhaps with others derived from them, but none moved by a fit.
        result: The fit of the agent's candidates.
        references: The reference stores.

    Returns:
        A completed copy; the candidates it had keep their order and come first.
    """
    out = variants_pb2.MentionSet()
    out.CopyFrom(mention_set)
    for mention, fitted, transcript in zip(out.mentions, result.fitted, result.preferred, strict=True):
        if transcript is None or any(_accession(c) in fitted for c in mention.candidates):
            continue
        protein = references.protein_of(transcript)
        for source, kind, change in _sources(list(mention.candidates)):
            target = transcript if kind == 'c' else protein
            if target is None or any(c.hgvs == f'{target}:{change}' for c in mention.candidates):
                continue
            moved = mention.candidates.add(hgvs=f'{target}:{change}', rewrites=source.rewrites)
            moved.rewrites.add(
                kind=variants_pb2.REWRITE_KIND_TRANSCRIPT_FIT,
                note=f"moved from {_accession(source)} to {target}, which the paper's mentions of {mention.gene} fit",
                source=source.hgvs,
            )
    return out


def without_derived(mention_set: variants_pb2.MentionSet) -> variants_pb2.MentionSet:
    """The mention set with the candidates an earlier validation derived dropped: the agent's alone."""
    out = variants_pb2.MentionSet()
    out.CopyFrom(mention_set)
    for mention in out.mentions:
        kept = [c for c in mention.candidates if not any(r.kind in DERIVED_REWRITES for r in c.rewrites)]
        del mention.candidates[:]
        mention.candidates.extend(kept)
    return out
