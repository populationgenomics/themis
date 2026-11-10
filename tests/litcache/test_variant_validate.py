"""The choice of a mention's allele among its candidates' verdicts."""

from __future__ import annotations

import itertools
from typing import cast

import pytest

from themis.litcache import variant_reference, variant_validate
from themis.litcache.models import variants_pb2


class _Stores:
    def is_mane(self, accession: str) -> bool:
        return accession == 'NP_2.1'

    def mapper(self, accession: str) -> None:
        raise KeyError(accession)


def _choose(
    *candidates: tuple[str, variants_pb2.Outcome, variants_pb2.Anchor],
) -> tuple[variants_pb2.Choice, int | None]:
    return variant_validate._choose(
        [variants_pb2.Candidate(hgvs=h) for h, _, _ in candidates],
        [variants_pb2.CandidateVerdict(outcome=o, anchor=a) for _, o, a in candidates],
        declares_numbering=False,
        references=cast(variant_reference.References, _Stores()),
    )


def test_among_stated_transcripts_that_hold_mane_is_taken() -> None:
    choice, chosen = _choose(
        ('NP_1.1:p.Ser740Phe', variants_pb2.OUTCOME_VALIDATED, variants_pb2.ANCHOR_STATED_ACCESSION),
        ('NP_2.1:p.Ser740Phe', variants_pb2.OUTCOME_VALIDATED, variants_pb2.ANCHOR_STATED_ACCESSION),
    )
    assert (choice, chosen) == (variants_pb2.CHOICE_CHOSEN, 1)


def test_a_transcript_the_mentions_single_out_outranks_mane() -> None:
    choice, chosen = _choose(
        ('NP_2.1:p.Ser740Phe', variants_pb2.OUTCOME_VALIDATED, variants_pb2.ANCHOR_MANE),
        ('NP_1.1:p.Ser740Phe', variants_pb2.OUTCOME_VALIDATED, variants_pb2.ANCHOR_GENE_FIT),
    )
    assert (choice, chosen) == (variants_pb2.CHOICE_CHOSEN, 1)


def test_an_unchecked_change_on_a_transcript_the_mentions_single_out_is_taken() -> None:
    choice, chosen = _choose(
        ('NP_3.1:p.Ser740Phe', variants_pb2.OUTCOME_NOTHING_TO_CHECK, variants_pb2.ANCHOR_OTHER_TRANSCRIPT),
        ('NP_1.1:p.Ser740Phe', variants_pb2.OUTCOME_NOTHING_TO_CHECK, variants_pb2.ANCHOR_GENE_FIT),
    )
    assert (choice, chosen) == (variants_pb2.CHOICE_CHOSEN, 1)


def _choose_among(
    *candidates: tuple[variants_pb2.Candidate, variants_pb2.Outcome, variants_pb2.Anchor],
) -> tuple[variants_pb2.Choice, int | None]:
    return variant_validate._choose(
        [c for c, _, _ in candidates],
        [variants_pb2.CandidateVerdict(outcome=o, anchor=a) for _, o, a in candidates],
        declares_numbering=False,
        references=cast(variant_reference.References, _Stores()),
    )


def _written(hgvs: str) -> variants_pb2.Candidate:
    return variants_pb2.Candidate(hgvs=hgvs)


def _update(source: str, hgvs: str) -> variants_pb2.Candidate:
    candidate = variants_pb2.Candidate(hgvs=hgvs)
    candidate.rewrites.add(kind=variants_pb2.REWRITE_KIND_VERSION_UPDATE, source=source)
    return candidate


_HOLDS = variants_pb2.OUTCOME_VALIDATED
_OTHER = variants_pb2.ANCHOR_OTHER_TRANSCRIPT
_STATED = variants_pb2.ANCHOR_STATED_ACCESSION


def test_a_change_and_its_version_update_at_one_rank_choose_the_update() -> None:
    choice = _choose_among(
        (_written('NP_3.1:p.Phe200Ala'), _HOLDS, _OTHER),
        (_update('NP_3.1:p.Phe200Ala', 'NP_3.2:p.Phe200Ala'), _HOLDS, _OTHER),
    )
    assert choice == (variants_pb2.CHOICE_CHOSEN, 1)


def test_another_allele_at_the_rank_of_a_version_update_is_still_ambiguous() -> None:
    choice = _choose_among(
        (_written('NP_3.1:p.Phe200Ala'), _HOLDS, _OTHER),
        (_update('NP_3.1:p.Phe200Ala', 'NP_3.2:p.Phe200Ala'), _HOLDS, _OTHER),
        (_written('NP_4.1:p.Phe200Ala'), _HOLDS, _OTHER),
    )
    assert choice == (variants_pb2.CHOICE_AMBIGUOUS, None)


def test_a_source_whose_update_ranks_lower_does_not_yield() -> None:
    choice = _choose_among(
        (_written('NP_3.1:p.Phe200Ala'), _HOLDS, _STATED),
        (_update('NP_3.1:p.Phe200Ala', 'NP_3.2:p.Phe200Ala'), _HOLDS, variants_pb2.ANCHOR_STATED_OTHER_VERSION),
        (_written('NP_4.1:p.Phe200Ala'), _HOLDS, _STATED),
    )
    assert choice == (variants_pb2.CHOICE_AMBIGUOUS, None)


