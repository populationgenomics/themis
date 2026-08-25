"""Tests for placing a ClinVar pool against a coding span."""

from __future__ import annotations

import pytest

from themis.rpc import clinvar_pb2
from themis.svcv4 import placement, scoring
from themis.svcv4.tests import responses

_TRANSCRIPT = 'NM_000123.4'


def _record(
    clinvar_id: str,
    start: int,
    end: int,
    *,
    transcript: str = _TRANSCRIPT,
    edit: str = 'del',
    start_offset: int = 0,
    end_offset: int = 0,
) -> clinvar_pb2.ClinVarRecord:
    """One pooled record, with the expression its span was parsed from.

    The edit matters to placement for one form alone — an insertion's endpoints bracket it — so
    everything else here is written as the deletion its span would have been parsed from.
    """
    where = f'c.{start}' if (start, start_offset) == (end, end_offset) else f'c.{start}_{end}'
    return responses.clinvar_record(
        clinvar_id,
        hgvs=f'{transcript}:{where}{edit}',
        span=responses.coding_span(transcript, start, end, start_offset=start_offset, end_offset=end_offset),
    )


def _placed(*records: clinvar_pb2.ClinVarRecord, start: int = 1108, end: int = 1110) -> placement.Placement:
    return placement.records_in_span(records, transcript=_TRANSCRIPT, start=start, end=end, unparsed=())


def test_a_record_inside_the_codon_is_placed_there() -> None:
    inside = _record('VCV1', 1109, 1109)
    assert _placed(inside).inside == (inside,)


def test_a_record_at_a_neighbouring_codon_is_outside() -> None:
    # The search runs on genomic coordinates, so a codon's pool reaches its neighbours.
    assert _placed(_record('VCV2', 1111, 1111)).outside


def test_a_deletion_spanning_the_codon_is_inside_it() -> None:
    # The record that a coordinate range alone misses: it begins before and ends after.
    spanning = _record('VCV3', 1100, 1120)
    assert _placed(spanning).inside == (spanning,)


def test_a_record_numbered_on_another_transcript_is_never_compared() -> None:
    other = _record('VCV4', 1109, 1109, transcript='NM_999999.1')
    placed = _placed(other)
    assert placed.other_transcript == (other,)
    assert not placed.inside


def test_the_accessions_version_run_does_not_move_a_record_off_the_transcript() -> None:
    # ClinVar indexes a record under whichever version its submitter wrote.
    superseded = _record('VCV5', 1109, 1109, transcript='NM_000123.3')
    assert _placed(superseded).inside == (superseded,)


def test_an_intronic_position_is_not_in_the_exon() -> None:
    # c.1110+1 is the first base of the following intron, not a second name for c.1110.
    donor = _record('VCV6', 1110, 1110, start_offset=1, end_offset=1)
    placed = placement.records_in_span([donor], transcript=_TRANSCRIPT, start=1000, end=1110, unparsed=())
    assert placed.outside == (donor,)


def test_a_record_spanning_an_intron_reaches_the_exon_it_ends_in() -> None:
    # From the intron after c.1000 to c.1109: the exonic bases at the far end are covered.
    crossing = _record('VCV7', 1000, 1109, start_offset=1)
    assert _placed(crossing).inside == (crossing,)


def test_a_five_prime_utr_position_is_not_a_coding_one() -> None:
    # c.-25 and c.25 are different bases; the region is what tells them apart.
    utr = _record('VCV8', -25, -25)
    assert _placed(utr, start=20, end=30).outside == (utr,)


def test_the_five_prime_utr_counts_down_toward_the_coding_start() -> None:
    inside = _record('VCV9', -20, -20)
    outside = _record('VCV10', -40, -40)
    placed = placement.records_in_span([inside, outside], transcript=_TRANSCRIPT, start=-25, end=-1, unparsed=())
    assert placed.inside == (inside,)
    assert placed.outside == (outside,)


def test_a_span_crossing_the_coding_start_is_refused() -> None:
    with pytest.raises(ValueError, match='crosses the coding start'):
        _placed(start=-20, end=5)


@pytest.mark.parametrize(('start', 'end'), [(0, 10), (10, 0), (20, 10)])
def test_a_malformed_span_is_refused(start: int, end: int) -> None:
    with pytest.raises(ValueError, match=r'c\. numbering has no 0|ends before it begins'):
        _placed(start=start, end=end)


