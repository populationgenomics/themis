"""Variant validation: fitting transcripts, updating versions, anchoring, and choosing each mention's allele."""

from __future__ import annotations

import collections.abc
import itertools

import pytest

from themis.litcache import variant_reference, variant_validate
from themis.litcache.models import variants_pb2
from themis.testing import litcache_variants


@pytest.fixture(scope='module')
def references(tmp_path_factory: pytest.TempPathFactory) -> variant_reference.References:
    return litcache_variants.build_references(tmp_path_factory.mktemp('variant-validate-refs'))


def _candidate(
    hgvs: str,
    *,
    flanking: str | None = None,
    renumbered: bool = False,
    declared: bool = False,
    repaired: bool = False,
) -> variants_pb2.Candidate:
    candidate = variants_pb2.Candidate(hgvs=hgvs)
    if flanking is not None:
        candidate.flanking = flanking
    if renumbered:
        candidate.rewrites.add(kind=variants_pb2.REWRITE_KIND_NUMBERING, declared=declared)
    if repaired:
        candidate.rewrites.add(kind=variants_pb2.REWRITE_KIND_NOTATION)
    return candidate


def _mention(
    *candidates: str | variants_pb2.Candidate,
    verbatim: str = 'c.10A>G',
    gene: str | None = 'GENE1',
    species: variants_pb2.Species = variants_pb2.SPECIES_HUMAN,
    line: int | None = 1,
) -> variants_pb2.Mention:
    mention = variants_pb2.Mention(verbatim=verbatim, known_species=species)
    if gene is not None:
        mention.gene = gene
    if line is not None:
        mention.occurrences.add(line=line, context=variants_pb2.CONTEXT_OBSERVED)
    for item in candidates:
        mention.candidates.append(_candidate(item) if isinstance(item, str) else item)
    return mention


def _validate(
    references: variant_reference.References,
    *mentions: variants_pb2.Mention,
    text: str = 'Results.\n',
    builds: collections.abc.Sequence[tuple[str, str]] = (),
    conventions: collections.abc.Sequence[tuple[str | None, str]] = (),
) -> tuple[variants_pb2.MentionSet, variants_pb2.Verdicts, variants_pb2.VariantValidationConstruction]:
    mention_set = variants_pb2.MentionSet(mentions=mentions)
    for build, sentence in builds:
        mention_set.builds.add(build=build, sentence=sentence)
    for gene, sentence in conventions:
        mention_set.conventions.add(gene=gene, sentence=sentence)
    return variant_validate.validate(mention_set, text=text, references=references)


# --- anchors and ranking --------------------------------------------------------


def test_among_stated_transcripts_that_hold_mane_is_taken(references: variant_reference.References) -> None:
    mention = _mention('NM_000102.1:c.10A>G', 'NM_000101.1:c.10A>G')
    _, verdicts, construction = _validate(references, mention, text='Numbered on NM_000102.1 and NM_000101.1.')

    verdict = verdicts.mentions[0]
    assert [c.anchor for c in verdict.candidates] == [
        variants_pb2.ANCHOR_STATED_ACCESSION,
        variants_pb2.ANCHOR_STATED_ACCESSION,
    ]
    assert (verdict.choice, verdict.chosen) == (variants_pb2.CHOICE_CHOSEN, 1)
    assert verdict.nucleotide_vrs_id.startswith('ga4gh:VA.')
    assert verdict.protein_vrs_id.startswith('ga4gh:VA.')
    assert construction == variant_validate.construction(references)


def test_a_declared_numbering_convention_breaks_a_tie_at_one_anchor(references: variant_reference.References) -> None:
    sentence = 'Residues are numbered from the mature peptide.'
    mention = _mention(
        _candidate('NM_000102.1:c.10A>G'),
        _candidate('NM_000103.1:c.13G>A', renumbered=True, declared=True),
        verbatim='4A>G',
    )
    _, verdicts, _ = _validate(
        references,
        mention,
        text=f'Methods. {sentence}',
        conventions=[('GENE1', sentence)],
    )

    verdict = verdicts.mentions[0]
    assert (verdict.choice, verdict.chosen) == (variants_pb2.CHOICE_CHOSEN, 1)


