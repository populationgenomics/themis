"""Behaviour tests for the vep servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping

import grpc
import grpc.aio
import pytest

from themis.clients.auth import session as session_mod
from themis.evidence.models import evidence_pb2
from themis.rpc import auth_pb2, vep_pb2, vep_pb2_grpc
from themis.services.evidence.vep import backend as vep_backend
from themis.services.evidence.vep import servicer as servicer_mod
from themis.testing import in_process_grpc

_GOOD_TOKEN = (('x-themis-session-token', 'good'),)
_BAD_TOKEN = (('x-themis-session-token', 'bad'),)
_POOL_RECORDS = 500


async def _session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='proj', analysis_id='ana')
    raise session_mod.UnresolvedSessionError


def _backend(annotate: Mapping[str, vep_pb2.AnnotateResponse] | None = None) -> vep_backend.FixtureBackend:
    return vep_backend.FixtureBackend({} if annotate is None else annotate)


@contextlib.asynccontextmanager
async def _serving(backend: vep_backend.VepBackend) -> AsyncIterator[vep_pb2_grpc.VepAsyncStub]:
    def register(server: grpc.aio.Server) -> None:
        vep_pb2_grpc.add_VepServicer_to_server(servicer_mod.Servicer(backend, _session_resolver), server)

    async with in_process_grpc.serving(register) as channel:
        yield vep_pb2_grpc.VepStub(channel)


@pytest.mark.parametrize(
    'variant',
    [
        # The dangerous one: Ensembl answers a bare chromosome name 200 against whichever assembly
        # the queried host serves, so a GRCh37 locus comes back as a different GRCh38 variant.
        '17:g.41209079dup',
        'chr17:g.41209079dup',
        'NC_000017:g.31232881G>C',  # unversioned: NC_000017.10 is GRCh37, .11 is GRCh38
        '17-31232881-G-C',
        'chr17-31232881-G-C',
        'CA398989536',
        'rs1597719704',
        'NF1',
        '',
    ],
)
def test_vep_rejects_a_reference_that_does_not_name_an_assembly(variant: str) -> None:
    """The rpc pins GRCh38, so only the expression itself can say which assembly it is written in."""

    async def run() -> vep_pb2.AnnotateResponse:
        async with _serving(_backend()) as stub:
            return await stub.Annotate(vep_pb2.AnnotateRequest(variant=variant), metadata=_GOOD_TOKEN)

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert 'names its assembly' in (caught.value.details() or '')


def test_an_unbounded_change_never_reaches_the_upstream_url() -> None:
    """The reference is bounded by its accession pattern; the change needs its own bound.

    Without one a well-formed accession carries an arbitrarily long tail into the VEP URL, where it
    costs a round-trip to be refused — the boundary check exists precisely to spend nothing on it.
    """

    async def run() -> vep_pb2.AnnotateResponse:
        async with _serving(_backend()) as stub:
            return await stub.Annotate(
                vep_pb2.AnnotateRequest(variant=f'NM_001042492.3:c.{"A" * 40_000}'), metadata=_GOOD_TOKEN
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.parametrize('filler', ['x' * 40_000, '🧬' * 40_000])
def test_a_rejected_field_comes_back_as_the_rejection_over_the_wire(filler: str) -> None:
    """The clip has to survive the transport, which is where its units are enforced.

    `grpc-message` carries percent-encoded UTF-8, so one 4-byte character costs twelve bytes there.
    A message clipped by characters passes every in-process assertion and is then dropped whole by
    the transport for exceeding its metadata limit — reaching the caller as RESOURCE_EXHAUSTED, with
    the diagnosis gone and no retry, which is worse than the unbounded message it replaced.
    """

    async def run() -> vep_pb2.AnnotateResponse:
        async with _serving(_backend()) as stub:
            return await stub.Annotate(vep_pb2.AnnotateRequest(variant=filler), metadata=_GOOD_TOKEN)

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert 'names its assembly' in (caught.value.details() or '')


@pytest.mark.parametrize(
    'variant',
    [
        'NM_001042492.3:c.3496G>C',
        # A transcript's c./n./p. coordinates are relative to that sequence, not to an assembly, so
        # its version is not what fixes the build and is optional. Ensembl answers this one 200.
        'NM_001042492:c.3496G>C',
        'ENST00000356175:c.3496G>C',  # Ensembl's own namespace — VEP resolves it where ClinVar cannot
        'ENSP00000351015:p.Gly1166Arg',
        'NC_000017.11:g.31232881G>C',
        'LRG_214t1:c.3496G>C',
    ],
)
def test_vep_accepts_an_hgvs_over_any_assembly_naming_reference(variant: str) -> None:
    """The precondition must not be narrower than the upstream: VEP takes more than a RefSeq c. HGVS.

    Every form here is one Ensembl answers 200 (verified live), so refusing any costs a real answer.
    """
    tables = _backend(
        annotate={variant: vep_pb2.AnnotateResponse(most_severe_consequence=evidence_pb2.CONSEQUENCE_MISSENSE)}
    )

    async def run() -> vep_pb2.AnnotateResponse:
        async with _serving(tables) as stub:
            return await stub.Annotate(vep_pb2.AnnotateRequest(variant=variant), metadata=_GOOD_TOKEN)

    assert asyncio.run(run()).most_severe_consequence == evidence_pb2.CONSEQUENCE_MISSENSE


@pytest.mark.parametrize(
    'variant',
    [
        'NM_001042492.3(NF1):c.3496G>C',
        'NM_001042492.3(NF1):c.3496G>C (p.Gly1166Arg)',  # the esummary title `Clinvar` returns verbatim
    ],
)
def test_vep_accepts_the_renderings_clinvar_returns(variant: str) -> None:
    """`ClinvarRecord.hgvs` is the esummary title; chaining `Clinvar` into `Vep` must not need editing."""
    tables = _backend(
        annotate={
            'NM_001042492.3:c.3496G>C': vep_pb2.AnnotateResponse(
                most_severe_consequence=evidence_pb2.CONSEQUENCE_MISSENSE
            )
        }
    )

    async def run() -> vep_pb2.AnnotateResponse:
        async with _serving(tables) as stub:
            return await stub.Annotate(vep_pb2.AnnotateRequest(variant=variant), metadata=_GOOD_TOKEN)

    assert asyncio.run(run()).most_severe_consequence == evidence_pb2.CONSEQUENCE_MISSENSE


@pytest.mark.parametrize('predictor', ['BayesDel_noAF', 'alphamissense', 'VEST4', ''])
def test_vep_rejects_a_predictor_it_has_no_wire_form_for(predictor: str) -> None:
    """Ensembl ignores a flag it does not recognise, so an unlisted name is a 200 with no score.

    The precondition is the servicer's rather than the adapter's alone, so the fixture backend is
    held to it too: a request that only fails against the live upstream is a contract nothing states.
    """
    tables = _backend(annotate={'NM_001042492.3:c.3496G>C': vep_pb2.AnnotateResponse()})

    async def run() -> vep_pb2.AnnotateResponse:
        async with _serving(tables) as stub:
            return await stub.Annotate(
                vep_pb2.AnnotateRequest(variant='NM_001042492.3:c.3496G>C', predictors=['AlphaMissense', predictor]),
                metadata=_GOOD_TOKEN,
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert 'takes predictors from' in (caught.value.details() or '')
