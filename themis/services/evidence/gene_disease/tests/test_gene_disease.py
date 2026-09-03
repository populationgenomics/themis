"""Behaviour tests for the gene_disease servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from typing import cast, override

import grpc
import grpc.aio
import pytest

from themis.clients.auth import session as session_mod
from themis.evidence.models import evidence_pb2
from themis.rpc import auth_pb2, gene_disease_pb2, gene_disease_pb2_grpc
from themis.services.evidence import errors
from themis.services.evidence.gene_disease import backend as gene_disease_backend
from themis.services.evidence.gene_disease import servicer as servicer_mod
from themis.testing import in_process_grpc

_GOOD_TOKEN = (('x-themis-session-token', 'good'),)
_BAD_TOKEN = (('x-themis-session-token', 'bad'),)
_POOL_RECORDS = 500


async def _session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='proj', analysis_id='ana')
    raise session_mod.UnresolvedSessionError


def _backend(
    describe_gene: Mapping[str, gene_disease_pb2.DescribeGeneResponse] | None = None,
) -> gene_disease_backend.FixtureBackend:
    return gene_disease_backend.FixtureBackend({} if describe_gene is None else describe_gene)


@contextlib.asynccontextmanager
async def _serving(
    backend: gene_disease_backend.GeneDiseaseBackend,
) -> AsyncIterator[gene_disease_pb2_grpc.GeneDiseaseAsyncStub]:
    def register(server: grpc.aio.Server) -> None:
        gene_disease_pb2_grpc.add_GeneDiseaseServicer_to_server(
            servicer_mod.Servicer(backend, _session_resolver), server
        )

    async with in_process_grpc.serving(register) as channel:
        yield gene_disease_pb2_grpc.GeneDiseaseStub(channel)


def test_gene_disease_is_keyed_by_hgnc_id() -> None:
    tables = _backend(
        describe_gene={
            'HGNC:1100': gene_disease_pb2.DescribeGeneResponse(
                entities=[
                    gene_disease_pb2.CuratedEntity(mondo_id='MONDO:0011450', validity_classification='Definitive')
                ]
            )
        }
    )

    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with _serving(tables) as stub:
            return await stub.DescribeGene(
                gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'), metadata=_GOOD_TOKEN
            )

    assert [entity.validity_classification for entity in asyncio.run(run()).entities] == ['Definitive']


@pytest.mark.parametrize(('hgnc_id', 'expected'), [('', 'no hgnc_id'), ('BRCA1', '"HGNC:" and digits')])
def test_gene_disease_requires_the_hgnc_id_the_tables_key_on(hgnc_id: str, expected: str) -> None:
    # The precondition sits above the backends, so the seeded one refuses the request rather than
    # answering "nothing seeded under that key" — a statement about the tables, not about the request.
    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with _serving(_backend(describe_gene={'HGNC:1100': gene_disease_pb2.DescribeGeneResponse()})) as stub:
            return await stub.DescribeGene(gene_disease_pb2.DescribeGeneRequest(hgnc_id=hgnc_id), metadata=_GOOD_TOKEN)

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert expected in str(caught.value.details())


@pytest.mark.parametrize(
    ('unaccepted', 'expected'),
    [
        (
            gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id='familial hypercholesterolemia'),
            'MONDO curie',
        ),
        (gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id='MONDO:007947'), 'MONDO curie'),
        (
            gene_disease_pb2.DescribeGeneRequest(
                hgnc_id='HGNC:1100', inheritance=evidence_pb2.INHERITANCE_AUTOSOMAL_DOMINANT
            ),
            'narrows nothing',
        ),
        # proto3 carries an enum number the schema does not name; unchecked it matches no entity and
        # comes back as a statement about the gene's curations.
        (
            gene_disease_pb2.DescribeGeneRequest(
                hgnc_id='HGNC:1100', mondo_id='MONDO:0011450', inheritance=cast('evidence_pb2.Inheritance', 99)
            ),
            'takes inheritance',
        ),
    ],
)
def test_gene_disease_refuses_an_entity_it_cannot_key_on(
    unaccepted: gene_disease_pb2.DescribeGeneRequest, expected: str
) -> None:
    # A free-text condition or a malformed curie would resolve against no curation and come back as
    # a statement about the gene's entities; an inheritance with no term narrows nothing at all.
    tables = _backend(describe_gene={'HGNC:1100': gene_disease_pb2.DescribeGeneResponse()})

    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with _serving(tables) as stub:
            return await stub.DescribeGene(unaccepted, metadata=_GOOD_TOKEN)

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert expected in str(caught.value.details())


def test_the_gene_disease_fixture_key_names_the_entity_asked_about() -> None:
    # A seed built for the gene alone answers no entity: it carries no `resolution`, which is the
    # gene-scoped answer this rpc exists to stop giving.
    entity = gene_disease_pb2.DescribeGeneRequest(
        hgnc_id='HGNC:1100',
        mondo_id='MONDO:0011450',
        inheritance=evidence_pb2.INHERITANCE_AUTOSOMAL_DOMINANT,
    )
    seeded = gene_disease_pb2.DescribeGeneResponse(
        resolution=gene_disease_pb2.EntityResolution(requested_mondo_id='MONDO:0011450')
    )
    tables = _backend(
        describe_gene={
            'HGNC:1100': gene_disease_pb2.DescribeGeneResponse(),
            'HGNC:1100:MONDO:0011450:INHERITANCE_AUTOSOMAL_DOMINANT': seeded,
        }
    )

    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with _serving(tables) as stub:
            return await stub.DescribeGene(entity, metadata=_GOOD_TOKEN)

    assert asyncio.run(run()).resolution.requested_mondo_id == 'MONDO:0011450'


def test_an_unresolved_entity_is_failed_precondition_not_a_missing_record() -> None:
    # The gene is held and the request is well-formed; what is missing is an entity the caller must
    # restate, which NOT_FOUND ("no record") and INVALID_ARGUMENT ("malformed") both misreport.
    class _Unresolved(gene_disease_backend.FixtureBackend):
        @override
        async def describe_gene(
            self, request: gene_disease_pb2.DescribeGeneRequest
        ) -> gene_disease_pb2.DescribeGeneResponse:
            raise errors.UnresolvedEntityError(f'the gene is curated for other entities than {request.mondo_id}')

    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with _serving(_Unresolved({})) as stub:
            return await stub.DescribeGene(
                gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id='MONDO:0011450'),
                metadata=_GOOD_TOKEN,
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert 'MONDO:0011450' in (caught.value.details() or '')