def test_a_printed_stretch_found_once_in_the_genes_mane_sequence_anchors_its_candidate(
    references: variant_reference.References,
) -> None:
    # Split across the occurrence's line and the next, with both RefSeq and Ensembl MANE holding the sequence.
    flank = litcache_variants.UNIQUE_FLANK
    half = len(flank) // 2
    text = f'intro\nflanked by {flank[:half]}\n{flank[half:]} c.10A>G\nend'
    mention = _mention(_candidate('NM_000103.1:c.10A>G', flanking=flank), line=2)

    _, verdicts, _ = _validate(references, mention, text=text)

    assert verdicts.mentions[0].candidates[0].anchor == variants_pb2.ANCHOR_FLANKING
    assert verdicts.mentions[0].choice == variants_pb2.CHOICE_CHOSEN


def test_a_stretch_printed_twice_overlapping_in_the_gene_does_not_anchor(
    references: variant_reference.References,
) -> None:
    text = f'in {litcache_variants.OVERLAPPING_FLANK}, c.10A>G'
    mention = _mention(_candidate('NM_000103.1:c.10A>G', flanking=litcache_variants.OVERLAPPING_FLANK), line=1)

    _, verdicts, _ = _validate(references, mention, text=text)

    assert verdicts.mentions[0].candidates[0].anchor == variants_pb2.ANCHOR_OTHER_TRANSCRIPT


def test_an_occurrence_past_the_end_of_the_text_raises_misplaced_occurrence_error(
    references: variant_reference.References,
) -> None:
    mention = _mention(_candidate('NM_000103.1:c.10A>G', flanking=litcache_variants.UNIQUE_FLANK), line=5)

    with pytest.raises(variant_validate.MisplacedOccurrenceError, match='past'):
        _validate(references, mention, text='line 1\nline 2')


@pytest.mark.parametrize(
    ('sentence', 'expected'),
    [
        ('Positions are on GRCh37.', variants_pb2.ANCHOR_GENOMIC_STATED_BUILD),
        ('—…', variants_pb2.ANCHOR_GENOMIC_UNSTATED_BUILD),
        ('.', variants_pb2.ANCHOR_GENOMIC_UNSTATED_BUILD),
    ],
)
def test_a_declaration_counts_only_where_the_text_bears_its_sentence_out(
    references: variant_reference.References, sentence: str, expected: variants_pb2.Anchor
) -> None:
    mention = _mention(f'{litcache_variants.CHROM_37}:g.1505A>G', verbatim='g.1505A>G', gene=None)
    _, verdicts, _ = _validate(
        references,
        mention,
        text='Methods. Positions are on GRCh37. Variants were called with GATK.',
        builds=[('GRCh37', sentence)],
    )

    verdict = verdicts.mentions[0]
    assert verdict.candidates[0].anchor == expected
    assert verdict.choice == variants_pb2.CHOICE_CHOSEN
    assert verdict.nucleotide_vrs_id == ''  # off GRCh38


# --- transcript fit (without_derived -> fit -> update -> complete) --------------


