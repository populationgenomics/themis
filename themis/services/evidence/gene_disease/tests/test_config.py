"""The gene_disease interface's env contract and gRPC registration seam."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import pathlib
import re
from collections.abc import AsyncIterator

import grpc
import grpc.aio
import httpx2
import pytest
from google.cloud import storage

from themis.common import authored_status
from themis.rpc import gene_disease_pb2, gene_disease_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.gene_disease import backend as gene_disease_backend
from themis.services.evidence.gene_disease import interface
from themis.services.evidence.tests import authz
from themis.testing import in_process_grpc

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_SEED = json.dumps(
    {'describe_gene': {'HGNC:1100': {'gene_symbol': 'BRCA1', 'entities': [{'disease_label': 'breast-ovarian cancer'}]}}}
)


def _no_network(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f'unexpected network call: {request.url}')


@contextlib.asynccontextmanager
async def _deps_context(http_client: httpx2.AsyncClient | None = None) -> AsyncIterator[deps_mod.Deps]:
    async with contextlib.AsyncExitStack() as stack:
        client = (
            http_client
            if http_client is not None
            else await stack.enter_async_context(httpx2.AsyncClient(transport=httpx2.MockTransport(_no_network)))
        )
        yield deps_mod.Deps(authorizer=authz.authorizer(), http_client=client, stack=stack)


@contextlib.asynccontextmanager
async def _serving(
    http_client: httpx2.AsyncClient | None = None,
) -> AsyncIterator[gene_disease_pb2_grpc.GeneDiseaseAsyncStub]:
    async with _deps_context(http_client) as deps:
        register = functools.partial(interface.register, deps=deps)
        async with authz.gated(register) as channel:
            yield gene_disease_pb2_grpc.GeneDiseaseStub(channel)


def _start_server() -> None:
    async def run() -> None:
        async with _serving():
            pass

    asyncio.run(run())


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_GENE_DISEASE_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_GENE_DISEASE_BACKEND'):
        _start_server()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _start_server()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_GENE_DISEASE_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_GENE_DISEASE_FIXTURE'):
        _start_server()


def test_fixture_selector_serves_seeded_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_GENE_DISEASE_FIXTURE', _SEED)

    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with _serving() as stub:
            return await stub.DescribeGene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100'))

    assert [e.disease_label for e in asyncio.run(run()).entities] == ['breast-ovarian cancer']


def test_fixture_selector_reports_unseeded_keys_as_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_GENE_DISEASE_FIXTURE', _SEED)

    async def run() -> None:
        async with _serving() as stub:
            await stub.DescribeGene(gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1101'))

    with pytest.raises(grpc.aio.AioRpcError) as unseeded:
        asyncio.run(run())
    assert unseeded.value.code() is grpc.StatusCode.NOT_FOUND
    assert authored_status.authored(unseeded.value)
    assert 'seeded' in (unseeded.value.details() or '')


def test_register_denies_an_unadmitted_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_GENE_DISEASE_FIXTURE', _SEED)

    async def run() -> None:
        async with (
            _deps_context() as deps,
            in_process_grpc.serving(
                functools.partial(interface.register, deps=deps),
                interceptors=(authz.InjectMetadata(authz.WEB),),
                server_interceptors=authz.server_interceptors(),
            ) as channel,
        ):
            await gene_disease_pb2_grpc.GeneDiseaseStub(channel).DescribeGene(
                gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100')
            )

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED


def test_live_backend_requires_the_resources_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset, the adapter would load no dump and answer 'not curated' for every gene."""
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'live')
    monkeypatch.delenv('THEMIS_RESOURCES_BUCKET', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_RESOURCES_BUCKET'):
        _start_server()


def test_live_selector_serves_over_the_named_bucket_and_shared_client(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
) -> None:
    """Registering loads the reference objects from the GCS bucket and resolves MONDO terms over http_client."""
    assert gcs_bucket.name is not None
    monkeypatch.setenv('THEMIS_GENE_DISEASE_BACKEND', 'live')
    monkeypatch.setenv('THEMIS_RESOURCES_BUCKET', gcs_bucket.name)
    monkeypatch.setenv('STORAGE_EMULATOR_HOST', gcs_endpoint)
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'themis-test')
    objects = {
        gene_disease_backend._GENCC_OBJECT: (_FIXTURES / 'gencc.tsv').read_bytes(),
        gene_disease_backend._VALIDITY_OBJECT: (_FIXTURES / 'clingen_validity.csv').read_bytes(),
        gene_disease_backend._DOSAGE_OBJECT: (_FIXTURES / 'clingen_dosage.csv').read_bytes(),
        gene_disease_backend._PANELAPP_OBJECT: (_FIXTURES / 'panelapp.json').read_bytes(),
        gene_disease_backend._HGNC_OBJECT: (_FIXTURES / 'hgnc_complete_set.tsv').read_bytes(),
    }
    for name, content in objects.items():
        gcs_bucket.blob(name).upload_from_string(content)

    def ols4_handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == '/ols4/api/ontologies/mondo':
            return httpx2.Response(200, json={'config': {'version': '2026-07-01'}})
        match = re.search(r'MONDO_\d+', str(request.url))
        assert match is not None, f'unexpected OLS4 request: {request.url}'
        ancestor = 'MONDO:0000100' if match.group(0) == 'MONDO_0054748' else 'MONDO:0000001'
        return httpx2.Response(
            200,
            json={
                '_embedded': {'terms': [{'obo_id': ancestor, 'label': f'label of {ancestor}'}]},
                'page': {'totalPages': 1, 'totalElements': 1},
            },
        )

    async def run() -> gene_disease_pb2.DescribeGeneResponse:
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(ols4_handler)) as http_client,
            _serving(http_client) as stub,
        ):
            return await stub.DescribeGene(
                gene_disease_pb2.DescribeGeneRequest(hgnc_id='HGNC:1100', mondo_id='MONDO:0000100')
            )

    reply = asyncio.run(run())
    assert reply.gene_symbol == 'BRCA1'
    assert {entity.source for entity in reply.entities} == {'ClinGen Gene Validity', 'GenCC'}
    assert reply.resolution.relation == gene_disease_pb2.TERM_RELATION_DESCENDANT
    assert reply.resolution.mondo_id == 'MONDO:0054748'
    assert 'EBI OLS4 (MONDO)' in {p.source for p in reply.provenance}
