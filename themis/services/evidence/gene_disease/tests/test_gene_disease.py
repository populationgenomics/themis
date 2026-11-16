"""Behaviour tests for the gene_disease servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import override

import grpc
import grpc.aio
import httpx2
import pytest

from themis.rpc import gene_disease_pb2, gene_disease_pb2_grpc
from themis.services.evidence import errors
from themis.services.evidence.gene_disease import backend as gene_disease_backend
from themis.services.evidence.gene_disease import servicer as servicer_mod
from themis.services.evidence.tests import authz
from themis.services.evidence.upstreams import clingen_dosage, clingen_validity, gencc, hgnc, panelapp


def _backend(
    describe_gene: Mapping[str, gene_disease_pb2.DescribeGeneResponse] | None = None,
) -> gene_disease_backend.FixtureBackend:
    return gene_disease_backend.FixtureBackend({} if describe_gene is None else describe_gene)


_FIXTURE_VAR = 'THEMIS_GENE_DISEASE_FIXTURE'
_HGNC_RELEASE = '2026-01-01'


def _seeded_with_one_gene(_client: httpx2.AsyncClient) -> gene_disease_backend.GeneDiseaseBackend:
    """The fixture adapter as an operator seeds it, through the seed document's own checks."""
    seed = {'describe_gene': {'HGNC:1100': {'gene_symbol': 'SEEDED1', 'coverage': 'GENE_COVERAGE_ABSENT'}}}
    return gene_disease_backend.fixture_backend_from_json(json.dumps(seed), var_name=_FIXTURE_VAR)


def _live_with_one_gene(client: httpx2.AsyncClient) -> gene_disease_backend.GeneDiseaseBackend:
    """The live adapter over an HGNC set naming one gene, which no reference table curates."""
    tables = gene_disease_backend.ReferenceTables(
        validity=clingen_validity.ClinGenValidity({}, ()),
        dosage=clingen_dosage.ClinGenDosage({}, ()),
        gencc=gencc.GenCC({}, ()),
        panelapp=panelapp.PanelAppTable({}, '', ()),
        hgnc=hgnc.HgncCompleteSet({'HGNC:1100': hgnc.ApprovedGene('APPROVED1', ())}, _HGNC_RELEASE),
    )
    return gene_disease_backend.LiveBackend(client, tables)


@contextlib.asynccontextmanager
async def _serving(
    backend: gene_disease_backend.GeneDiseaseBackend,
) -> AsyncIterator[gene_disease_pb2_grpc.GeneDiseaseAsyncStub]:
    def register(server: grpc.aio.Server) -> None:
        gene_disease_pb2_grpc.add_GeneDiseaseServicer_to_server(servicer_mod.Servicer(backend), server)

    async with authz.gated(register) as channel:
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
            return await stub.DescribeGene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'))

    assert [entity.validity_classification for entity in asyncio.run(run()).entities] == ['Definitive']


@pytest.mark.parametrize(
    'seeded', [{'coverage': 'GENE_COVERAGE_ABSENT'}, {'gene_symbol': ' '}], ids=['no_symbol', 'blank_symbol']
)
def test_a_fixture_seed_that_names_no_gene_is_rejected(seeded: dict[str, str]) -> None:
    # The contract never answers with an empty gene_symbol, and the seed is the fixture's only source of one.
    seed = {'describe_gene': {'HGNC:1100': seeded}}
    with pytest.raises(
        SystemExit, match=f"{_FIXTURE_VAR} section 'describe_gene' entry 'HGNC:1100' has no gene_symbol"
    ):
        gene_disease_backend.fixture_backend_from_json(json.dumps(seed), var_name=_FIXTURE_VAR)


@dataclasses.dataclass(frozen=True)
class _Answer:
    """What one backend answers an id with: the status, and the symbol or the status text."""

    code: grpc.StatusCode
    gene_symbol: str
    details: str


def _answer(
    backend_over: Callable[[httpx2.AsyncClient], gene_disease_backend.GeneDiseaseBackend], hgnc_id: str
) -> _Answer:
    async def describe() -> _Answer:
        async with httpx2.AsyncClient() as client, _serving(backend_over(client)) as stub:
            try:
                response = await stub.DescribeGene(gene_disease_pb2.DescribeGeneRequest(hgnc_id=hgnc_id))
            except grpc.aio.AioRpcError as e:
                return _Answer(e.code(), '', e.details() or '')
            return _Answer(grpc.StatusCode.OK, response.gene_symbol, '')

    return asyncio.run(describe())


def test_the_fixture_and_live_backends_answer_a_held_and_an_unknown_id_alike() -> None:
    # A caller handles an id the same way offline and deployed: a held one is answered under a
    # symbol, an unknown one fails with one status.
    backends = {'fixture': _seeded_with_one_gene, 'live': _live_with_one_gene}
    held = {name: _answer(backend_over, 'HGNC:1100') for name, backend_over in backends.items()}
    unknown = {name: _answer(backend_over, 'HGNC:404040') for name, backend_over in backends.items()}

    # Each backend names the gene from its own source, so the failure below is the id's and not the backend's.
    assert held == {
        'fixture': _Answer(grpc.StatusCode.OK, 'SEEDED1', ''),
        'live': _Answer(grpc.StatusCode.OK, 'APPROVED1', ''),
    }
    assert unknown['fixture'].code == unknown['live'].code == grpc.StatusCode.NOT_FOUND
    assert all('HGNC:404040' in answer.details for answer in unknown.values())
    # The live status text states the release the id is missing from, which a later release may hold.
    assert f'release {_HGNC_RELEASE} of the HGNC complete set' in unknown['live'].details


@pytest.mark.parametrize(('hgnc_id', 'expected'), [('', 'no hgnc_id'), ('BRCA1', '"HGNC:" and digits')])
def test_gene_disease_requires_the_hgnc_id_the_tables_key_on(hgnc_id: str, expected: str) -> None:
    # The precondition sits above the backends, so the seeded one refuses the request rather than
    # answering "nothing seeded under that key" — a statement about the tables, not about the request.
    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with _serving(_backend(describe_gene={'HGNC:1100': gene_disease_pb2.DescribeGeneResponse()})) as stub:
            return await stub.DescribeGene(gene_disease_pb2.DescribeGeneRequest(hgnc_id=hgnc_id))

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
    ],
)
def test_gene_disease_refuses_a_term_it_cannot_key_on(
    unaccepted: gene_disease_pb2.DescribeGeneRequest, expected: str
) -> None:
    # A free-text condition or a malformed curie would resolve against no curation and come back as
    # a statement about the gene's records.
    tables = _backend(describe_gene={'HGNC:1100': gene_disease_pb2.DescribeGeneResponse()})

    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with _serving(tables) as stub:
            return await stub.DescribeGene(unaccepted)

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert expected in str(caught.value.details())


def test_the_gene_disease_fixture_key_names_the_term_asked_about() -> None:
    # A seed built for the gene alone answers no term: it carries no `resolution`, which is the
    # gene-scoped answer this rpc exists to stop giving.
    entity = gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id='MONDO:0011450')
    seeded = gene_disease_pb2.DescribeGeneResponse(
        resolution=gene_disease_pb2.EntityResolution(requested_mondo_id='MONDO:0011450')
    )
    tables = _backend(
        describe_gene={
            'HGNC:1100': gene_disease_pb2.DescribeGeneResponse(),
            'HGNC:1100:MONDO:0011450': seeded,
        }
    )

    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with _serving(tables) as stub:
            return await stub.DescribeGene(entity)

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
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert 'MONDO:0011450' in (caught.value.details() or '')