def test_the_transcript_holding_every_checkable_mention_is_singled_out_and_outranks_mane(
    references: variant_reference.References,
) -> None:
    # c.10A>G and c.20C>T hold on all isoforms; c.30G>A holds on NM_000102.1 alone; c.40T>C holds on none.
    mentions = [
        _mention('NM_000101.1:c.10A>G', verbatim='c.10A>G'),
        _mention('NM_000102.1:c.20C>T', verbatim='c.20C>T'),
        _mention('NM_000101.1:c.30G>A', verbatim='c.30G>A'),
        _mention('NM_000101.1:c.40T>C', verbatim='c.40T>C'),
    ]

    completed, verdicts, _ = _validate(references, *mentions)

    assert [[c.hgvs for c in m.candidates] for m in completed.mentions] == [
        ['NM_000101.1:c.10A>G', 'NM_000102.1:c.10A>G'],
        ['NM_000102.1:c.20C>T'],
        ['NM_000101.1:c.30G>A', 'NM_000102.1:c.30G>A'],
        ['NM_000101.1:c.40T>C', 'NM_000102.1:c.40T>C'],
    ]
    assert [r.kind for r in completed.mentions[0].candidates[1].rewrites] == [variants_pb2.REWRITE_KIND_TRANSCRIPT_FIT]
    # On c.10A>G, both MANE (index 0) and NM_000102.1 (index 1) validate; ANCHOR_GENE_FIT outranks ANCHOR_MANE.
    assert [c.anchor for c in verdicts.mentions[0].candidates] == [
        variants_pb2.ANCHOR_MANE,
        variants_pb2.ANCHOR_GENE_FIT,
    ]
    assert (verdicts.mentions[0].choice, verdicts.mentions[0].chosen) == (variants_pb2.CHOICE_CHOSEN, 1)
    assert (verdicts.mentions[2].choice, verdicts.mentions[2].chosen) == (variants_pb2.CHOICE_CHOSEN, 1)
    assert verdicts.mentions[3].choice == variants_pb2.CHOICE_NONE

    # Re-validating a completed set strips derived candidates first, so it stays idempotent.
    again_completed, again_verdicts, _ = variant_validate.validate(completed, text='Results.\n', references=references)
    assert again_completed == completed
    assert again_verdicts == verdicts


def test_a_superseded_transcript_version_holding_every_checkable_mention_is_singled_out(
    references: variant_reference.References,
) -> None:
    # Current NM_000200.3 fails c.22C>A and c.55A>G; superseded NM_000200.2 (reached via held_versions) holds all three.
    mentions = [
        _mention('NM_000200.3:c.5T>C', verbatim='c.5T>C', gene='VER1'),
        _mention('NM_000200.3:c.22C>A', verbatim='c.22C>A', gene='VER1'),
        _mention('NM_000200.3:c.55A>G', verbatim='c.55A>G', gene='VER1'),
    ]

    completed, verdicts, _ = _validate(references, *mentions)

    assert [m.candidates[-1].hgvs for m in completed.mentions] == [
        'NM_000200.2:c.5T>C',
        'NM_000200.2:c.22C>A',
        'NM_000200.2:c.55A>G',
    ]
    assert [v.candidates[-1].anchor for v in verdicts.mentions] == [variants_pb2.ANCHOR_GENE_FIT] * 3
    assert [(v.choice, v.chosen) for v in verdicts.mentions] == [(variants_pb2.CHOICE_CHOSEN, 1)] * 3


def test_an_unchecked_change_on_a_transcript_the_mentions_single_out_is_taken(
    references: variant_reference.References,
) -> None:
    mentions = [
        _mention('NM_000101.1:c.10A>G', verbatim='c.10A>G'),
        _mention('NM_000101.1:c.20C>T', verbatim='c.20C>T'),
        _mention('NM_000101.1:c.30G>A', verbatim='c.30G>A'),
        _mention('NM_000103.1:c.15del', verbatim='c.15del'),
    ]

    _, verdicts, _ = _validate(references, *mentions)

    unchecked = verdicts.mentions[3]
    assert [c.outcome for c in unchecked.candidates] == [
        variants_pb2.OUTCOME_NOTHING_TO_CHECK,
        variants_pb2.OUTCOME_NOTHING_TO_CHECK,
    ]
    assert (unchecked.choice, unchecked.chosen) == (variants_pb2.CHOICE_CHOSEN, 1)


def test_a_former_symbol_joins_its_genes_mentions(references: variant_reference.References) -> None:
    mentions = [
        _mention('NM_000101.1:c.10A>G', verbatim='c.10A>G', gene='GENE1'),
        _mention('NM_000101.1:c.20C>T', verbatim='c.20C>T', gene='GENE1'),
        _mention('NM_000101.1:c.30G>A', verbatim='c.30G>A', gene='OLDGENE1'),
    ]

    _, verdicts, _ = _validate(references, *mentions)

    assert [v.candidates[-1].anchor for v in verdicts.mentions] == [variants_pb2.ANCHOR_GENE_FIT] * 3


