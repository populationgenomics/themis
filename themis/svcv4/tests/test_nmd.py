"""Tests for NMD (50-nt rule) and NSD prediction."""

from __future__ import annotations

import pytest

from themis.rpc import splice_pb2
from themis.svcv4 import nmd, provenance
from themis.svcv4.tests import responses


def test_single_exon_never_triggers_nmd() -> None:
    assert nmd.predicts_nmd((300,), 100) is False


def test_ptc_far_upstream_of_last_junction_triggers_nmd() -> None:
    # Exons [100, 100, 100]; last junction at position 200; PTC at 100 is 100 nt upstream (>= 50).
    assert nmd.predicts_nmd((100, 100, 100), 100) is True


def test_ptc_exactly_fifty_upstream_triggers_nmd() -> None:
    # Last junction at 200; PTC at 150 is exactly 50 nt upstream.
    assert nmd.predicts_nmd((100, 100, 100), 150) is True


def test_ptc_within_fifty_of_last_junction_escapes() -> None:
    assert nmd.predicts_nmd((100, 100, 100), 151) is False


def test_ptc_in_last_exon_escapes() -> None:
    assert nmd.predicts_nmd((100, 100, 100), 260) is False


def test_nmd_rejects_ptc_outside_transcript() -> None:
    with pytest.raises(ValueError, match='outside transcript'):
        nmd.predicts_nmd((100, 100), 500)


def test_nsd_predicted_when_no_inframe_stop_before_polya() -> None:
    assert nmd.predicts_nsd(None, 900) is True
    assert nmd.predicts_nsd(950, 900) is True  # next stop is past the polyA


def test_nsd_not_predicted_when_inframe_stop_precedes_polya() -> None:
    assert nmd.predicts_nsd(850, 900) is False  # C-terminal extension, not NSD


def test_nsd_rejects_nonpositive_polya() -> None:
    with pytest.raises(ValueError, match='polyA'):
        nmd.predicts_nsd(None, 0)


def test_the_ptc_is_converted_from_c_to_n_before_the_margin_is_measured() -> None:
    # c.1 sits at n.51, so a PTC at c.100 is n.150 and the margin runs from there to n.800 — the last
    # base before the final junction. Measuring from c.100 itself would report 50 nt more.
    call = nmd.nmd_from_structure(responses.transcript_structure(), ptc_cds_position=100)
    assert call.predicted is True
    assert call.nt_upstream_of_last_junction == 650
    assert 'n.150' in call.derivation
    assert provenance.Release('VariantValidator', 'VariantValidator 2.2.0') in call.releases


def test_a_ptc_inside_the_last_fifty_nucleotides_does_not_trigger_decay() -> None:
    # n.760 leaves a 40 nt margin, inside the rule's boundary.
    call = nmd.nmd_from_structure(responses.transcript_structure(), ptc_cds_position=710)
    assert call.predicted is False
    assert call.nt_upstream_of_last_junction == 40


def test_a_ptc_past_the_last_junction_reports_a_negative_margin() -> None:
    call = nmd.nmd_from_structure(responses.transcript_structure(), ptc_cds_position=900)
    assert call.predicted is False
    assert call.nt_upstream_of_last_junction is not None
    assert call.nt_upstream_of_last_junction < 0


def test_a_single_exon_transcript_has_no_junction_to_measure_against() -> None:
    call = nmd.nmd_from_structure(responses.transcript_structure(exon_lengths=(1000,)), ptc_cds_position=100)
    assert call.predicted is False
    assert call.nt_upstream_of_last_junction is None


def test_a_ptc_past_the_reference_stop_is_not_a_premature_one() -> None:
    with pytest.raises(ValueError, match='not a premature stop'):
        nmd.nmd_from_structure(responses.transcript_structure(), ptc_cds_position=5000)


def test_an_exon_table_stating_no_position_for_the_coding_start_is_refused() -> None:
    # Unset, the field is 0 and the c. to n. conversion silently becomes no conversion — which on a
    # long 5'UTR moves the margin across the 50-nt boundary.
    structure = responses.transcript_structure()
    structure.ClearField('cds_transcript_start')
    with pytest.raises(ValueError, match=r'no transcript position for c\.1'):
        nmd.nmd_from_structure(structure, ptc_cds_position=100)


@pytest.mark.parametrize('position', [0, -3])
def test_a_ptc_that_is_not_a_positive_coding_position_is_refused(position: int) -> None:
    with pytest.raises(ValueError, match=r'positive c\. position'):
        nmd.nmd_from_structure(responses.transcript_structure(), ptc_cds_position=position)


def test_an_exon_table_that_does_not_describe_the_transcript_is_refused() -> None:
    # The margin is measured over the table, so a table disagreeing with its own transcript length
    # measures it over something else.
    structure = responses.transcript_structure()
    structure.transcript_length += 7
    with pytest.raises(ValueError, match='stated transcript length'):
        nmd.nmd_from_structure(structure, ptc_cds_position=100)


def test_the_skips_own_determination_is_read_with_its_margin() -> None:
    # The rpc makes the call over the ABERRANT transcript's structure, which the reference table
    # cannot be made to describe.
    call = nmd.nmd_from_skip(responses.predicted_skip())
    assert call.predicted is True
    assert call.nt_upstream_of_last_junction == 260
    assert 'aberrant transcript' in call.derivation


def test_a_skip_predicting_decay_without_a_premature_stop_is_refused() -> None:
    with pytest.raises(ValueError, match='premature stop triggers'):
        nmd.nmd_from_skip(
            responses.predicted_skip(product=splice_pb2.SPLICE_PRODUCT_INFRAME_DELETION, ptc_cds_position=None)
        )


def test_a_skip_stating_a_premature_stop_without_its_position_is_refused() -> None:
    with pytest.raises(ValueError, match='where it lands'):
        nmd.nmd_from_skip(responses.predicted_skip(ptc_cds_position=None))


def test_an_in_frame_product_never_triggers_decay() -> None:
    call = nmd.nmd_from_skip(
        responses.predicted_skip(
            product=splice_pb2.SPLICE_PRODUCT_INFRAME_DELETION,
            nmd_predicted=False,
            ptc_cds_position=None,
            nt_upstream_of_last_junction=None,
        )
    )
    assert call.predicted is False
    assert call.nt_upstream_of_last_junction is None
