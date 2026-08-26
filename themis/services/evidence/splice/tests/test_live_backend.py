"""LiveBackend composition: the dual-provenance split, and the exon a c. position resolves onto.

Every upstream client function is replaced with a canned Result — the outcome leg over the recorded
NF1 exon table and transcript sequence — so no test here touches the network.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import Awaitable, Callable

import httpx
import pytest

from themis.rpc import splice_pb2
from themis.services.evidence.splice import backend as splice_backend
from themis.services.evidence.upstreams import spliceai, transcript_sequence, transcript_structure

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_NF1_TRANSCRIPT = 'NM_001042492.3'


def _returns[T](value: T) -> Callable[..., Awaitable[T]]:
    """An async stand-in for an upstream client function that ignores its args and returns `value`."""

    async def fake(*_args: object, **_kwargs: object) -> T:
        return value

    return fake


def _run[T](call: Callable[[splice_backend.LiveBackend], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx.AsyncClient() as client:
            return await call(splice_backend.LiveBackend(client))

    return asyncio.run(run())


def _nf1_structure() -> transcript_structure.TranscriptStructureResult:
    return transcript_structure.parse_transcript_structure(
        json.loads((_FIXTURES / 'transcript_structure.json').read_text()),
        transcript=_NF1_TRANSCRIPT,
        genome_build='GRCh38',
        dataset_versions=('vvta_2025_02',),
        query='gene2transcripts',
    )


def _nf1_sequence() -> transcript_sequence.TranscriptSequenceResult:
    return transcript_sequence.parse_fasta(
        (_FIXTURES / 'transcript_sequence.fasta').read_text(), accession=_NF1_TRANSCRIPT, query='efetch'
    )


def test_predict_skip_outcome_takes_the_exon_from_a_cds_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chaining from a c. HGVS must not need the caller to work out the exon themselves."""
    monkeypatch.setattr(transcript_structure, 'fetch_transcript_structure', _returns(_nf1_structure()))
    monkeypatch.setattr(transcript_sequence, 'fetch_transcript_sequence', _returns(_nf1_sequence()))
    resp = _run(
        lambda be: be.predict_skip_outcome(
            splice_pb2.PredictSkipOutcomeRequest(transcript=_NF1_TRANSCRIPT, genome_build='GRCh38', cds_position=3496)
        )
    )
    assert resp.affected_exon == 26
    assert resp.skips[0].nmd_predicted
    assert [p.source for p in resp.provenance] == [
        'VariantValidator gene2transcripts',
        'NCBI Nucleotide (E-utilities efetch)',
    ]
    sequence_record = resp.raw.fields['transcript_sequence'].struct_value
    assert sequence_record.fields['length'].number_value == 12373
    assert 'transcript_structure' in resp.raw


def test_predict_deltas_emits_dual_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        spliceai,
        'fetch_splice',
        _returns(
            spliceai.SpliceResult(
                spliceai_gain=0.04,
                spliceai_loss=0.83,
                pangolin_gain=0.01,
                pangolin_loss=0.71,
                raw={'spliceai': {}, 'pangolin': {}},
                source='Broad SpliceAI + Pangolin',
                dataset_versions=('GRCh38',),
                query='SPLICEAI_URL | PANGOLIN_URL',
            )
        ),
    )
    resp = _run(lambda be: be.predict_deltas(splice_pb2.PredictDeltasRequest(variant='17-43093464-A-G')))
    assert resp.spliceai_gain == 0.04
    assert resp.spliceai_loss == 0.83
    assert resp.pangolin_gain == 0.01
    assert resp.pangolin_loss == 0.71
    assert [p.source for p in resp.provenance] == ['Broad SpliceAI', 'Broad Pangolin']
    assert resp.provenance[0].query == 'SPLICEAI_URL'
    assert resp.provenance[1].query == 'PANGOLIN_URL'


def test_predict_deltas_leaves_scores_absent_when_unscored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        spliceai,
        'fetch_splice',
        _returns(
            spliceai.SpliceResult(
                spliceai_gain=None,
                spliceai_loss=None,
                pangolin_gain=None,
                pangolin_loss=None,
                raw={},
                source='Broad SpliceAI + Pangolin',
                dataset_versions=('GRCh38',),
                query='A | B',
            )
        ),
    )
    resp = _run(lambda be: be.predict_deltas(splice_pb2.PredictDeltasRequest(variant='x')))
    for field in ('spliceai_gain', 'spliceai_loss', 'pangolin_gain', 'pangolin_loss'):
        assert not resp.HasField(field)
    assert len(resp.provenance) == 2