def test_version_updates_and_transcript_fit_compose_without_conflict(
    references: variant_reference.References,
) -> None:
    mentions = [
        _mention('NM_000101.1:c.10A>G', 'NP_000200.1:p.Phe2Ala', verbatim='c.10A>G'),
        _mention('NM_000101.1:c.20C>T', verbatim='c.20C>T'),
        _mention('NM_000101.1:c.30G>A', verbatim='c.30G>A'),
    ]

    completed, _, _ = _validate(references, *mentions)

    assert [c.hgvs for c in completed.mentions[0].candidates] == [
        'NM_000101.1:c.10A>G',
        'NP_000200.1:p.Phe2Ala',
        'NP_000200.3:p.Phe2Ala',
        'NM_000102.1:c.10A>G',
        'NP_000102.1:p.Phe2Ala',
    ]


def test_a_renumbered_candidate_is_not_tried_in_the_fit(references: variant_reference.References) -> None:
    completed, _, _ = _validate(
        references,
        _mention('NM_000101.1:c.10A>G', verbatim='c.10A>G'),
        _mention('NM_000101.1:c.20C>T', verbatim='c.20C>T'),
        _mention(_candidate('NM_000101.1:c.30G>A', renumbered=True), verbatim='c.30G>A'),
    )

    assert [[c.hgvs for c in m.candidates] for m in completed.mentions] == [
        ['NM_000101.1:c.10A>G'],
        ['NM_000101.1:c.20C>T'],
        ['NM_000101.1:c.30G>A'],
    ]


def test_a_renumbered_candidate_is_not_moved_onto_the_singled_out_transcript(
    references: variant_reference.References,
) -> None:
    completed, _, _ = _validate(
        references,
        _mention('NM_000101.1:c.10A>G', verbatim='c.10A>G'),
        _mention('NM_000101.1:c.20C>T', verbatim='c.20C>T'),
        _mention('NM_000101.1:c.30G>A', verbatim='c.30G>A'),
        _mention(_candidate('NM_000101.1:c.25A>G', renumbered=True), verbatim='1A>G'),
    )

    assert [c.hgvs for c in completed.mentions[3].candidates] == ['NM_000101.1:c.25A>G']


def test_mentions_without_a_known_human_gene_are_left_out_of_the_fit(
    references: variant_reference.References,
) -> None:
    unknown = [_mention(f'NM_000101.1:{c}', verbatim=c, gene='UNKNOWN') for c in ('c.10A>G', 'c.20C>T', 'c.30G>A')]
    mouse = [
        _mention(f'NM_000101.1:{c}', verbatim=c, species=variants_pb2.SPECIES_MOUSE)
        for c in ('c.10A>G', 'c.20C>T', 'c.30G>A')
    ]

    completed, verdicts, _ = _validate(references, *unknown, *mouse)

    assert all(len(m.candidates) == 1 for m in completed.mentions)
    assert all(v.candidates[0].anchor != variants_pb2.ANCHOR_GENE_FIT for v in verdicts.mentions)


def test_fewer_than_three_checkable_mentions_single_nothing_out(
    references: variant_reference.References,
) -> None:
    completed, _, _ = _validate(
        references,
        _mention('NM_000101.1:c.20C>T', verbatim='c.20C>T'),
        _mention('NM_000101.1:c.30G>A', verbatim='c.30G>A'),
    )

    assert all(len(m.candidates) == 1 for m in completed.mentions)


def test_mentions_every_isoform_holds_single_nothing_out(references: variant_reference.References) -> None:
    completed, verdicts, _ = _validate(
        references,
        _mention('NM_000101.1:c.4A>G', verbatim='c.4A>G'),
        _mention('NM_000101.1:c.10A>G', verbatim='c.10A>G'),
        _mention('NM_000101.1:c.20C>T', verbatim='c.20C>T'),
    )

    assert all(len(m.candidates) == 1 for m in completed.mentions)
    assert [v.candidates[0].anchor for v in verdicts.mentions] == [variants_pb2.ANCHOR_MANE] * 3


