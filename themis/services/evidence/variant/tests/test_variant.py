"""Behaviour tests for the variant servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping

import grpc
import grpc.aio
import pytest

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, variant_pb2, variant_pb2_grpc
from themis.services.evidence.variant import backend as variant_backend
from themis.services.evidence.variant import servicer as servicer_mod
from themis.testing import in_process_grpc

_GOOD_TOKEN = (('x-themis-session-token', 'good'),)
_BAD_TOKEN = (('x-themis-session-token', 'bad'),)
_POOL_RECORDS = 500


async def _session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='proj', analysis_id='ana')
    raise session_mod.UnresolvedSessionError


def _backend(normalize: Mapping[str, variant_pb2.NormalizeResponse] | None = None) -> variant_backend.FixtureBackend:
    return variant_backend.FixtureBackend({} if normalize is None else normalize)


@contextlib.asynccontextmanager
async def _serving(backend: variant_backend.VariantBackend) -> AsyncIterator[variant_pb2_grpc.VariantAsyncStub]:
    def register(server: grpc.aio.Server) -> None:
        variant_pb2_grpc.add_VariantServicer_to_server(servicer_mod.Servicer(backend, _session_resolver), server)

    async with in_process_grpc.serving(register) as channel:
        yield variant_pb2_grpc.VariantStub(channel)


def test_resolve_is_keyed_by_variant() -> None:
    tables = _backend(normalize={'NM_000546.6:c.524G>A': variant_pb2.NormalizeResponse(caid='CA123')})

    async def run() -> variant_pb2.NormalizeResponse:
        async with _serving(tables) as stub:
            return await stub.Normalize(
                variant_pb2.NormalizeRequest(variant='NM_000546.6:c.524G>A', genome_build='GRCh38'),
                metadata=_GOOD_TOKEN,
            )

    assert asyncio.run(run()).caid == 'CA123'


@pytest.mark.parametrize(
    'variant',
    [
        'ENST00000269305.9:c.524G>A',  # Ensembl accession — VariantValidator holds no RefSeq record for it
        'CA000251',
        'rs28934578',
        '17-7675088-C-T',
        'NC_000017.11:g.7675088C>T',
        'NP_000537.3:p.Arg175His',
        'NM_000546:c.524G>A',  # unversioned — the projection a claim rests on would not be reproducible
        # HGVS qualifies a GENOMIC reference with the transcript it is read through; a transcript
        # qualified by a second transcript is not an expression any upstream here resolves.
        'NM_000546.6(NM_001126112.3):c.524G>A',
        '',
    ],
)
def test_resolve_rejects_a_variant_form_it_cannot_canonicalise(variant: str) -> None:
    """The precondition holds at the rpc boundary, so it binds every backend, not just the live one."""

    async def run() -> variant_pb2.NormalizeResponse:
        async with _serving(_backend()) as stub:
            return await stub.Normalize(
                variant_pb2.NormalizeRequest(variant=variant, genome_build='GRCh38'), metadata=_GOOD_TOKEN
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert 'RefSeq transcript HGVS' in (caught.value.details() or '')


@pytest.mark.parametrize(
    'variant',
    [
        'NM_000546.6(TP53):c.524G>A',
        'NM_000546.6(TP53):c.524G>A (p.Arg175His)',  # the esummary title `Clinvar` returns verbatim
    ],
)
def test_resolve_accepts_the_renderings_clinvar_returns(variant: str) -> None:
    """`ClinvarRecord.hgvs` is the esummary title; chaining it into `Resolve` must not need hand-editing."""
    tables = _backend(normalize={'NM_000546.6:c.524G>A': variant_pb2.NormalizeResponse(caid='CA123')})

    async def run() -> variant_pb2.NormalizeResponse:
        async with _serving(tables) as stub:
            return await stub.Normalize(
                variant_pb2.NormalizeRequest(variant=variant, genome_build='GRCh38'), metadata=_GOOD_TOKEN
            )

    assert asyncio.run(run()).caid == 'CA123'


@pytest.mark.parametrize('genome_build', ['', 'GRCh39', 'hg38'])
def test_resolve_rejects_a_genome_build_the_upstreams_do_not_serve(genome_build: str) -> None:
    """Rejected at the boundary: reaching VEP's host check first costs two live round-trips."""

    async def run() -> variant_pb2.NormalizeResponse:
        async with _serving(_backend()) as stub:
            return await stub.Normalize(
                variant_pb2.NormalizeRequest(variant='NM_000546.6:c.524G>A', genome_build=genome_build),
                metadata=_GOOD_TOKEN,
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert 'genome_build' in (caught.value.details() or '')
