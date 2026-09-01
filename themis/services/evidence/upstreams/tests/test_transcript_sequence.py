"""Tests for the transcript-sequence adapter, against a recorded FASTA via a mocked transport."""

from __future__ import annotations

import asyncio
import pathlib
import re

import httpx2
import pytest

from themis.services.evidence import errors
from themis.services.evidence.upstreams import transcript_sequence

_ACCESSION = 'NM_001042492.3'
_FASTA = (pathlib.Path(__file__).parent / 'fixtures' / 'transcript_sequence.fasta').read_text()


def _fetch(handler: httpx2.MockTransport) -> transcript_sequence.TranscriptSequenceResult:
    async def run() -> transcript_sequence.TranscriptSequenceResult:
        async with httpx2.AsyncClient(transport=handler) as client:
            return await transcript_sequence.fetch_transcript_sequence(_ACCESSION, http_client=client)

    return asyncio.run(run())


def test_the_sequence_is_indexed_by_the_transcripts_own_n_coordinates() -> None:
    """The n. position p is sequence[p-1]: NF1's CDS starts at n.334, so that base opens an ATG."""
    result = _fetch(httpx2.MockTransport(lambda _: httpx2.Response(200, text=_FASTA)))
    assert result.accession == _ACCESSION
    assert len(result.sequence) == 12373
    assert result.sequence[333:336] == 'ATG'
    assert result.sequence[8850:8853] in {'TAA', 'TAG', 'TGA'}
    assert set(result.sequence) <= set('ACGTN')


def test_a_superseded_version_is_rejected_rather_than_spliced_against() -> None:
    """Efetch answers an older accession version with its own bases; the coordinates would not line up."""
    with pytest.raises(ValueError, match=re.escape('NM_001042492.2')):
        transcript_sequence.parse_fasta('>NM_001042492.2 NF1\nACGT\n', accession=_ACCESSION, query='q')


def test_a_body_that_is_not_a_fasta_record_raises() -> None:
    with pytest.raises(ValueError, match='no FASTA record'):
        transcript_sequence.parse_fasta('Error: failed to retrieve sequence', accession=_ACCESSION, query='q')


def test_a_record_ncbi_does_not_hold_is_not_found() -> None:
    """Efetch answers an accession it holds no sequence for with a 400: an answer, not an outage."""
    with pytest.raises(errors.UnknownVariantError, match='holds no sequence'):
        _fetch(httpx2.MockTransport(lambda _: httpx2.Response(400, text='Error: Failed to retrieve sequence')))


def test_a_server_fault_stays_an_http_error() -> None:
    with pytest.raises(httpx2.HTTPStatusError):
        _fetch(httpx2.MockTransport(lambda _: httpx2.Response(502, text='bad gateway')))


@pytest.mark.parametrize('status', [403, 429])
def test_a_4xx_about_the_client_is_not_read_as_an_absent_record(status: int) -> None:
    """NCBI blocks an abusive client with 403 and throttles a keyless one with 429.

    Neither says anything about the accession, and a NOT_FOUND here is what `SpliceOutcome` would
    then rest a splice prediction on — while never retrying the call that would have cleared it.
    """
    with pytest.raises(httpx2.HTTPStatusError):
        _fetch(httpx2.MockTransport(lambda _: httpx2.Response(status, text='no sequence for you')))