# --- version updates and choice -------------------------------------------------


def test_a_change_and_its_version_update_at_one_rank_choose_the_update(
    references: variant_reference.References,
) -> None:
    completed, verdicts, _ = _validate(references, _mention('NP_000200.1:p.Phe2Ala', verbatim='F2A', gene='VER1'))

    assert [c.hgvs for c in completed.mentions[0].candidates] == ['NP_000200.1:p.Phe2Ala', 'NP_000200.3:p.Phe2Ala']
    verdict = verdicts.mentions[0]
    assert (verdict.choice, verdict.chosen) == (variants_pb2.CHOICE_CHOSEN, 1)


def test_another_allele_at_the_rank_of_a_version_update_is_still_ambiguous(
    references: variant_reference.References,
) -> None:
    mention = _mention('NP_000200.1:p.Phe2Ala', 'NP_000201.1:p.Phe2Ala', verbatim='F2A', gene='VER1')
    _, verdicts, _ = _validate(references, mention)

    assert verdicts.mentions[0].choice == variants_pb2.CHOICE_AMBIGUOUS


def test_a_source_whose_update_ranks_lower_does_not_yield(references: variant_reference.References) -> None:
    mention = _mention('NP_000200.1:p.Phe2Ala', 'NP_000201.1:p.Phe2Ala', verbatim='F2A', gene='VER1')
    _, verdicts, _ = _validate(references, mention, text='Numbered on NP_000200.1 and NP_000201.1.')

    verdict = verdicts.mentions[0]
    assert [c.anchor for c in verdict.candidates] == [
        variants_pb2.ANCHOR_STATED_ACCESSION,
        variants_pb2.ANCHOR_STATED_ACCESSION,
        variants_pb2.ANCHOR_STATED_OTHER_VERSION,
    ]
    assert verdict.choice == variants_pb2.CHOICE_AMBIGUOUS


def test_only_the_source_of_an_update_yields_to_it(references: variant_reference.References) -> None:
    # .1 updates to .3; .2 differs within the window of Phe2 so it stays on .2 and competes with .3.
    mention = _mention('NP_000200.1:p.Phe2Ala', 'NP_000200.2:p.Phe2Ala', verbatim='F2A', gene='VER1')
    completed, verdicts, _ = _validate(references, mention)

    assert [c.hgvs for c in completed.mentions[0].candidates] == [
        'NP_000200.1:p.Phe2Ala',
        'NP_000200.2:p.Phe2Ala',
        'NP_000200.3:p.Phe2Ala',
    ]
    assert verdicts.mentions[0].choice == variants_pb2.CHOICE_AMBIGUOUS


def test_an_update_that_unsettles_a_choice_is_set_aside(references: variant_reference.References) -> None:
    # c.4G>A projects to the same genomic base on .1 and .2, so it updates to .2; p.Ala2Thr stays on .1 because
    # codon 7 differs, so the update disagrees with p.Ala2Thr and is set aside in favour of the written pair.
    mention = _mention('NM_000202.1:c.4G>A', 'NP_000202.1:p.Ala2Thr', verbatim='c.4G>A', gene='VER1')
    completed, verdicts, _ = _validate(references, mention)

    assert [c.hgvs for c in completed.mentions[0].candidates] == [
        'NM_000202.1:c.4G>A',
        'NP_000202.1:p.Ala2Thr',
        'NM_000202.2:c.4G>A',
    ]
    assert (verdicts.mentions[0].choice, verdicts.mentions[0].chosen) == (variants_pb2.CHOICE_CHOSEN, 0)


# --- no-allele descriptions, allele identity, and protein spellings -------------


@pytest.mark.parametrize(
    'hgvs',
    [
        'NM_000101.1:c.4A=',
        'NP_000101.1:p.(?)',
        'NP_000101.1:p.?',
        'NM_000101.1:r.?',
        'NP_000101.1:p.(=)',
        'NM_000101.1:r.spl?',
        'NM_000101.1:r.(spl?)',
    ],
)
def test_a_description_naming_no_allele_is_never_chosen(references: variant_reference.References, hgvs: str) -> None:
    _, verdicts, _ = _validate(references, _mention(hgvs, verbatim=hgvs))

    assert verdicts.mentions[0].choice == variants_pb2.CHOICE_NONE


