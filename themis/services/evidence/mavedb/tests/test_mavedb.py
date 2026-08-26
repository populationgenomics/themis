"""Behaviour tests for the mavedb servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping

import grpc
import grpc.aio
import pytest

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, mavedb_pb2, mavedb_pb2_grpc
from themis.services.evidence.mavedb import backend as mavedb_backend
from themis.services.evidence.mavedb import servicer as servicer_mod
from themis.testing import in_process_grpc

_GOOD_TOKEN = (('x-themis-session-token', 'good'),)
_BAD_TOKEN = (('x-themis-session-token', 'bad'),)
_POOL_RECORDS = 500


async def _session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='proj', analysis_id='ana')
    raise session_mod.UnresolvedSessionError


def _backend(
    describe_variant: Mapping[str, mavedb_pb2.DescribeVariantResponse] | None = None,
) -> mavedb_backend.FixtureBackend:
    return mavedb_backend.FixtureBackend({} if describe_variant is None else describe_variant)


@contextlib.asynccontextmanager
async def _serving(backend: mavedb_backend.MaveDbBackend) -> AsyncIterator[mavedb_pb2_grpc.MaveDbAsyncStub]:
    def register(server: grpc.aio.Server) -> None:
        mavedb_pb2_grpc.add_MaveDbServicer_to_server(servicer_mod.Servicer(backend, _session_resolver), server)

    async with in_process_grpc.serving(register) as channel:
        yield mavedb_pb2_grpc.MaveDbStub(channel)


async def _mavedb(backend: mavedb_backend.MaveDbBackend, variant: str) -> mavedb_pb2.DescribeVariantResponse:
    async with _serving(backend) as stub:
        return await stub.DescribeVariant(mavedb_pb2.DescribeVariantRequest(variant=variant), metadata=_GOOD_TOKEN)


@pytest.mark.parametrize(
    'variant',
    [
        'p.Thr1677His',  # a bare change: no accession, so it names no allele to register
        'ENSP00000269305.4:p.Arg175His',  # the registry registers no id for an Ensembl protein
        'NM_000546:c.524G>A',  # unversioned — the coding acceptor takes the versioned RefSeq form only
        'CA000251',  # an id, not the expression that registers one
        'rs1597719704',
        '17-31232881-G-C',
        '',
    ],
)
def test_mavedb_rejects_a_variant_that_registers_no_clingen_allele(variant: str) -> None:
    """MaveDB is keyed on ClinGen alleles, and an absence from it is scored.

    Unchecked, a variant naming no allele reaches the lookup as a question about nothing and comes
    back "no assay covers this variant" — a caller's mistake promoted to a *_FXN finding.
    """
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(_mavedb(_backend(), variant))
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    ('sent', 'accepted'),
    [
        ('NP_000537.3:p.(Arg175His)', 'NP_000537.3:p.Arg175His'),
        ('NP_000537.3:p.Arg175His', 'NP_000537.3:p.Arg175His'),
        ('NP_000537.3(TP53):p.(Arg175His)', 'NP_000537.3:p.Arg175His'),
        ('NM_000546.6(TP53):c.524G>A', 'NM_000546.6:c.524G>A'),
    ],
)
def test_mavedb_accepts_both_renderings_of_one_protein_allele(sent: str, accepted: str) -> None:
    """`Resolve` returns the predicted rendering; the sources are keyed on the bare one.

    They name the same allele, so both have to answer alike — a lookup keyed on the literal string
    would make the parentheses the difference between a score and "no assay covers this variant".
    """
    seeded = mavedb_pb2.DescribeVariantResponse(acmg_criterion='BS3')
    resp = asyncio.run(_mavedb(_backend(describe_variant={accepted: seeded}), sent))
    assert resp.acmg_criterion == 'BS3'
