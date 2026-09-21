"""What `serving.EvidenceServicer` gives every interface that mixes it in: the deadline and the status mapping.

Driven over gnomad's servicer on a real, gated server, and gnomad stands for all of them: the base class
is what is under test, and every database-backed interface takes the whole of it. Admission is the
interceptor's and is pinned in `themis.clients.auth.tests`; nothing here asserts on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator
from typing import override

import grpc
import grpc.aio
import pytest

from themis.rpc import gnomad_pb2, gnomad_pb2_grpc
from themis.services.evidence import errors, serving
from themis.services.evidence.gnomad import backend as gnomad_backend
from themis.services.evidence.gnomad import servicer as servicer_mod
from themis.services.evidence.tests import authz


class _RaisingBackend(gnomad_backend.GnomadBackend):
    """Fails its one call with `error`."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    @override
    async def describe_variant(self, request: gnomad_pb2.DescribeVariantRequest) -> gnomad_pb2.DescribeVariantResponse:
        raise self._error


class _StalledBackend(gnomad_backend.GnomadBackend):
    """Never answers, and records the cancellation the rpc's deadline delivers to it."""

    def __init__(self) -> None:
        self.cancelled = False

    @override
    async def describe_variant(self, request: gnomad_pb2.DescribeVariantRequest) -> gnomad_pb2.DescribeVariantResponse:
        stalled = asyncio.Event()  # nothing sets it
        try:
            while True:
                await stalled.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@contextlib.asynccontextmanager
async def _serving(backend: gnomad_backend.GnomadBackend) -> AsyncIterator[gnomad_pb2_grpc.GnomadAsyncStub]:
    def register(server: grpc.aio.Server) -> None:
        gnomad_pb2_grpc.add_GnomadServicer_to_server(servicer_mod.Servicer(backend), server)

    async with authz.gated(register) as channel:
        yield gnomad_pb2_grpc.GnomadStub(channel)


def _request() -> gnomad_pb2.DescribeVariantRequest:
    """A request the servicer's own preconditions accept, so every failure here is the base class's."""
    return gnomad_pb2.DescribeVariantRequest(gnomad_id='1-100-A-T', dataset='gnomad_r4')


def _refused(backend: gnomad_backend.GnomadBackend) -> grpc.aio.AioRpcError:
    """Drive one `DescribeVariant` that is expected to fail, and hand back the status it failed with."""

    async def run() -> grpc.aio.AioRpcError:
        async with _serving(backend) as stub:
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await stub.DescribeVariant(_request())
            return caught.value

    return asyncio.run(run())


# Each taxonomy error, with the status it has to reach a caller under. Two share
# FAILED_PRECONDITION: a request the sources cannot settle and one they settle inconsistently are
# both well-formed questions whose answer no reissue changes.
_TAXONOMY: list[tuple[Exception, grpc.StatusCode]] = [
    (errors.UnknownVariantError('gnomAD holds no record of 1-100-A-T'), grpc.StatusCode.NOT_FOUND),
    (errors.InvalidRequestError('gnomAD rejected 1-100-A-T (400)'), grpc.StatusCode.INVALID_ARGUMENT),
    (errors.UnresolvedEntityError('two curated entities sit under MONDO:0007254'), grpc.StatusCode.FAILED_PRECONDITION),
    (
        errors.InconsistentSourcesError("ClinVar holds no record under accession 'VCV000704508'"),
        grpc.StatusCode.FAILED_PRECONDITION,
    ),
]


@pytest.mark.parametrize(('error', 'code'), _TAXONOMY, ids=[type(error).__name__ for error, _ in _TAXONOMY])
def test_a_taxonomy_error_reaches_the_caller_as_its_own_status(error: Exception, code: grpc.StatusCode) -> None:
    """Each `errors` type carries a distinct meaning, and the status is what carries it over the wire.

    NOT_FOUND most of all: absence from gnomAD is the POP_FRQ rarity finding and absence from MaveDB
    is "no assay exists", so answering either with UNKNOWN reads as an outage and is retried against a
    question the source has already settled.
    """
    failure = _refused(_RaisingBackend(error))
    assert failure.code() == code
    assert str(error) in (failure.details() or '')


def test_every_taxonomy_error_is_mapped_to_a_status() -> None:
    """Listing the mapped types proves nothing on its own — nothing forces a new one into the list.

    So the set is derived from `errors` instead: a type added there and not mapped here surfaces as
    UNKNOWN, which a caller's retry helper reissues four times against an answer that cannot change.
    """
    defined = {
        value
        for name, value in vars(errors).items()
        if not name.startswith('_') and inspect.isclass(value) and issubclass(value, Exception)
    }
    assert defined == {type(error) for error, _ in _TAXONOMY}


def test_a_backend_that_never_answers_ends_as_this_rpcs_own_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The overrun is this service's own status naming the rpc, and the work behind it is dropped.

    A caller that has given up is not owed the instance the composition still holds, so the backend
    has to see its cancellation rather than run on to completion.
    """
    monkeypatch.setattr(serving, '_RPC_DEADLINE_S', 0.05)
    backend = _StalledBackend()

    async def run() -> tuple[grpc.aio.AioRpcError, bool]:
        async with _serving(backend) as stub:
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await stub.DescribeVariant(_request())
            # Read while the loop still runs: `asyncio.run` cancels whatever is left at teardown, so a
            # flag read after it cannot tell a dropped backend from an abandoned one.
            return caught.value, backend.cancelled

    failure, cancelled = asyncio.run(run())
    assert failure.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    assert 'DescribeVariant' in (failure.details() or '')
    assert cancelled