def test_a_synonymous_change_at_a_position_names_an_allele(references: variant_reference.References) -> None:
    _, verdicts, _ = _validate(references, _mention('NP_000101.1:p.(Lys2=)', verbatim='p.(Lys2=)'))

    assert verdicts.mentions[0].choice == variants_pb2.CHOICE_CHOSEN
    assert verdicts.mentions[0].protein_vrs_id.startswith('ga4gh:VA.')


def test_two_nucleotide_changes_projecting_to_different_loci_are_ambiguous(
    references: variant_reference.References,
) -> None:
    # NM_000102.1 and NM_000103.1 share exon 1's coding sequence at different genomic loci; both c.4del predict
    # p.Lys2fs.
    mention = _mention('NM_000102.1:c.4del', 'NM_000103.1:c.4del', verbatim='c.4del')
    _, verdicts, _ = _validate(references, mention, text='Numbered on NM_000102.1 and NM_000103.1.')

    assert verdicts.mentions[0].choice == variants_pb2.CHOICE_AMBIGUOUS


def test_a_transcript_and_genomic_change_projecting_to_one_locus_choose_one_allele(
    references: variant_reference.References,
) -> None:
    mention = _mention(
        'NM_000103.1:c.4del',
        f'{litcache_variants.CHROM_38}:g.404del',
        verbatim=f'NM_000103.1:c.4del ({litcache_variants.CHROM_38}:g.404del)',
    )
    _, verdicts, _ = _validate(references, mention)

    assert verdicts.mentions[0].choice == variants_pb2.CHOICE_CHOSEN


def test_a_c_change_and_its_p_change_choose_the_nucleotide_candidate(
    references: variant_reference.References,
) -> None:
    mention = _mention('NP_000101.1:p.Lys2Arg', 'NM_000101.1:c.5A>G', verbatim='K2R')
    _, verdicts, _ = _validate(references, mention, text='NM_000101.1 (NP_000101.1).')

    verdict = verdicts.mentions[0]
    assert (verdict.choice, verdict.chosen) == (variants_pb2.CHOICE_CHOSEN, 1)
    assert verdict.nucleotide_vrs_id.startswith('ga4gh:VA.')
    assert verdict.protein_vrs_id.startswith('ga4gh:VA.')


def test_a_c_change_and_a_different_p_change_are_ambiguous(references: variant_reference.References) -> None:
    mention = _mention('NP_000101.1:p.Lys2Thr', 'NM_000101.1:c.5A>G', verbatim='K2T')
    _, verdicts, _ = _validate(references, mention, text='NM_000101.1 (NP_000101.1).')

    assert verdicts.mentions[0].choice == variants_pb2.CHOICE_AMBIGUOUS


@pytest.mark.parametrize(
    'order',
    list(itertools.permutations(('NP_000101.1:p.Lys2Asn', 'NM_000101.1:c.6A>T', 'NM_000101.1:c.6A>C'))),
)
def test_candidates_naming_two_alleles_are_ambiguous_in_any_order(
    references: variant_reference.References, order: tuple[str, str, str]
) -> None:
    # Both c.6A>T and c.6A>C predict p.Lys2Asn, while naming distinct nucleotide alleles.
    _, verdicts, _ = _validate(references, _mention(*order, verbatim='K2N'), text='NM_000101.1 (NP_000101.1).')

    assert verdicts.mentions[0].choice == variants_pb2.CHOICE_AMBIGUOUS


