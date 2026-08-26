"""Tests for the splice-outcome prediction, over the recorded NF1 exon table + transcript sequence."""

from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from themis.rpc import splice_pb2
from themis.services.evidence import errors
from themis.services.evidence.splice import outcome as splice_outcome
from themis.services.evidence.upstreams import transcript_sequence, transcript_structure

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_ACCESSION = 'NM_001042492.3'


def _structure() -> transcript_structure.TranscriptStructureResult:
    return transcript_structure.parse_transcript_structure(
        json.loads((_FIXTURES / 'transcript_structure.json').read_text()),
        transcript=_ACCESSION,
        genome_build='GRCh38',
        dataset_versions=('vvta_2025_02',),
        query='q',
    )


def _sequence() -> transcript_sequence.TranscriptSequenceResult:
    return transcript_sequence.parse_fasta(
        (_FIXTURES / 'transcript_sequence.fasta').read_text(), accession=_ACCESSION, query='q'
    )


def _skips(affected_exon: int) -> list[splice_pb2.PredictedSkip]:
    return splice_outcome.predict_skips(_structure(), _sequence(), affected_exon=affected_exon)


def test_nf1_exon_26_skipping_is_out_of_frame_and_nmd_predicted() -> None:
    """The worked case: 182 nt removed, frame shifted, PTC at codon 1133, well clear of the 50-nt rule."""
    skip = _skips(26)[0]
    assert list(skip.skipped_exons) == [26]
    assert skip.coding_nt_removed == 182
    assert skip.frame_shift == 2
    assert skip.product == splice_pb2.SPLICE_PRODUCT_PREMATURE_STOP
    assert skip.ptc_cds_position == 3397
    assert skip.ptc_codon == 1133
    assert skip.nt_upstream_of_last_junction == 4798
    assert skip.nmd_predicted


def test_an_out_of_frame_skip_also_reports_the_adjacent_pairs() -> None:
    """Losing the neighbouring exon too is the frame-restoring alternative an analyst weighs."""
    assert [list(skip.skipped_exons) for skip in _skips(26)] == [[26], [25, 26], [26, 27]]


def test_an_in_frame_skip_reports_only_itself() -> None:
    (skip,) = _skips(25)
    assert skip.frame_shift == 0
    assert skip.product == splice_pb2.SPLICE_PRODUCT_INFRAME_DELETION
    assert not skip.HasField('ptc_cds_position')
    assert not skip.nmd_predicted


def test_losing_the_first_exon_routes_to_the_start_lost_tree() -> None:
    """No initiation codon means no reading frame to search for a PTC in."""
    assert _skips(1)[0].product == splice_pb2.SPLICE_PRODUCT_START_LOST


def test_losing_the_last_exon_leaves_the_product_without_a_termination_codon() -> None:
    """The reference stop lives in the final exon, so the product reads to the 3' end: NSD, not NMD."""
    skip = _skips(58)[0]
    assert skip.product == splice_pb2.SPLICE_PRODUCT_NO_TERMINATION
    assert not skip.nmd_predicted


@pytest.mark.parametrize('affected_exon', [1, 25, 26, 27, 57, 58])
def test_the_ptc_fields_are_present_exactly_for_a_premature_stop(affected_exon: int) -> None:
    """A PTC coordinate on a product that terminates normally would be read as a null-path variant."""
    for skip in _skips(affected_exon):
        premature = skip.product == splice_pb2.SPLICE_PRODUCT_PREMATURE_STOP
        assert skip.HasField('ptc_cds_position') is premature
        assert skip.HasField('ptc_codon') is premature
        assert skip.HasField('nt_upstream_of_last_junction') is premature
        assert skip.nmd_predicted <= premature


@pytest.mark.parametrize('affected_exon', [1, 26, 57])
def test_a_reported_ptc_terminates_the_products_own_reading_frame(affected_exon: int) -> None:
    """The PTC is a c. coordinate in the aberrant transcript, so it must be codon-aligned there."""
    for skip in _skips(affected_exon):
        if skip.HasField('ptc_cds_position'):
            assert skip.ptc_cds_position % 3 == 1


@pytest.mark.parametrize('affected_exon', [0, -1, 59])
def test_an_exon_the_transcript_does_not_have_is_an_invalid_request(affected_exon: int) -> None:
    with pytest.raises(errors.InvalidRequestError, match='has exons 1-58'):
        _skips(affected_exon)


def test_a_sequence_that_does_not_match_the_exon_table_raises() -> None:
    """Two upstreams describing different records would splice against the wrong bases."""
    truncated = dataclasses.replace(_sequence(), sequence='ACGT')
    with pytest.raises(ValueError, match='exon table'):
        splice_outcome.predict_skips(_structure(), truncated, affected_exon=26)