def test_an_update_that_unsettles_a_choice_is_set_aside(monkeypatch: pytest.MonkeyPatch) -> None:
    # The c. change and its protein change name one protein allele; moved to .3, the c. change names another protein's.
    proteins = {
        'NM_5.2:c.1444G>T': 'ga4gh:VA.p1',
        'NP_5.1:p.Ala482Ser': 'ga4gh:VA.p1',
        'NM_5.3:c.1444G>T': 'ga4gh:VA.p2',
    }
    monkeypatch.setattr(
        variant_validate,
        'identity',
        lambda expression, _references: variant_validate.Identity(expression, protein=proteins[expression]),
    )
    choice = _choose_among(
        (_written('NM_5.2:c.1444G>T'), _HOLDS, _OTHER),
        (_written('NP_5.1:p.Ala482Ser'), _HOLDS, _OTHER),
        (_update('NM_5.2:c.1444G>T', 'NM_5.3:c.1444G>T'), _HOLDS, _OTHER),
    )
    assert choice == (variants_pb2.CHOICE_CHOSEN, 0)


def test_only_the_source_of_an_update_yields_to_it() -> None:
    choice = _choose_among(
        (_written('NP_3.1:p.Phe200Ala'), _HOLDS, _OTHER),
        (_written('NP_3.3:p.Phe200Ala'), _HOLDS, _OTHER),
        (_update('NP_3.1:p.Phe200Ala', 'NP_3.4:p.Phe200Ala'), _HOLDS, _OTHER),
    )
    assert choice == (variants_pb2.CHOICE_AMBIGUOUS, None)


@pytest.mark.parametrize('change', ['c.842C=', 'p.(?)', 'p.?', 'r.?', 'p.(=)', 'r.spl?', 'r.(spl?)'])
def test_a_description_naming_no_allele_is_never_chosen(change: str) -> None:
    choice, chosen = _choose((f'NP_2.1:{change}', variants_pb2.OUTCOME_VALIDATED, variants_pb2.ANCHOR_MANE))
    assert (choice, chosen) == (variants_pb2.CHOICE_NONE, None)


def test_a_synonymous_change_at_a_position_names_an_allele() -> None:
    choice, _ = _choose(('NP_2.1:p.(Arg41=)', variants_pb2.OUTCOME_VALIDATED, variants_pb2.ANCHOR_MANE))
    assert choice == variants_pb2.CHOICE_CHOSEN


def test_an_occurrence_on_a_line_is_printed_in_that_line_and_the_next() -> None:
    lines = ['intro', 'flanked by GATTACA', 'CAT', 'end']
    occurrence = variants_pb2.Occurrence(line=2)
    assert variant_validate._printed_at(occurrence, lines) == 'flanked by GATTACA CAT'
    with pytest.raises(ValueError, match='past'):
        variant_validate._printed_at(variants_pb2.Occurrence(line=5), lines)


def _named(
    expression: str, allele: str | None = None, protein: str | None = None, key: str | None = None
) -> variant_validate.Identity:
    # A change on GRCh38's primary assembly, whose recorded identifier is the one it is compared by.
    return variant_validate.Identity(expression, allele=allele, nucleotide=allele, protein=protein, protein_key=key)


def test_two_nucleotide_changes_are_one_allele_only_where_their_projections_are() -> None:
    # Two transcripts' c.4334del, projected to different bases, though both predict a frameshift on one protein.
    a = _named('NM_1.5:c.4334del', 'ga4gh:VA.a', key='SQ.p:Asn1445fs')
    b = _named('NM_2.5:c.4334del', 'ga4gh:VA.b', key='SQ.p:Asn1445fs')
    assert not a.same(b)
    assert a.same(_named('NC_1.11:g.100del', 'ga4gh:VA.a'))


def test_a_c_change_and_a_p_change_are_compared_as_proteins() -> None:
    c = _named('NM_1.4:c.1346G>A', 'ga4gh:VA.c', 'ga4gh:VA.p')
    assert c.same(_named('NP_1.3:p.Arg449His', protein='ga4gh:VA.p'))
    assert not c.same(_named('NP_1.3:p.Arg449Cys', protein='ga4gh:VA.q'))


def test_proteins_without_a_vrs_allele_compare_by_sequence_and_spelling() -> None:
    assert _named('NP_1.2:p.S765fs*23', key='SQ.x:Ser765fs').same(_named('NP_2.2:p.Ser765fsTer23', key='SQ.x:Ser765fs'))


def test_expressions_nothing_resolves_compare_as_written() -> None:
    assert _named('NM_9.1:c.1A>G').same(_named('NM_9.1:c.1A>G'))
    assert not _named('NM_9.1:c.1A>G').same(_named('NM_9.1:c.2A>G'))