@pytest.mark.parametrize(
    'spellings',
    [
        ('NP_000101.1:p.K2R', 'ENSP00000000101.1:p.Lys2Arg', 'NP_000101.1:p.(Lys2Arg)'),
        ('NP_000101.1:p.Lys2*', 'ENSP00000000101.1:p.Lys2Ter', 'NP_000101.1:p.(Lys2*)'),
        (
            'NP_000101.1:p.Lys2fs',
            'ENSP00000000101.1:p.Lys2fsTer8',
            'NP_000101.1:p.K2fs*8',
            'ENSP00000000101.1:p.(Lys2fs*?)',
        ),
    ],
)
def test_spellings_of_one_protein_change_across_accessions_of_one_sequence_choose_one_allele(
    references: variant_reference.References, spellings: tuple[str, ...]
) -> None:
    _, verdicts, _ = _validate(references, _mention(*spellings, verbatim='p.Lys2'))

    assert verdicts.mentions[0].choice == variants_pb2.CHOICE_CHOSEN


@pytest.mark.parametrize(
    ('first', 'second'),
    [
        ('NP_000101.1:p.Lys2Arg', 'ENSP00000000101.1:p.Lys2Thr'),
        ('NP_000101.1:p.Lys2fs', 'ENSP00000000101.1:p.Lys2Argfs*8'),
    ],
)
def test_different_protein_changes_across_accessions_of_one_sequence_are_ambiguous(
    references: variant_reference.References, first: str, second: str
) -> None:
    _, verdicts, _ = _validate(references, _mention(first, second, verbatim='p.Lys2'))

    assert verdicts.mentions[0].choice == variants_pb2.CHOICE_AMBIGUOUS


@pytest.mark.parametrize(
    ('second', 'expected'),
    [
        ('NM_000101.1:c.(1_6)del', variants_pb2.CHOICE_CHOSEN),
        ('NM_000101.1:c.(4_9)del', variants_pb2.CHOICE_AMBIGUOUS),
    ],
)
def test_expressions_nothing_resolves_to_vrs_compare_as_written(
    references: variant_reference.References, second: str, expected: variants_pb2.Choice
) -> None:
    mention = _mention('NM_000101.1:c.(1_6)del', _candidate(second, repaired=True), verbatim='del')
    _, verdicts, _ = _validate(references, mention)

    assert verdicts.mentions[0].choice == expected
    assert verdicts.mentions[0].nucleotide_vrs_id == ''


# --- reversed printed substitutions ---------------------------------------------


@pytest.mark.parametrize(
    ('hgvs', 'verbatim'),
    [
        ('NP_000101.1:p.Lys2Arg', 'p.R2K'),
        ('NP_000101.1:p.(Lys2Arg)', 'GENE1 Arg2Lys'),
        ('NM_000101.1:c.5A>G', 'c.5G>A'),
    ],
)
def test_a_candidate_stating_the_printed_substitution_backwards_is_caught(
    references: variant_reference.References, hgvs: str, verbatim: str
) -> None:
    _, verdicts, _ = _validate(references, _mention(hgvs, verbatim=verbatim))

    verdict = verdicts.mentions[0]
    assert verdict.candidates[0].outcome == variants_pb2.OUTCOME_REVERSES_PRINTED
    assert verdict.choice == variants_pb2.CHOICE_NONE


@pytest.mark.parametrize(
    ('hgvs', 'verbatim'),
    [
        ('NP_000101.1:p.Lys2Arg', 'p.K2R'),  # the printed change
        ('NP_000101.1:p.Lys2Arg', 'p.R5K'),  # another position, as a numbering offset gives
        ('NP_000101.1:p.Ala5Val', 'C677T'),  # a base change printed in the shape of a residue change
        ('NM_000101.1:c.24+2T>C', 'c.24+2C>T'),  # an intronic offset is not a coding position
        ('NP_000101.1:p.Lys2Arg', 'p.R2K / p.K2R'),  # the paper prints both directions
        ('NP_000101.1:p.Lys2Lys', 'p.K2K'),  # names no residue change
    ],
)
def test_a_candidate_not_stating_the_printed_substitution_backwards_passes(
    references: variant_reference.References, hgvs: str, verbatim: str
) -> None:
    _, verdicts, _ = _validate(references, _mention(hgvs, verbatim=verbatim))

    assert verdicts.mentions[0].candidates[0].outcome == variants_pb2.OUTCOME_VALIDATED
