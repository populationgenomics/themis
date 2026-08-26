"""Behaviour tests for the splice servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping

import grpc
import grpc.aio
import pytest

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, splice_pb2, splice_pb2_grpc
from themis.services.evidence.splice import backend as splice_backend
from themis.services.evidence.splice import servicer as servicer_mod
from themis.testing import in_process_grpc

_GOOD_TOKEN = (('x-themis-session-token', 'good'),)
_BAD_TOKEN = (('x-themis-session-token', 'bad'),)
_POOL_RECORDS = 500


async def _session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='proj', analysis_id='ana')
    raise session_mod.UnresolvedSessionError


def _backend(
    predict_deltas: Mapping[str, splice_pb2.PredictDeltasResponse] | None = None,
    predict_skip_outcome: Mapping[str, splice_pb2.PredictSkipOutcomeResponse] | None = None,
) -> splice_backend.FixtureBackend:
    return splice_backend.FixtureBackend(
        {} if predict_deltas is None else predict_deltas, {} if predict_skip_outcome is None else predict_skip_outcome
    )


@contextlib.asynccontextmanager
async def _serving(backend: splice_backend.SpliceBackend) -> AsyncIterator[splice_pb2_grpc.SpliceAsyncStub]:
    def register(server: grpc.aio.Server) -> None:
        splice_pb2_grpc.add_SpliceServicer_to_server(servicer_mod.Servicer(backend, _session_resolver), server)

    async with in_process_grpc.serving(register) as channel:
        yield splice_pb2_grpc.SpliceStub(channel)


def test_splice_outcome_is_keyed_by_transcript_build_and_affected_exon() -> None:
    tables = _backend(
        predict_skip_outcome={
            'NM_001042492.3:GRCh38:exon:26': splice_pb2.PredictSkipOutcomeResponse(
                affected_exon=26,
                skips=[
                    splice_pb2.PredictedSkip(
                        skipped_exons=[26],
                        frame_shift=2,
                        product=splice_pb2.SPLICE_PRODUCT_PREMATURE_STOP,
                        nmd_predicted=True,
                    )
                ],
            )
        }
    )

    async def run() -> splice_pb2.PredictSkipOutcomeResponse:
        async with _serving(tables) as stub:
            return await stub.PredictSkipOutcome(
                splice_pb2.PredictSkipOutcomeRequest(transcript='NM_001042492.3', genome_build='GRCh38', exon=26),
                metadata=_GOOD_TOKEN,
            )

    resp = asyncio.run(run())
    assert resp.affected_exon == 26
    assert resp.skips[0].nmd_predicted


@pytest.mark.parametrize(
    ('request_kwargs', 'detail'),
    [
        # Neither selector set: proto3 would read the unset `exon` as 0 — a silently wrong answer.
        ({}, 'affected exon'),
        ({'exon': 0}, '1-based exon number'),
        ({'exon': -3}, '1-based exon number'),
    ],
)
def test_splice_outcome_requires_a_real_affected_exon(request_kwargs: dict[str, int], detail: str) -> None:
    async def run() -> splice_pb2.PredictSkipOutcomeResponse:
        async with _serving(_backend()) as stub:
            return await stub.PredictSkipOutcome(
                splice_pb2.PredictSkipOutcomeRequest(
                    transcript='NM_001042492.3', genome_build='GRCh38', **request_kwargs
                ),
                metadata=_GOOD_TOKEN,
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert detail in (caught.value.details() or '')


def test_splice_is_keyed_by_genomic_locus() -> None:
    tables = _backend(predict_deltas={'17-43093464-A-G': splice_pb2.PredictDeltasResponse(spliceai_loss=0.83)})

    async def run() -> splice_pb2.PredictDeltasResponse:
        async with _serving(tables) as stub:
            return await stub.PredictDeltas(
                splice_pb2.PredictDeltasRequest(variant='17-43093464-A-G'), metadata=_GOOD_TOKEN
            )

    resp = asyncio.run(run())
    assert resp.HasField('spliceai_loss')
    assert resp.spliceai_loss == 0.83
