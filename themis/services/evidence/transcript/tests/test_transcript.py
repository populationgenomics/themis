"""Behaviour tests for the transcript servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping

import grpc
import grpc.aio
import pytest

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, transcript_pb2, transcript_pb2_grpc
from themis.services.evidence.transcript import backend as transcript_backend
from themis.services.evidence.transcript import servicer as servicer_mod
from themis.testing import in_process_grpc

_GOOD_TOKEN = (('x-themis-session-token', 'good'),)
_BAD_TOKEN = (('x-themis-session-token', 'bad'),)
_POOL_RECORDS = 500


async def _session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='proj', analysis_id='ana')
    raise session_mod.UnresolvedSessionError


def _backend(
    get_structure: Mapping[str, transcript_pb2.GetStructureResponse] | None = None,
    assess_exon_relevance: Mapping[str, transcript_pb2.AssessExonRelevanceResponse] | None = None,
) -> transcript_backend.FixtureBackend:
    return transcript_backend.FixtureBackend(
        {} if get_structure is None else get_structure, {} if assess_exon_relevance is None else assess_exon_relevance
    )


@contextlib.asynccontextmanager
async def _serving(
    backend: transcript_backend.TranscriptBackend,
) -> AsyncIterator[transcript_pb2_grpc.TranscriptAsyncStub]:
    def register(server: grpc.aio.Server) -> None:
        transcript_pb2_grpc.add_TranscriptServicer_to_server(servicer_mod.Servicer(backend, _session_resolver), server)

    async with in_process_grpc.serving(register) as channel:
        yield transcript_pb2_grpc.TranscriptStub(channel)


def test_exon_relevance_is_keyed_by_gene_transcript_exon() -> None:
    tables = _backend(
        assess_exon_relevance={
            'BRCA1:NM_007294.4:10': transcript_pb2.AssessExonRelevanceResponse(
                in_mane_select=True, clinvar_plp_density=7
            )
        }
    )

    async def run() -> transcript_pb2.AssessExonRelevanceResponse:
        async with _serving(tables) as stub:
            return await stub.AssessExonRelevance(_relevance_request(), metadata=_GOOD_TOKEN)

    signals = asyncio.run(run())
    assert signals.in_mane_select
    assert signals.clinvar_plp_density == 7


@pytest.mark.parametrize('gene', ['', '   '])
def test_exon_relevance_rejects_an_absent_gene(gene: str) -> None:
    """The gene is the inventory's denominator, so an empty one would answer over no transcripts."""
    assert 'gene' in _refused(_relevance_request(gene=gene))


@pytest.mark.parametrize('unset', ['in_mane_select', 'in_mane_plus_clinical'])
def test_exon_relevance_rejects_an_unset_mane_flag(unset: str) -> None:
    """Both are echoed, and false/false forces "Few" — so an unsent flag would arrive as evidence."""
    request = _relevance_request()
    request.ClearField(unset)
    assert unset in _refused(request)


def _relevance_request(gene: str = 'BRCA1') -> transcript_pb2.AssessExonRelevanceRequest:
    return transcript_pb2.AssessExonRelevanceRequest(
        gene=gene, transcript='NM_007294.4', exon=10, in_mane_select=True, in_mane_plus_clinical=False
    )


def _refused(request: transcript_pb2.AssessExonRelevanceRequest) -> str:
    """The INVALID_ARGUMENT detail the servicer refuses this request with."""

    async def run() -> transcript_pb2.AssessExonRelevanceResponse:
        async with _serving(_backend()) as stub:
            return await stub.AssessExonRelevance(request, metadata=_GOOD_TOKEN)

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    return caught.value.details() or ''


def test_transcript_structure_is_keyed_by_transcript_build_and_queried_position() -> None:
    """The table is position-independent but the located position is not, so it is part of the key."""
    tables = _backend(
        get_structure={
            'NM_001042492.3:GRCh38:c:3496': transcript_pb2.GetStructureResponse(
                gene='NF1', position=transcript_pb2.TranscriptPosition(exon=26, nt_to_exon_end=1)
            )
        }
    )

    async def run() -> transcript_pb2.GetStructureResponse:
        async with _serving(tables) as stub:
            return await stub.GetStructure(
                transcript_pb2.GetStructureRequest(
                    transcript='NM_001042492.3', genome_build='GRCh38', cds_position=3496
                ),
                metadata=_GOOD_TOKEN,
            )

    resp = asyncio.run(run())
    assert resp.gene == 'NF1'
    assert resp.position.exon == 26
    assert resp.position.nt_to_exon_end == 1
