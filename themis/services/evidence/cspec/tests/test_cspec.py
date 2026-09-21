"""Behaviour tests for the cspec servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping

import grpc
import grpc.aio
import pytest

from themis.rpc import cspec_pb2, cspec_pb2_grpc
from themis.services.evidence.cspec import backend as cspec_backend
from themis.services.evidence.cspec import servicer as servicer_mod
from themis.services.evidence.tests import authz


def _backend(
    list_specifications: Mapping[str, cspec_pb2.ListSpecificationsResponse] | None = None,
) -> cspec_backend.FixtureBackend:
    return cspec_backend.FixtureBackend({} if list_specifications is None else list_specifications)


@contextlib.asynccontextmanager
async def _serving(backend: cspec_backend.CspecBackend) -> AsyncIterator[cspec_pb2_grpc.CspecAsyncStub]:
    def register(server: grpc.aio.Server) -> None:
        cspec_pb2_grpc.add_CspecServicer_to_server(servicer_mod.Servicer(backend), server)

    async with authz.gated(register) as channel:
        yield cspec_pb2_grpc.CspecStub(channel)


def test_criteria_specification_is_keyed_by_gene() -> None:
    tables = _backend(
        list_specifications={
            'ACTC1': cspec_pb2.ListSpecificationsResponse(
                coverage=cspec_pb2.SPECIFICATION_COVERAGE_SPECIFIED,
                specifications=[
                    cspec_pb2.VcepSpecification(id='GN101', status=cspec_pb2.SPECIFICATION_STATUS_IN_FORCE)
                ],
            )
        }
    )

    async def run() -> cspec_pb2.ListSpecificationsResponse:
        async with _serving(tables) as stub:
            return await stub.ListSpecifications(cspec_pb2.ListSpecificationsRequest(gene='ACTC1'))

    assert [specification.id for specification in asyncio.run(run()).specifications] == ['GN101']


@pytest.mark.parametrize('gene', ['', '   '])
def test_criteria_specification_rejects_an_absent_gene(gene: str) -> None:
    # The gene is the whole key; an empty one would reach the registry as a lookup of no entity.
    tables = _backend(list_specifications={'': cspec_pb2.ListSpecificationsResponse()})

    async def run() -> cspec_pb2.ListSpecificationsResponse:
        async with _serving(tables) as stub:
            return await stub.ListSpecifications(cspec_pb2.ListSpecificationsRequest(gene=gene))

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_the_criteria_specification_fixture_refuses_an_unseeded_gene() -> None:
    # The fixture never manufactures an empty response: an unseeded gene would otherwise come back as
    # SPECIFICATION_COVERAGE_UNSPECIFIED with no specification, which reads as "no panel specified it".
    tables = _backend(list_specifications={'ACTC1': cspec_pb2.ListSpecificationsResponse()})

    async def run() -> cspec_pb2.ListSpecificationsResponse:
        async with _serving(tables) as stub:
            return await stub.ListSpecifications(cspec_pb2.ListSpecificationsRequest(gene='MYH7'))

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() is grpc.StatusCode.NOT_FOUND
