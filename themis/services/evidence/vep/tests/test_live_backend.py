"""LiveBackend composition: the annotation `Annotate` builds from a canned VEP result.

The upstream client function is replaced with a canned Result, so no test here touches the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx2
import pytest

from themis.evidence.models import evidence_pb2
from themis.rpc import vep_pb2
from themis.services.evidence.upstreams import vep
from themis.services.evidence.vep import backend as vep_backend


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
            evidence_pb2.CONSEQUENCE_MISSENSE, 'AGT', 'HGNC:333', {'x': 1}, 'Ensembl VEP REST', ('GRCh38',), 'q'
        )

    monkeypatch.setattr(vep, 'fetch_vep', fake_vep)
    annotation = _run(lambda be: be.annotate(vep_pb2.AnnotateRequest(variant='v', predictors=['AlphaMissense'])))
    assert captured['build'] == 'GRCh38'
    assert captured['predictors'] == ['AlphaMissense']
    assert annotation.most_severe_consequence == evidence_pb2.CONSEQUENCE_MISSENSE
    assert annotation.raw['x'] == 1
    assert annotation.provenance[0].source == 'Ensembl VEP REST'
