"""The variants contract refuses records whose parts do not line up."""

from __future__ import annotations

import protovalidate
import pytest

from themis.litcache.models import variants_pb2


def _verdict() -> variants_pb2.MentionVerdict:
    verdict = variants_pb2.MentionVerdict(choice=variants_pb2.CHOICE_CHOSEN, chosen=0)
    verdict.candidates.add(outcome=variants_pb2.OUTCOME_VALIDATED, anchor=variants_pb2.ANCHOR_MANE)
    return verdict


def test_a_chosen_index_inside_candidates_is_accepted() -> None:
    protovalidate.validate(_verdict())


def test_a_chosen_index_past_the_candidates_is_refused() -> None:
    verdict = _verdict()
    verdict.chosen = 1
    with pytest.raises(protovalidate.ValidationError, match='chosen must index into candidates'):
        protovalidate.validate(verdict)


def test_a_derived_candidate_without_a_verdict_is_refused() -> None:
    verdict = _verdict()
    verdict.derived.add(hgvs='NM_000001.2:c.2A>G')
    verdict.derived.add(hgvs='NM_000001.3:c.2A>G')
    with pytest.raises(protovalidate.ValidationError, match='every derived candidate has a verdict'):
        protovalidate.validate(verdict)


def test_an_unchosen_mention_carries_no_allele_identifier() -> None:
    verdict = variants_pb2.MentionVerdict(choice=variants_pb2.CHOICE_AMBIGUOUS, nucleotide_vrs_id='ga4gh:VA.x')
    verdict.candidates.add(outcome=variants_pb2.OUTCOME_VALIDATED, anchor=variants_pb2.ANCHOR_MANE)
    with pytest.raises(protovalidate.ValidationError, match='VRS ids are set only when'):
        protovalidate.validate(verdict)
    chosen = _verdict()
    chosen.nucleotide_vrs_id = 'ga4gh:VA.x'
    protovalidate.validate(chosen)


@pytest.mark.parametrize('kind', [variants_pb2.REWRITE_KIND_TRANSCRIPT_FIT, variants_pb2.REWRITE_KIND_VERSION_UPDATE])
def test_a_rewrite_the_validation_step_made_must_name_its_source(kind: variants_pb2.RewriteKind) -> None:
    with pytest.raises(protovalidate.ValidationError, match='names its source'):
        protovalidate.validate(variants_pb2.Rewrite(kind=kind))
    protovalidate.validate(variants_pb2.Rewrite(kind=kind, source='NM_000001.1:c.2A>G'))


def test_a_rewrite_the_agent_made_needs_no_source() -> None:
    protovalidate.validate(variants_pb2.Rewrite(kind=variants_pb2.REWRITE_KIND_NUMBERING))


@pytest.mark.parametrize(
    ('line', 'page', 'valid'), [(12, None, True), (None, 3, True), (None, None, False), (12, 3, False)]
)
def test_an_occurrence_is_cited_by_its_line_or_failing_that_its_page(
    line: int | None, page: int | None, valid: bool
) -> None:
    occurrence = variants_pb2.Occurrence(context=variants_pb2.CONTEXT_OBSERVED, line=line, page=page)
    if valid:
        protovalidate.validate(occurrence)
    else:
        with pytest.raises(protovalidate.ValidationError, match='exactly one'):
            protovalidate.validate(occurrence)


def test_a_box_needs_its_page() -> None:
    box = variants_pb2.Box(left=0.1, top=0.1, right=0.5, bottom=0.5)
    with pytest.raises(protovalidate.ValidationError, match='a box needs the page'):
        protovalidate.validate(variants_pb2.Occurrence(context=variants_pb2.CONTEXT_OBSERVED, line=12, box=box))
    protovalidate.validate(variants_pb2.Occurrence(context=variants_pb2.CONTEXT_OBSERVED, page=3, box=box))


def _mention(disposition: variants_pb2.Disposition, *, reason: str = '', candidates: int = 0) -> variants_pb2.Mention:
    mention = variants_pb2.Mention(
        verbatim='exon 3 deletion', disposition=disposition, reason=reason, known_species=variants_pb2.SPECIES_HUMAN
    )
    mention.occurrences.add(context=variants_pb2.CONTEXT_OBSERVED, line=2)
    for n in range(candidates):
        mention.candidates.add(hgvs=f'NM_000001.1:c.{n + 1}del')
    return mention


def test_a_mention_with_nothing_to_offer_says_why_and_carries_no_candidate() -> None:
    not_writable = variants_pb2.DISPOSITION_NOT_WRITABLE
    protovalidate.validate(_mention(not_writable, reason='no breakpoints stated'))
    for bad in (_mention(not_writable), _mention(not_writable, reason='no breakpoints stated', candidates=1)):
        with pytest.raises(protovalidate.ValidationError, match='gives a reason and carries no candidates'):
            protovalidate.validate(bad)
    protovalidate.validate(_mention(variants_pb2.DISPOSITION_HAS_CANDIDATES, candidates=1))


def test_a_mention_names_its_organism_from_the_list_or_as_printed() -> None:
    mention = _mention(variants_pb2.DISPOSITION_HAS_CANDIDATES, candidates=1)
    mention.printed_species = 'Xenopus laevis'
    protovalidate.validate(mention)
    for bad in ('', None):
        mention = _mention(variants_pb2.DISPOSITION_HAS_CANDIDATES, candidates=1)
        if bad is None:
            mention.ClearField('organism')
        else:
            mention.printed_species = bad
        with pytest.raises(protovalidate.ValidationError, match=r'species|organism'):
            protovalidate.validate(mention)
    mention = _mention(variants_pb2.DISPOSITION_HAS_CANDIDATES, candidates=1)
    mention.known_species = variants_pb2.SPECIES_UNSPECIFIED
    with pytest.raises(protovalidate.ValidationError, match='known_species'):
        protovalidate.validate(mention)


@pytest.mark.parametrize(
    ('outcome', 'anchor', 'valid'),
    [
        (variants_pb2.OUTCOME_VALIDATED, variants_pb2.ANCHOR_MANE, True),
        (variants_pb2.OUTCOME_UNPARSEABLE, variants_pb2.ANCHOR_UNSPECIFIED, True),
        (variants_pb2.OUTCOME_VALIDATED, variants_pb2.ANCHOR_UNSPECIFIED, False),
        (variants_pb2.OUTCOME_UNPARSEABLE, variants_pb2.ANCHOR_MANE, False),
    ],
)
def test_a_candidate_has_an_anchor_exactly_when_it_parsed(
    outcome: variants_pb2.Outcome, anchor: variants_pb2.Anchor, valid: bool
) -> None:
    verdict = variants_pb2.CandidateVerdict(outcome=outcome, anchor=anchor)
    if valid:
        protovalidate.validate(verdict)
    else:
        with pytest.raises(protovalidate.ValidationError, match='anchor must be unspecified iff'):
            protovalidate.validate(verdict)
