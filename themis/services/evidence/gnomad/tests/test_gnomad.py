"""Behaviour tests for the gnomad servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping

import grpc
import grpc.aio
import pytest

from themis.clients.auth import session as session_mod
from themis.evidence.models import evidence_pb2
from themis.rpc import auth_pb2, gnomad_pb2, gnomad_pb2_grpc
from themis.services.evidence.gnomad import backend as gnomad_backend
from themis.services.evidence.gnomad import servicer as servicer_mod
from themis.testing import in_process_grpc

_GOOD_TOKEN = (('x-themis-session-token', 'good'),)
_BAD_TOKEN = (('x-themis-session-token', 'bad'),)
_POOL_RECORDS = 500


async def _session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='proj', analysis_id='ana')
    raise session_mod.UnresolvedSessionError


def _backend(
    describe_variant: Mapping[str, gnomad_pb2.DescribeVariantResponse] | None = None,
) -> gnomad_backend.FixtureBackend:
    return gnomad_backend.FixtureBackend({} if describe_variant is None else describe_variant)


@contextlib.asynccontextmanager
async def _serving(backend: gnomad_backend.GnomadBackend) -> AsyncIterator[gnomad_pb2_grpc.GnomadAsyncStub]:
    def register(server: grpc.aio.Server) -> None:
        gnomad_pb2_grpc.add_GnomadServicer_to_server(servicer_mod.Servicer(backend, _session_resolver), server)

    async with in_process_grpc.serving(register) as channel:
        yield gnomad_pb2_grpc.GnomadStub(channel)


def test_seeded_query_returns_the_record() -> None:
    tables = _backend(
        describe_variant={
            '1-100-A-T': gnomad_pb2.DescribeVariantResponse(
                provenance=[evidence_pb2.Provenance(source='gnomAD GraphQL', dataset_versions=('gnomad_r4',))]
            )
        }
    )

    async def run() -> gnomad_pb2.DescribeVariantResponse:
        async with _serving(tables) as stub:
            return await stub.DescribeVariant(
                gnomad_pb2.DescribeVariantRequest(gnomad_id='1-100-A-T', dataset='gnomad_r4'), metadata=_GOOD_TOKEN
            )

    resp = asyncio.run(run())
    assert resp.provenance[0].source == 'gnomAD GraphQL'
    assert resp.provenance[0].dataset_versions == ['gnomad_r4']


@pytest.mark.parametrize(
    'dataset',
    [
        '',  # outside gnomAD's own enum: a 500 there, so unchecked it reads as a fault and is retried
        'gnomad_r5',
        'GRCh38',
        'gnomad_r3',  # inside the enum and answered 200 — refused as policy, not because it fails
    ],
)
def test_gnomad_serves_only_the_two_releases_its_frequencies_are_defined_against(dataset: str) -> None:
    """v4 is the frequency source and v2 the only one with co-occurrence.

    A third release would answer, and would change the allele-frequency denominator under a caller
    reading the result as a v4 FAF.
    """

    async def run() -> gnomad_pb2.DescribeVariantResponse:
        async with _serving(_backend()) as stub:
            return await stub.DescribeVariant(
                gnomad_pb2.DescribeVariantRequest(gnomad_id='17-31232881-G-C', dataset=dataset), metadata=_GOOD_TOKEN
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert 'dataset' in (caught.value.details() or '')
