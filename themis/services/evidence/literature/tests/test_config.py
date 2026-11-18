"""The literature interface's env contract and gRPC registration seam."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import functools
import hashlib
import json
import pathlib
from collections.abc import AsyncIterator, Callable
from typing import override

import grpc
import grpc.aio
import httpx2
import pg8000.dbapi
import pytest
from google.cloud import storage, tasks_v2
from google.cloud.tasks_v2.services.cloud_tasks.transports import grpc as tasks_grpc
from google.protobuf import timestamp_pb2

from themis.common import authored_status
from themis.litcache import crosswalk, metadata_fields, sourced_metadata
from themis.litcache.models import litcache_pb2, work_pb2
from themis.rpc import literature_pb2, literature_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.literature import config, interface
from themis.services.evidence.tests import authz
from themis.services.evidence.upstreams import destinations
from themis.testing import in_process_grpc

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_EUROPEPMC_BATCH = json.loads((_FIXTURES / 'europepmc_batch_records.json').read_text('utf-8'))

_OPENALEX_API_KEY_VAR = 'THEMIS_LITERATURE_OPENALEX_API_KEY'
_OPENALEX_API_KEY = 'an-openalex-key'
_CROSSWALK_VARS = (
    'THEMIS_LITERATURE_CROSSWALK_INSTANCE',
    'THEMIS_LITERATURE_CROSSWALK_DATABASE',
    'THEMIS_LITERATURE_CROSSWALK_DB_USER',
)
_CROSSWALK_ENV = dict(zip(_CROSSWALK_VARS, ('p:r:i', 'themis', 'themis-evidence@p.iam'), strict=True))
_CONVERT_VARS = (
    'THEMIS_LITERATURE_CONVERT_QUEUE',
    'THEMIS_LITERATURE_CONVERT_WORKER_URL',
    'THEMIS_LITERATURE_CONVERT_INVOKER_SA',
)
_CONVERT_ENV = dict(
    zip(
        _CONVERT_VARS,
        (
            'projects/p/locations/r/queues/themis-convert',
            'https://themis-convert-worker-abc-ts.a.run.app',
            'themis-convert-invoker@p.iam.gserviceaccount.com',
        ),
        strict=True,
    )
)

_EMPTY_SEED = json.dumps({'store': {}, 'discovery': {'records': [], 'entities': [], 'book_articles': []}})
_CAPTURED = dt.datetime(2026, 6, 29, tzinfo=dt.UTC)
_MARKDOWN = '# A live paper\n\nBody text.\n'


def _no_network(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f'unexpected network call: {request.url}')


def _track_storage_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[storage.Client], list[storage.Client]]:
    created: list[storage.Client] = []
    closed: list[storage.Client] = []

    class _TrackedStorageClient(storage.Client):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

        @override
        def close(self) -> None:
            closed.append(self)
            super().close()

    monkeypatch.setattr(config.storage, 'Client', _TrackedStorageClient)
    return created, closed


@contextlib.asynccontextmanager
async def _serving(
    *,
    handler: Callable[[httpx2.Request], httpx2.Response] = _no_network,
    stack: contextlib.AsyncExitStack | None = None,
) -> AsyncIterator[literature_pb2_grpc.LiteratureAsyncStub]:
    async with contextlib.AsyncExitStack() as owned:
        active_stack = stack if stack is not None else owned
        http_client = await owned.enter_async_context(
            destinations.admitting_client(timeout=httpx2.Timeout(10.0), transport=httpx2.MockTransport(handler))
        )
        deps = deps_mod.Deps(authorizer=authz.authorizer(), http_client=http_client, stack=active_stack)
        async with in_process_grpc.serving(
            functools.partial(interface.register, deps=deps),
            interceptors=(authz.InjectMetadata(authz.ADMITTED),),
            server_interceptors=authz.server_interceptors(),
        ) as channel:
            yield literature_pb2_grpc.LiteratureStub(channel)


def _start_server(*, stack: contextlib.AsyncExitStack | None = None) -> None:
    async def run() -> None:
        async with _serving(stack=stack):
            pass

    asyncio.run(run())


def _seed_paper(bucket: storage.Bucket, doc_id: str, *, ready: bool) -> None:
    stamp = timestamp_pb2.Timestamp()
    stamp.FromDatetime(_CAPTURED)
    xml_source = litcache_pb2.Source(
        handle='xml',
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_XML,
        licence='https://creativecommons.org/licenses/by/4.0/',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT,
        access=litcache_pb2.Access(free_to_read=litcache_pb2.FreeToRead()),
        revisions=[
            litcache_pb2.Revision(
                hash='xmlrev',
                kind=litcache_pb2.SourceKind.SOURCE_KIND_PMC_OA_S3,
                captured_at=stamp,
            )
        ],
    )
    manifest = litcache_pb2.Manifest(
        doc_id=doc_id,
        external_ids=litcache_pb2.ExternalIds(doi='10.1/x'),
        sources=[xml_source],
    )
    if ready:
        rendering_hash = hashlib.sha256(_MARKDOWN.encode('utf-8')).hexdigest()
        manifest.records[rendering_hash].CopyFrom(
            litcache_pb2.Record(
                deps=['xmlrev'],
                created_at=stamp,
                rendering=litcache_pb2.RenderingConstruction(
                    converter=litcache_pb2.Converter.CONVERTER_LITDOWN,
                    converter_version='0.4.0',
                ),
            )
        )
        manifest.refs['markdown'] = rendering_hash
        bucket.blob(f'papers/{doc_id}/renderings/{rendering_hash}.md').upload_from_string(_MARKDOWN)
    source = litcache_pb2.MetadataSource()
    source.pubmed.article.medline_citation.article.article_title.value = 'A live paper'
    sourced = litcache_pb2.SourcedMetadata(metadata=metadata_fields.fill(source), source=source)
    bucket.blob(f'papers/{doc_id}/manifest.pb').upload_from_string(manifest.SerializeToString())
    bucket.blob(f'papers/{doc_id}/metadata.pb').upload_from_string(sourced_metadata.to_canonical_bytes(sourced))


def _live_env(monkeypatch: pytest.MonkeyPatch, *, bucket_name: str, gcs_endpoint: str) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'live')
    monkeypatch.setenv('THEMIS_FULLTEXT_BUCKET', bucket_name)
    monkeypatch.setenv(_OPENALEX_API_KEY_VAR, _OPENALEX_API_KEY)
    monkeypatch.setenv('STORAGE_EMULATOR_HOST', gcs_endpoint)
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'themis-test')
    for var in (*_CROSSWALK_VARS, *_CONVERT_VARS):
        monkeypatch.delenv(var, raising=False)


def _wire_crosswalk(
    monkeypatch: pytest.MonkeyPatch,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
) -> tuple[list[tuple[str, str, str]], list[bool]]:
    dialled: list[tuple[str, str, str]] = []
    closed: list[bool] = []

    class _Dialer:
        def close(self) -> None:
            closed.append(True)

    dialer = _Dialer()

    def dial_sql(pool: object, *, connection_name: str, database: str, db_user: str) -> pg8000.dbapi.Connection:
        assert pool is dialer
        dialled.append((connection_name, database, db_user))
        return crosswalk_connect()

    monkeypatch.setattr(config.sql, 'lazy_connector', lambda: dialer)
    monkeypatch.setattr(config.sql, 'iam_connect', dial_sql)
    return dialled, closed


def _fixture_env(monkeypatch: pytest.MonkeyPatch, seed: str = _EMPTY_SEED) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_LITERATURE_FIXTURE', seed)


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_LITERATURE_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_LITERATURE_BACKEND'):
        _start_server()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _start_server()


def test_live_backend_requires_the_fulltext_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'live')
    monkeypatch.delenv('THEMIS_FULLTEXT_BUCKET', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_FULLTEXT_BUCKET'):
        _start_server()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_LITERATURE_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_LITERATURE_FIXTURE'):
        _start_server()


@pytest.mark.parametrize('section', [pytest.param('store', id='store'), pytest.param('discovery', id='discovery')])
def test_every_section_of_the_seed_is_required(monkeypatch: pytest.MonkeyPatch, section: str) -> None:
    document = {'store': {}, 'discovery': {'records': [], 'entities': [], 'book_articles': []}}
    del document[section]
    _fixture_env(monkeypatch, json.dumps(document))
    with pytest.raises(SystemExit, match=section):
        _start_server()


@pytest.mark.parametrize(
    'seed',
    [
        pytest.param('not json', id='malformed'),
        pytest.param(json.dumps([]), id='not-an-object'),
        pytest.param(json.dumps({'store': {}, 'discovery': {}, 'papers': []}), id='unknown-section'),
        pytest.param(json.dumps({'store': {'d': {}}, 'discovery': {}}), id='paper-without-a-title'),
        pytest.param(
            json.dumps(
                {'store': {}, 'discovery': {'records': [{'pmid': 'PMC1'}], 'entities': [], 'book_articles': []}}
            ),
            id='pmid-that-is-not-one',
        ),
    ],
)
def test_a_seed_the_backend_cannot_stand_up_on_exits(monkeypatch: pytest.MonkeyPatch, seed: str) -> None:
    _fixture_env(monkeypatch, seed)
    with pytest.raises(SystemExit, match='THEMIS_LITERATURE_FIXTURE'):
        _start_server()


def test_the_fixture_selector_seeds_both_halves_of_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture_env(
        monkeypatch,
        json.dumps(
            {
                'store': {'doc-1': {'title': 'A paper', 'markdown': {'gcs_uri': 'gs://c/r.md', 'from_xml': True}}},
                'discovery': {
                    'records': [{'pmid': '111', 'title': 'An indexed paper', 'abstract': 'Something.'}],
                    'entities': [],
                    'book_articles': [],
                },
            }
        ),
    )

    async def run() -> tuple[literature_pb2.PaperInfo, literature_pb2.SearchEuropePmcResponse]:
        async with _serving() as stub:
            info = await stub.DescribePaper(literature_pb2.DescribePaperRequest(doc_id='doc-1'))
            searched = await stub.SearchEuropePmc(
                literature_pb2.SearchEuropePmcRequest(query='indexed', max_results=10)
            )
            return info, searched

    info, searched = asyncio.run(run())
    assert info.title == 'A paper'
    assert info.default_representation == literature_pb2.REPRESENTATION_MARKDOWN
    assert [record.pmid for record in searched.records] == ['111']


def test_the_live_selector_hands_its_client_to_the_stack(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
) -> None:
    assert gcs_bucket.name is not None
    doc_id = '000006fa-e679-4f46-a052-8fb0e69f280c'
    _seed_paper(gcs_bucket, doc_id, ready=True)
    _live_env(monkeypatch, bucket_name=gcs_bucket.name, gcs_endpoint=gcs_endpoint)

    created_clients, closed_clients = _track_storage_clients(monkeypatch)

    def europe_pmc_handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == 'www.ebi.ac.uk'
        assert request.url.params.get('query') == 'variant'
        assert request.url.params.get('pageSize') == '5'
        return httpx2.Response(200, json=_EUROPEPMC_BATCH)

    async def run() -> tuple[literature_pb2.PaperInfo, literature_pb2.SearchEuropePmcResponse]:
        async with contextlib.AsyncExitStack() as stack, _serving(handler=europe_pmc_handler, stack=stack) as stub:
            info = await stub.DescribePaper(literature_pb2.DescribePaperRequest(doc_id=doc_id))
            searched = await stub.SearchEuropePmc(literature_pb2.SearchEuropePmcRequest(query='variant', max_results=5))
            assert len(created_clients) == 1
            assert not closed_clients, 'the client is held open for as long as the server runs'
        assert closed_clients == created_clients, 'the stack closed the client on unwind'
        return info, searched

    info, searched = asyncio.run(run())
    assert info.title == 'A live paper'
    assert [r.pmid for r in searched.records] == ['24789689', '24789688']


def test_live_backend_requires_the_openalex_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'live')
    monkeypatch.setenv('THEMIS_FULLTEXT_BUCKET', 'a-bucket')
    monkeypatch.delenv(_OPENALEX_API_KEY_VAR, raising=False)
    with pytest.raises(SystemExit, match=_OPENALEX_API_KEY_VAR):
        _start_server()


def test_a_malformed_openalex_api_key_fails_at_startup_without_showing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'live')
    monkeypatch.setenv('THEMIS_FULLTEXT_BUCKET', 'a-bucket')
    monkeypatch.setenv(_OPENALEX_API_KEY_VAR, f'{_OPENALEX_API_KEY}\n')
    with pytest.raises(SystemExit, match=_OPENALEX_API_KEY_VAR) as caught:
        _start_server()
    assert _OPENALEX_API_KEY not in str(caught.value)


def test_live_selector_fails_loud_on_an_unreadable_bucket(
    monkeypatch: pytest.MonkeyPatch,
    gcs_endpoint: str,
) -> None:
    missing_bucket = 'themis-nonexistent-bucket'
    _live_env(monkeypatch, bucket_name=missing_bucket, gcs_endpoint=gcs_endpoint)
    created_clients, closed_clients = _track_storage_clients(monkeypatch)

    async def run() -> None:
        async with contextlib.AsyncExitStack() as stack:
            with pytest.raises(SystemExit, match=missing_bucket):
                async with _serving(stack=stack):
                    pass
        assert len(created_clients) == 1
        assert closed_clients == created_clients, 'a failed startup probe leaks no client'

    asyncio.run(run())


def test_live_selector_without_the_crosswalk_leaves_id_resolution_off(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
) -> None:
    assert gcs_bucket.name is not None
    _live_env(monkeypatch, bucket_name=gcs_bucket.name, gcs_endpoint=gcs_endpoint)

    async def run() -> None:
        async with _serving() as stub:
            await stub.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=['doi:10.1/x']))

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert authored_status.authored(caught.value)
    assert 'id resolution is not configured' in (caught.value.details() or '')


@pytest.mark.parametrize('omitted', _CROSSWALK_VARS)
def test_a_partial_crosswalk_config_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
    omitted: str,
) -> None:
    assert gcs_bucket.name is not None
    _live_env(monkeypatch, bucket_name=gcs_bucket.name, gcs_endpoint=gcs_endpoint)
    for var, value in _CROSSWALK_ENV.items():
        if var != omitted:
            monkeypatch.setenv(var, value)
    with pytest.raises(SystemExit, match='all be set or all unset'):
        _start_server()


def test_a_complete_crosswalk_config_resolves_through_the_named_instance(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
) -> None:
    assert gcs_bucket.name is not None
    doc_id = crosswalk.mint(conn, ['doi:10.1/x']).doc_id
    _seed_paper(gcs_bucket, doc_id, ready=True)
    _live_env(monkeypatch, bucket_name=gcs_bucket.name, gcs_endpoint=gcs_endpoint)
    for var, value in _CROSSWALK_ENV.items():
        monkeypatch.setenv(var, value)
    dialled, closed = _wire_crosswalk(monkeypatch, crosswalk_connect)

    async def run() -> literature_pb2.MaybeIngestPapersResponse:
        async with contextlib.AsyncExitStack() as stack, _serving(stack=stack) as stub:
            reply = await stub.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=['doi:10.1/x']))
            assert not closed, 'the connector is held for as long as the server runs'
        assert closed == [True], 'the stack closed the connector on unwind'
        return reply

    reply = asyncio.run(run())
    assert [(r.external_id, r.doc_id, r.state) for r in reply.readiness] == [
        ('doi:10.1/x', doc_id, literature_pb2.FULL_TEXT_STATE_READY)
    ]
    instance, database, db_user = _CROSSWALK_ENV.values()
    assert dialled == [(instance, database, db_user)]


def test_live_selector_without_the_convert_trio_leaves_conversion_off(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
) -> None:
    assert gcs_bucket.name is not None
    doc_id = crosswalk.mint(conn, ['doi:10.1/x']).doc_id
    _seed_paper(gcs_bucket, doc_id, ready=False)
    _live_env(monkeypatch, bucket_name=gcs_bucket.name, gcs_endpoint=gcs_endpoint)
    for var, value in _CROSSWALK_ENV.items():
        monkeypatch.setenv(var, value)
    _wire_crosswalk(monkeypatch, crosswalk_connect)

    async def run() -> None:
        async with _serving() as stub:
            await stub.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=['doi:10.1/x']))

    with pytest.raises(grpc.aio.AioRpcError) as caught:
        asyncio.run(run())
    assert caught.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert authored_status.authored(caught.value)
    assert 'conversion is not configured' in (caught.value.details() or '')


@pytest.mark.parametrize('omitted', _CONVERT_VARS)
def test_a_partial_convert_config_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
    omitted: str,
) -> None:
    assert gcs_bucket.name is not None
    _live_env(monkeypatch, bucket_name=gcs_bucket.name, gcs_endpoint=gcs_endpoint)
    for var, value in _CONVERT_ENV.items():
        if var != omitted:
            monkeypatch.setenv(var, value)
    with pytest.raises(SystemExit, match='all be set or all unset'):
        _start_server()


def test_a_complete_convert_config_targets_the_named_queue_and_worker(
    monkeypatch: pytest.MonkeyPatch,
    gcs_bucket: storage.Bucket,
    gcs_endpoint: str,
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
) -> None:
    assert gcs_bucket.name is not None
    doc_id = crosswalk.mint(conn, ['doi:10.1/x']).doc_id
    _seed_paper(gcs_bucket, doc_id, ready=False)
    _live_env(monkeypatch, bucket_name=gcs_bucket.name, gcs_endpoint=gcs_endpoint)
    for var, value in (*_CROSSWALK_ENV.items(), *_CONVERT_ENV.items()):
        monkeypatch.setenv(var, value)
    _wire_crosswalk(monkeypatch, crosswalk_connect)

    created_requests: list[tasks_v2.CreateTaskRequest] = []
    built_clients: list[tasks_v2.CloudTasksClient] = []

    def register_cloud_tasks(server: grpc.aio.Server) -> None:
        def create_task(request: tasks_v2.CreateTaskRequest, _ctx: object) -> tasks_v2.Task:
            created_requests.append(request)
            return request.task

        handler = grpc.method_handlers_generic_handler(
            'google.cloud.tasks.v2.CloudTasks',
            {
                'CreateTask': grpc.unary_unary_rpc_method_handler(
                    create_task,
                    request_deserializer=tasks_v2.CreateTaskRequest.deserialize,
                    response_serializer=tasks_v2.Task.serialize,
                )
            },
        )
        server.add_generic_rpc_handlers([handler])

    real_tasks_client = config.tasks_v2.CloudTasksClient

    with in_process_grpc.serving_in_thread(register_cloud_tasks) as tasks_target:

        def build_loopback_tasks_client() -> tasks_v2.CloudTasksClient:
            transport = tasks_grpc.CloudTasksGrpcTransport(channel=grpc.insecure_channel(tasks_target))
            client = real_tasks_client(transport=transport)
            built_clients.append(client)
            return client

        monkeypatch.setattr(config.tasks_v2, 'CloudTasksClient', build_loopback_tasks_client)

        async def run() -> literature_pb2.MaybeIngestPapersResponse:
            async with contextlib.AsyncExitStack() as stack, _serving(stack=stack) as stub:
                return await stub.MaybeIngestPapers(
                    literature_pb2.MaybeIngestPapersRequest(external_ids=['doi:10.1/x'])
                )

        reply = asyncio.run(run())
        assert [(r.external_id, r.doc_id, r.state) for r in reply.readiness] == [
            ('doi:10.1/x', doc_id, literature_pb2.FULL_TEXT_STATE_PENDING)
        ]
        assert len(created_requests) == 1
        queue, worker_url, invoker = _CONVERT_ENV.values()
        enqueued = created_requests[0]
        assert enqueued.parent == queue
        assert enqueued.task.http_request.url == f'{worker_url}/convert'
        assert enqueued.task.http_request.oidc_token.service_account_email == invoker
        assert enqueued.task.http_request.oidc_token.audience == worker_url
        assert work_pb2.WorkRequest.FromString(enqueued.task.http_request.body).doc_id == doc_id
        # Exiting `deps.stack` exited `CloudTasksClient`, closing its underlying gRPC channel.
        with pytest.raises(ValueError, match='closed'):
            built_clients[0].create_task(parent=queue, task=enqueued.task)