@pytest.mark.parametrize(
    ('hgvs', 'verbatim'),
    [
        ('NP_1.1:p.Thr153Ile', 'p.I153T'),
        ('NP_1.1:p.(Thr153Ile)', 'CTSC Ile153Thr'),
        ('NM_1.1:c.815C>G', 'c.815G>C'),
    ],
)
def test_a_candidate_stating_the_printed_substitution_backwards_is_caught(hgvs: str, verbatim: str) -> None:
    assert variant_validate._reverses_printed(hgvs, verbatim) is not None


@pytest.mark.parametrize(
    ('hgvs', 'verbatim'),
    [
        ('NP_1.4:p.Ile153Thr', 'p.I153T'),  # the printed change
        ('NP_1.1:p.Thr150Ile', 'p.I153T'),  # another position, as a numbering offset gives
        ('NP_1.1:p.Ala222Val', 'C677T'),  # a base change printed in the shape of a residue change
        ('NM_1.1:c.815+3C>G', 'c.815+3G>C'),  # an intronic offset is not a coding position
        ('NP_1.1:p.Thr153Ile', 'p.I153T / p.T153I'),  # the paper prints both directions
        ('NP_1.1:p.Thr153Thr', 'p.T153T'),  # names no residue change
    ],
)
def test_a_candidate_not_stating_the_printed_substitution_backwards_passes(hgvs: str, verbatim: str) -> None:
    assert variant_validate._reverses_printed(hgvs, verbatim) is None


_ALLELES = {
    'NP_1.1:p.Pro72Arg': variant_validate.Identity('p', protein='VA.protein'),
    'NM_1.1:c.215C>G': variant_validate.Identity('c1', allele='VA.one', nucleotide='VA.one', protein='VA.protein'),
    'NM_1.1:c.[215C>G;216C>G]': variant_validate.Identity(
        'c2', allele='VA.two', nucleotide='VA.two', protein='VA.protein'
    ),
}


def _identity(expression: str, references: variant_reference.References) -> variant_validate.Identity:
    del references
    return _ALLELES[expression]


@pytest.mark.parametrize('order', itertools.permutations(_ALLELES))
def test_candidates_naming_two_alleles_are_ambiguous_in_any_order(
    order: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The protein change agrees with both c. changes, which differ from each other.
    monkeypatch.setattr(variant_validate, 'identity', _identity)
    choice, _ = _choose(*((h, variants_pb2.OUTCOME_VALIDATED, variants_pb2.ANCHOR_STATED_ACCESSION) for h in order))
    assert choice == variants_pb2.CHOICE_AMBIGUOUS


def test_of_candidates_naming_one_allele_the_one_reaching_its_nucleotide_identity_is_chosen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(variant_validate, 'identity', _identity)
    choice, chosen = _choose(
        ('NP_1.1:p.Pro72Arg', variants_pb2.OUTCOME_VALIDATED, variants_pb2.ANCHOR_STATED_ACCESSION),
        ('NM_1.1:c.215C>G', variants_pb2.OUTCOME_VALIDATED, variants_pb2.ANCHOR_STATED_ACCESSION),
    )
    assert (choice, chosen) == (variants_pb2.CHOICE_CHOSEN, 1)


class _ManeTwice:
    """A gene whose RefSeq and Ensembl MANE Select are one sequence, as they are."""

    def transcripts(self, gene: str) -> list[variant_reference.Transcript]:
        del gene
        return [
            variant_reference.Transcript('NM_1.1', 'NP_1.1', mane_select=True, store='refseq-grch38'),
            variant_reference.Transcript('ENST1.1', 'ENSP1.1', mane_select=True, store='ensembl-grch38'),
        ]

    def sequence(self, accession: str, start: int, end: int | None = None) -> str:
        del accession, start, end
        return 'ATGGCCAAGTTTGGATCCCGTACGTTAG'


def test_a_printed_stretch_found_once_in_the_gene_s_mane_sequence_anchors_its_candidate() -> None:
    mention = variants_pb2.Mention(verbatim='c.10G>A', gene='GENE1')
    mention.occurrences.add(line=1)
    references = cast(variant_reference.References, _ManeTwice())
    assert variant_validate._flanking_holds('AAGTTTGGATCC', mention, ['in AAGTTTGGATCC, c.10G>A'], references)


def test_a_stretch_printed_twice_overlapping_in_the_gene_does_not_anchor() -> None:
    assert variant_validate._occurrences('GGATATATGG', 'ATAT') == 2


@pytest.mark.parametrize(('sentence', 'stated'), [('Positions are on GRCh37.', True), ('—…', False), ('.', False)])
def test_a_declaration_counts_only_where_the_text_bears_its_sentence_out(sentence: str, stated: bool) -> None:
    text = variant_validate._normalise('Methods. Positions are on GRCh37. Variants were called with GATK.')
    mention_set = variants_pb2.MentionSet()
    mention_set.builds.add(build='GRCh37', sentence=sentence)
    mention = mention_set.mentions.add(verbatim='c.10A>G')

    result = variant_validate._stated(mention_set, mention, text, [])

    assert bool(result.builds) is stated
