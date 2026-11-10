"""The transcript fit: which of a gene's transcripts a paper's mentions hold on, in its own numbering."""

from __future__ import annotations

import dataclasses
from typing import cast

from themis.litcache import variant_fit, variant_reference
from themis.litcache.models import variants_pb2


@dataclasses.dataclass(frozen=True)
class _Transcript:
    accession: str


class _Stores:
    """Two versions of one gene's transcript, an older numbering and the current one, and which expressions hold."""

    def __init__(self, validated: set[str], disagree: set[str]) -> None:
        self._validated, self._disagree = validated, disagree

    def current_symbol(self, symbol: str) -> str | None:
        return 'ABC1' if symbol in ('ABC1', 'OLD1') else None

    def transcripts(self, gene: str) -> list[_Transcript]:
        assert gene == 'ABC1'
        return [_Transcript('NM_1.2')]

    def held_versions(self, base: str) -> list[str]:
        return ['NM_1.1', 'NM_1.2'] if base == 'NM_1' else []

    def protein_of(self, transcript: str) -> str:
        return {'NM_1.1': 'NP_1.1', 'NM_1.2': 'NP_1.2'}[transcript]

    def is_mane(self, accession: str) -> bool:
        return accession == 'NM_1.2'

    def check(self, expression: str) -> variant_reference.Check:
        if expression in self._validated:
            return variant_reference.Check(variants_pb2.OUTCOME_VALIDATED)
        if expression in self._disagree:
            return variant_reference.Check(variants_pb2.OUTCOME_REFERENCE_DISAGREES, 'disagrees')
        return variant_reference.Check(variants_pb2.OUTCOME_NOTHING_TO_CHECK, 'nothing to check')


def _mention(
    hgvs: str,
    *,
    gene: str = 'ABC1',
    species: variants_pb2.Species = variants_pb2.SPECIES_HUMAN,
    renumbered: bool = False,
) -> variants_pb2.Mention:
    mention = variants_pb2.Mention(verbatim=hgvs, gene=gene, known_species=species)
    candidate = mention.candidates.add(hgvs=hgvs)
    if renumbered:
        candidate.rewrites.add(kind=variants_pb2.REWRITE_KIND_NUMBERING)
    return mention


def _fit(stores: _Stores, *mentions: variants_pb2.Mention) -> variant_fit.Fit:
    return variant_fit.fit(variants_pb2.MentionSet(mentions=mentions), cast(variant_reference.References, stores))


def _completed(stores: _Stores, mention_set: variants_pb2.MentionSet) -> variants_pb2.MentionSet:
    references = cast(variant_reference.References, stores)
    mention_set = variant_fit.without_derived(mention_set)
    return variant_fit.complete(mention_set, variant_fit.fit(mention_set, references), references)


# Three changes the older version holds and the current one fails once, and a misprint that holds on neither.
_ON_THE_OLDER_VERSION = _Stores(
    validated={'NM_1.1:c.10A>G', 'NM_1.1:c.20C>T', 'NM_1.1:c.30G>A', 'NM_1.2:c.10A>G', 'NM_1.2:c.20C>T'},
    disagree={'NM_1.2:c.30G>A', 'NM_1.1:c.40T>C', 'NM_1.2:c.40T>C'},
)
_FOUR = ('c.10A>G', 'c.20C>T', 'c.30G>A', 'c.40T>C')


def test_the_transcript_holding_every_checkable_mention_is_singled_out() -> None:
    result = _fit(_ON_THE_OLDER_VERSION, *(_mention(f'NM_1.2:{c}') for c in _FOUR))
    assert result.fitted == [frozenset({'NM_1.1', 'NP_1.1'})] * 4
    assert result.preferred == ['NM_1.1'] * 4


def test_a_former_symbol_joins_its_genes_mentions() -> None:
    mentions = [_mention(f'NM_1.2:{c}') for c in _FOUR[:3]] + [_mention(f'NM_1.2:{_FOUR[3]}', gene='OLD1')]
    assert _fit(_ON_THE_OLDER_VERSION, *mentions).preferred == ['NM_1.1'] * 4


def test_a_renumbered_candidate_is_not_tried() -> None:
    result = _fit(_ON_THE_OLDER_VERSION, *(_mention(f'NM_1.2:{c}', renumbered=True) for c in _FOUR))
    assert result.preferred == [None] * 4


def test_mentions_without_a_known_human_gene_are_left_out() -> None:
    stores = _Stores(validated={'NM_1.2:c.10A>G'}, disagree=set())
    result = _fit(
        stores,
        _mention('NM_1.2:c.10A>G', gene='UNKNOWN'),
        _mention('NM_1.2:c.10A>G', species=variants_pb2.SPECIES_MOUSE),
    )
    assert result.fitted == [frozenset(), frozenset()]
    assert result.preferred == [None, None]


def test_fewer_than_three_checkable_mentions_single_nothing_out() -> None:
    result = _fit(_ON_THE_OLDER_VERSION, _mention('NM_1.2:c.20C>T'), _mention('NM_1.2:c.30G>A'))
    assert result.preferred == [None, None]


def test_mentions_every_transcript_holds_single_nothing_out() -> None:
    stores = _Stores(validated={f'NM_1.{v}:c.{n}0A>G' for v in (1, 2) for n in (1, 2, 3)}, disagree=set())
    result = _fit(stores, *(_mention(f'NM_1.2:c.{n}0A>G') for n in (1, 2, 3)))
    assert result.preferred == [None] * 3


def test_a_mention_written_off_the_singled_out_transcript_gains_its_change_there() -> None:
    mentions = variants_pb2.MentionSet(
        mentions=[_mention('NM_1.2:c.10A>G'), _mention('NM_1.1:c.20C>T'), _mention('NM_1.2:c.30G>A')]
    )
    completed = _completed(_ON_THE_OLDER_VERSION, mentions)
    assert [[c.hgvs for c in m.candidates] for m in completed.mentions] == [
        ['NM_1.2:c.10A>G', 'NM_1.1:c.10A>G'],
        ['NM_1.1:c.20C>T'],
        ['NM_1.2:c.30G>A', 'NM_1.1:c.30G>A'],
    ]
    derived = completed.mentions[0].candidates[1]
    assert [r.kind for r in derived.rewrites] == [variants_pb2.REWRITE_KIND_TRANSCRIPT_FIT]
    assert _completed(_ON_THE_OLDER_VERSION, completed) == completed


def test_a_renumbered_candidate_is_not_moved() -> None:
    mentions = variants_pb2.MentionSet(
        mentions=[_mention('NM_1.2:c.10A>G'), _mention('NM_1.2:c.20C>T'), _mention('NM_1.2:c.30G>A')]
    )
    mentions.mentions.append(_mention('NM_1.2:c.99A>G', renumbered=True))
    completed = _completed(_ON_THE_OLDER_VERSION, mentions)
    assert [c.hgvs for c in completed.mentions[3].candidates] == ['NM_1.2:c.99A>G']