def test_a_record_with_no_coding_span_is_carried_through_by_name() -> None:
    # A copy-number title carries no c. span; dropping it silently understates the exon.
    unplaceable = responses.clinvar_record('VCV11', hgvs='GRCh38/hg38 1q21.1(chr1:100-200)x1')
    placed = placement.records_in_span(
        [unplaceable, _record('VCV12', 1109, 1109)],
        transcript=_TRANSCRIPT,
        start=1108,
        end=1110,
        unparsed=('VCV11',),
    )
    assert placed.unplaceable == ('VCV11',)
    assert [record.clinvar_id for record in placed.inside] == ['VCV12']


def test_a_record_the_response_did_not_report_as_unplaceable_is_refused() -> None:
    with pytest.raises(ValueError, match='not among the records'):
        _placed(responses.clinvar_record('VCV13', hgvs='GRCh38/hg38 1q21.1(chr1:100-200)x1'))


def test_every_record_lands_in_exactly_one_group() -> None:
    """The property a caller reading one group depends on: nothing was dropped on the way."""
    records = [
        _record('VCV14', 1109, 1109),
        _record('VCV15', 1200, 1200),
        _record('VCV16', 1109, 1109, transcript='NM_999999.1'),
    ]
    placed = _placed(*records)
    grouped = [*placed.inside, *placed.outside, *placed.other_transcript]
    assert [record.clinvar_id for record in grouped] == [record.clinvar_id for record in records]


def test_a_waiving_variant_carries_the_records_identity_and_review_status() -> None:
    waiving = placement.waiving_variant(
        responses.clinvar_record('VCV17', classification='Pathogenic', review_stars=3, hgvs='c.100A>G'),
        basis=scoring.PathogenicVariantBasis.EXPERT_CLASSIFIED,
    )
    assert 'VCV17' in waiving.variant
    assert waiving.review_stars == 3


def test_a_split_pathogenic_aggregate_does_not_defeat_the_reduction() -> None:
    # SM18 §17 rests on P variants; ClinVar's split call resolves to LP, which §17 does not admit.
    with pytest.raises(ValueError, match='SM18'):
        placement.waiving_variant(
            responses.clinvar_record('VCV18', classification='Pathogenic/Likely pathogenic', review_stars=3),
            basis=scoring.PathogenicVariantBasis.EXPERT_CLASSIFIED,
        )


def test_a_pathogenic_record_below_the_expert_panel_rung_cannot_claim_one() -> None:
    with pytest.raises(ValueError, match='expert panel'):
        placement.waiving_variant(
            responses.clinvar_record('VCV19', classification='Pathogenic', review_stars=1),
            basis=scoring.PathogenicVariantBasis.EXPERT_CLASSIFIED,
        )


def test_an_insertion_bracketing_the_span_is_not_in_it() -> None:
    # HGVS writes an insertion range exclusive: c.1107_1108insG alters neither base, so reading its
    # span as an overlap would enter it in the codon it merely abuts.
    abutting = _record('VCV20', 1107, 1108, edit='insG')
    assert _placed(abutting).outside == (abutting,)


def test_an_insertion_between_two_bases_of_the_span_is_in_it() -> None:
    interior = _record('VCV21', 1108, 1109, edit='insG')
    assert _placed(interior).inside == (interior,)


def test_a_deletion_over_the_same_two_bases_is_placed_by_what_it_covers() -> None:
    # The span cannot tell the two apart, which is why the edit is read off the expression.
    deletion = _record('VCV22', 1107, 1108, edit='del')
    assert _placed(deletion).inside == (deletion,)


def test_a_delins_covers_its_endpoints_as_a_deletion_does() -> None:
    replaced = _record('VCV23', 1107, 1108, edit='delinsGG')
    assert _placed(replaced).inside == (replaced,)


def test_a_record_carrying_a_span_and_no_expression_is_refused() -> None:
    # The span was parsed from an expression, so a record holding one without the other is a
    # response that cannot be placed rather than one to place as a deletion.
    with pytest.raises(ValueError, match='no expression it was parsed from'):
        _placed(responses.clinvar_record('VCV24', span=responses.coding_span(_TRANSCRIPT, 1109, 1109)))


def test_the_unplaceable_ids_are_the_pools_own() -> None:
    # `records_with_unparsed_hgvs` covers `this_variant` too, so an id outside the pool is not this
    # pool's to report; the four groups have to reconcile against the records handed in.
    placed = placement.records_in_span(
        [_record('VCV25', 1109, 1109)],
        transcript=_TRANSCRIPT,
        start=1108,
        end=1110,
        unparsed=('VCV_THIS_VARIANT',),
    )
    assert placed.unplaceable == ()
