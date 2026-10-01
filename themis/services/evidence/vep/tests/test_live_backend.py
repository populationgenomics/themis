"""LiveBackend composition: the annotation `Annotate` builds from a canned VEP result.

The upstream is a canned Result or the recorded VEP payload behind a mock transport, so no test here
touches the network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import decimal
import json
import pathlib
from collections.abc import Awaitable, Callable

import httpx2
import pytest

from themis.evidence.models import evidence_pb2
from themis.rpc import vep_pb2
from themis.services.evidence.upstreams import vep
from themis.services.evidence.vep import backend as vep_backend
from themis.svcv4 import predictor_policy, predictors

_RECORDED = json.loads(
    (pathlib.Path(__file__).parents[2] / 'upstreams' / 'tests' / 'fixtures' / 'vep.json').read_text()
)


def _run[T](call: Callable[[vep_backend.LiveBackend], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient() as client:
            return await call(vep_backend.LiveBackend(client))

    return asyncio.run(run())


def test_annotate_passthrough_pins_grch38(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_vep(_variant: str, predictors: list[str], genome_build: str, **_kwargs: object) -> vep.VepResult:
        captured['build'] = genome_build
        captured['predictors'] = predictors
        return vep.VepResult(
            most_severe_consequence=evidence_pb2.CONSEQUENCE_MISSENSE,
            gene_symbol='AGT',
            hgnc_id='HGNC:333',
            transcripts=(
                vep.TranscriptAnnotation(
                    transcript_id='ENST00000366667',
                    mane_select='NM_000029.4',
                    mane_plus_clinical='',
                    scores=(vep.PredictorScore(predictor='AlphaMissense', score=0.93),),
                ),
            ),
            raw={'x': 1},
            source='Ensembl VEP REST',
            dataset_versions=('GRCh38',),
            query='q',
        )

    monkeypatch.setattr(vep, 'fetch_vep', fake_vep)
    annotation = _run(lambda be: be.annotate(vep_pb2.AnnotateRequest(variant='v', predictors=['AlphaMissense'])))
    assert captured['build'] == 'GRCh38'
    assert captured['predictors'] == ['AlphaMissense']
    assert annotation.most_severe_consequence == evidence_pb2.CONSEQUENCE_MISSENSE
    (transcript,) = annotation.transcripts
    assert (transcript.transcript_id, transcript.mane_select, transcript.mane_plus_clinical) == (
        'ENST00000366667',
        'NM_000029.4',
        '',
    )
    assert [(s.predictor, s.score) for s in transcript.scores] == [('AlphaMissense', 0.93)]
    assert annotation.raw['x'] == 1
    assert annotation.provenance[0].source == 'Ensembl VEP REST'


def _recorded(request: httpx2.Request) -> httpx2.Response:
    if request.url.path == '/info/software':
        return httpx2.Response(200, json={'release': 116})
    return httpx2.Response(200, json=_RECORDED)


@pytest.mark.parametrize(
    ('predictor', 'score', 'points'),
    [(predictors.Predictor.BAYESDEL, '0.0531436', '0.0'), (predictors.Predictor.ALPHAMISSENSE, '0.9467', '2.0')],
)
@pytest.mark.parametrize('transcript', ['NM_001042492.3', 'ENST00000358273.9', 'ENST00000358273'])
def test_the_recorded_answer_bins_through_the_library(
    predictor: predictors.Predictor, score: str, points: str, transcript: str
) -> None:
    """The live path end to end: the recorded VEP payload, the service's reading of it, the library's.

    Each half has its own fixture elsewhere; only this one would fail if the two disagreed about
    where a score sits. The MANE Select RefSeq accession is the one the workflow holds.
    """
    selection = dataclasses.replace(predictor_policy.load_policy().for_hgnc_id('HGNC:7765'), predictor=predictor)
    request = predictor_policy.annotate_request(selection, variant='NM_001042492.3:c.3496G>C')

    async def run() -> vep_pb2.AnnotateResponse:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(_recorded)) as client:
            return await vep_backend.LiveBackend(client).annotate(request)

    scored = predictor_policy.mis_prd_from_vep(request, asyncio.run(run()), selection, transcript=transcript)
    assert scored.transcript_id == 'ENST00000358273.9'
    assert scored.mane_pair == ('NM_001042492.3' if transcript.startswith('NM_') else '')
    assert scored.score == decimal.Decimal(score)
    assert scored.points == decimal.Decimal(points)
