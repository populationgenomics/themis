"""Behavioural tests for `LiveBackend` over loopback gRPC, `litcache.Store`, and `discovery.Indexes`."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import pathlib
import typing
from collections.abc import AsyncIterable, Awaitable, Callable, Sequence

import httpx2
import pg8000.dbapi
from google.cloud import storage, tasks_v2
from google.protobuf import timestamp_pb2

from themis.litcache import crosswalk, enqueue, metadata_fields, sourced_metadata
from themis.litcache.models import litcache_pb2
from themis.rpc import literature_pb2, literature_pb2_grpc
from themis.services.evidence.literature import discovery as discovery_mod
from themis.services.evidence.literature import litcache as litcache_store
from themis.services.evidence.literature import live as live_mod
from themis.services.evidence.literature import servicer as servicer_mod
from themis.services.evidence.tests import authz
from themis.testing import in_process_grpc
from themis.testing import openalex as fake_openalex

_UPSTREAM_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_EUROPEPMC_BATCH = json.loads((_UPSTREAM_FIXTURES / 'europepmc_batch_records.json').read_text('utf-8'))
_PUBMED_EFETCH_XML = (_UPSTREAM_FIXTURES / 'pubmed_efetch.xml').read_bytes()

_DOC = '000006fa-e679-4f46-a052-8fb0e69f280c'
_DOC_PENDING = '000006fa-e679-4f46-a052-8fb0e69f281d'
_MARKDOWN = '# A live paper\n\nThe channel showed markedly reduced ATP sensitivity in vitro.\n'
_QUOTE = 'markedly reduced ATP sensitivity'
_FRESH_DOI = 'doi:10.1038/s41586-020-2649-2'
_CAPTURED = datetime.datetime(2026, 6, 29, tzinfo=datetime.UTC)
_CONVERT_TARGET = enqueue.ConversionTarget(
    queue_path='projects/p/locations/r/queues/themis-convert',
    worker_url='https://themis-convert-worker-abc-ts.a.run.app',
    invoker_service_account_email='themis-convert-invoker@p.iam.gserviceaccount.com',
)


class _RecordingTasksClient:
    """Records Cloud Tasks creates without dialling Google Cloud."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def create_task(self, *, parent: str, task: object) -> object:
        del parent
        self.names.append(getattr(task, 'name', ''))
        return task


def _metadata_bytes(title: str) -> bytes:
    source = litcache_pb2.MetadataSource()
    source.pubmed.article.medline_citation.article.article_title.value = title
    sourced = litcache_pb2.SourcedMetadata(metadata=metadata_fields.fill(source), source=source)
    return sourced_metadata.to_canonical_bytes(sourced)


def _seed_paper(
    bucket: storage.Bucket,
    doc_id: str,
    *,
    markdown: str | None = _MARKDOWN,
    files: Sequence[litcache_pb2.AssociatedFile] = (),
) -> str:
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
        external_ids=litcache_pb2.ExternalIds(doi='10.1/live'),
        sources=[xml_source],
        files=list(files),
    )
    rendering_hash = ''
    if markdown is not None:
        rendering_hash = hashlib.sha256(markdown.encode('utf-8')).hexdigest()
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
        bucket.blob(f'papers/{doc_id}/renderings/{rendering_hash}.md').upload_from_string(markdown)
    bucket.blob(f'papers/{doc_id}/manifest.pb').upload_from_string(manifest.SerializeToString())
    bucket.blob(f'papers/{doc_id}/metadata.pb').upload_from_string(_metadata_bytes('A live paper'))
    return rendering_hash


def _no_upstream(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f'unexpected upstream request: {request.url}')


def _run_store_rpc[T](
    bucket: storage.Bucket,
    call: Callable[[literature_pb2_grpc.LiteratureAsyncStub], Awaitable[T]],
) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(_no_upstream)) as http_client:
            store = litcache_store.Store(bucket, http_client, openalex_api_key=fake_openalex.API_KEY)
            servicer = servicer_mod.Servicer(live_mod.LiveBackend(store, discovery_mod.Indexes(http_client)))
            async with in_process_grpc.serving(
                lambda server: literature_pb2_grpc.add_LiteratureServicer_to_server(servicer, server),
                interceptors=(authz.InjectMetadata(authz.ADMITTED), authz.InjectStreamMetadata(authz.ADMITTED)),
                server_interceptors=authz.server_interceptors(),
            ) as channel:
                return await call(literature_pb2_grpc.LiteratureStub(channel))

    return asyncio.run(run())


def _run_index_rpc[T](
    handler: Callable[[httpx2.Request], httpx2.Response],
    call: Callable[[literature_pb2_grpc.LiteratureAsyncStub], Awaitable[T]],
) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
            store = litcache_store.Store(
                typing.cast('storage.Bucket', None), http_client, openalex_api_key=fake_openalex.API_KEY
            )
            servicer = servicer_mod.Servicer(live_mod.LiveBackend(store, discovery_mod.Indexes(http_client)))
            async with in_process_grpc.serving(
                lambda server: literature_pb2_grpc.add_LiteratureServicer_to_server(servicer, server),
                interceptors=(authz.InjectMetadata(authz.ADMITTED),),
                server_interceptors=authz.server_interceptors(),
            ) as channel:
                return await call(literature_pb2_grpc.LiteratureStub(channel))

    return asyncio.run(run())


async def _drained(
    responses: AsyncIterable[literature_pb2.ReadMarkdownResponse],
) -> list[literature_pb2.ReadMarkdownResponse]:
    return [response async for response in responses]


def test_live_describe_paper_serves_the_seeded_paper(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(
        gcs_bucket,
        _DOC,
        files=[
            litcache_pb2.AssociatedFile(
                name='figure1.png',
                path='figures/figure1.png',
                role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_FIGURE,
            )
        ],
    )
    info = _run_store_rpc(gcs_bucket, lambda s: s.DescribePaper(literature_pb2.DescribePaperRequest(doc_id=_DOC)))
    assert info.title == 'A live paper'
    assert info.has_markdown is True
    assert [(f.name, f.role) for f in info.files] == [('figure1.png', literature_pb2.FILE_ROLE_FIGURE)]


def test_live_resolve_content_names_the_bucket_object(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(
        gcs_bucket,
        _DOC,
        files=[
            litcache_pb2.AssociatedFile(
                name='figure1.png',
                path='figures/figure1.png',
                role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_FIGURE,
            )
        ],
    )
    content = _run_store_rpc(
        gcs_bucket,
        lambda s: s.ResolveContent(
            literature_pb2.ResolveContentRequest(
                doc_id=_DOC,
                file=literature_pb2.FileSelector(name='figure1.png'),
            )
        ),
    )
    assert content.gcs_uri == f'gs://{gcs_bucket.name}/papers/{_DOC}/figures/figure1.png'
    assert content.media_type == 'image/png'


def test_live_read_markdown_streams_the_rendering(gcs_bucket: storage.Bucket) -> None:
    rendering_hash = _seed_paper(gcs_bucket, _DOC)
    parts = _run_store_rpc(
        gcs_bucket,
        lambda s: _drained(s.ReadMarkdown(literature_pb2.ReadMarkdownRequest(doc_id=_DOC))),
    )
    assert parts[0].header.rendering_key == rendering_hash
    assert b''.join(part.chunk for part in parts[1:]).decode('utf-8') == _MARKDOWN


def test_live_locate_forwards_doc_id_and_quote_without_swapping(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, _DOC)
    located = _run_store_rpc(
        gcs_bucket,
        lambda s: s.Locate(
            literature_pb2.LocateRequest(
                doc_id=_DOC,
                quote=_QUOTE,
                representation=literature_pb2.REPRESENTATION_MARKDOWN,
            )
        ),
    )
    assert _MARKDOWN[located.offsets.start : located.offsets.end] == _QUOTE


def test_live_validate_forwards_doc_id_and_quote_without_swapping(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, _DOC)
    validated = _run_store_rpc(
        gcs_bucket,
        lambda s: s.Validate(literature_pb2.ValidateRequest(doc_id=_DOC, quote=_QUOTE)),
    )
    assert validated.ok is True
    assert list(validated.located_in) == [literature_pb2.REPRESENTATION_MARKDOWN]


def test_live_poll_full_texts_reports_readiness_from_the_bucket(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, _DOC)
    _seed_paper(gcs_bucket, _DOC_PENDING, markdown=None)
    reply = _run_store_rpc(
        gcs_bucket,
        lambda s: s.PollFullTexts(literature_pb2.PollFullTextsRequest(doc_ids=[_DOC, _DOC_PENDING])),
    )
    assert {r.doc_id: r.state for r in reply.readiness} == {
        _DOC: literature_pb2.FULL_TEXT_STATE_READY,
        _DOC_PENDING: literature_pb2.FULL_TEXT_STATE_PENDING,
    }


def test_live_maybe_ingest_papers_resolves_known_ids_through_the_crosswalk(
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
    gcs_bucket: storage.Bucket,
) -> None:
    minted = crosswalk.mint(conn, ['doi:10.1/live'])
    _seed_paper(gcs_bucket, minted.doc_id)

    async def run() -> literature_pb2.MaybeIngestPapersResponse:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(_no_upstream)) as http_client:
            store = litcache_store.Store(
                gcs_bucket,
                http_client,
                openalex_api_key=fake_openalex.API_KEY,
                connect=crosswalk_connect,
            )
            servicer = servicer_mod.Servicer(live_mod.LiveBackend(store, discovery_mod.Indexes(http_client)))
            async with in_process_grpc.serving(
                lambda server: literature_pb2_grpc.add_LiteratureServicer_to_server(servicer, server),
                interceptors=(authz.InjectMetadata(authz.ADMITTED),),
                server_interceptors=authz.server_interceptors(),
            ) as channel:
                stub = literature_pb2_grpc.LiteratureStub(channel)
                return await stub.MaybeIngestPapers(
                    literature_pb2.MaybeIngestPapersRequest(external_ids=['doi:10.1/live'])
                )

    reply = asyncio.run(run())
    assert [(r.doc_id, r.state) for r in reply.readiness] == [(minted.doc_id, literature_pb2.FULL_TEXT_STATE_READY)]


def test_live_maybe_ingest_papers_ingests_unknown_ids_and_enqueues_conversion(
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
    gcs_bucket: storage.Bucket,
) -> None:
    del conn  # requested for the per-test crosswalk truncation; the store dials its own
    tasks = _RecordingTasksClient()
    enqueuer = enqueue.Enqueuer(typing.cast('tasks_v2.CloudTasksClient', tasks), _CONVERT_TARGET)
    doi = _FRESH_DOI.removeprefix('doi:')

    def ingest_handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == fake_openalex.HOST:
            return fake_openalex.answer([fake_openalex.work(work_id='W1', doi=doi)], request)
        assert request.url.host == 'api.crossref.org', request.url
        return httpx2.Response(200, json={'message': {'DOI': doi, 'title': ['Resolved paper'], 'publisher': 'X'}})

    async def run() -> literature_pb2.MaybeIngestPapersResponse:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(ingest_handler)) as http_client:
            store = litcache_store.Store(
                gcs_bucket,
                http_client,
                openalex_api_key=fake_openalex.API_KEY,
                connect=crosswalk_connect,
                enqueuer=enqueuer,
            )
            servicer = servicer_mod.Servicer(live_mod.LiveBackend(store, discovery_mod.Indexes(http_client)))
            async with in_process_grpc.serving(
                lambda server: literature_pb2_grpc.add_LiteratureServicer_to_server(servicer, server),
                interceptors=(authz.InjectMetadata(authz.ADMITTED),),
                server_interceptors=authz.server_interceptors(),
            ) as channel:
                stub = literature_pb2_grpc.LiteratureStub(channel)
                return await stub.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[_FRESH_DOI]))

    reply = asyncio.run(run())
    doc_id = reply.readiness[0].doc_id
    assert reply.readiness[0].state == literature_pb2.FULL_TEXT_STATE_PENDING
    assert tasks.names == [f'{_CONVERT_TARGET.queue_path}/tasks/{doc_id}--main_text']


def test_live_search_europe_pmc_forwards_query_and_max_results() -> None:
    asked: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        page_size = int(request.url.params['pageSize'])
        asked.append((request.url.params['query'], str(page_size)))
        results = _EUROPEPMC_BATCH['resultList']['result']
        return httpx2.Response(
            200,
            json={'hitCount': _EUROPEPMC_BATCH['hitCount'], 'resultList': {'result': results[:page_size]}},
        )

    reply = _run_index_rpc(
        handler,
        lambda s: s.SearchEuropePmc(literature_pb2.SearchEuropePmcRequest(query='GENE1 truncating', max_results=1)),
    )
    assert asked == [('GENE1 truncating', '1')]
    assert len(reply.records) == 1
    assert reply.total_matched == _EUROPEPMC_BATCH['hitCount']


def test_live_fetch_pubmed_articles_returns_records_and_missing_pmids() -> None:
    reply = _run_index_rpc(
        lambda _r: httpx2.Response(200, content=_PUBMED_EFETCH_XML),
        lambda s: s.FetchPubmedArticles(literature_pb2.FetchPubmedArticlesRequest(pmids=['29089047', '9999999'])),
    )
    assert [a.medline_citation.pmid.value for a in reply.articles] == ['29089047']
    assert list(reply.pmids_without_record) == ['9999999']


def test_live_search_litvar_keeps_pmid_limit_and_entity_ceiling_distinct() -> None:
    entity_ids = ['litvar@CA1##', 'litvar@CA2##', 'litvar@CA3##']

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('/variant/autocomplete/'):
            return httpx2.Response(200, json=[{'_id': entity_id} for entity_id in entity_ids])
        if request.url.path.endswith('/litvar2-api/search/'):
            return httpx2.Response(
                200,
                json={'count': 4, 'total_pages': 1, 'results': [{'pmid': p} for p in ('11', '22', '33', '44')]},
            )
        entity_id = httpx2.URL(str(request.url)).path.rsplit('/', 1)[-1]
        return httpx2.Response(200, json={'_id': entity_id, 'clingen_ids': ['CA1'], 'gene': ['GENE1']})

    # max_pmids_per_entity=2 while the servicer's entity ceiling is 8: swapping max_results and
    # max_entities in LiveBackend.search_litvar would cap entities at 2 and return all 4 PMIDs.
    reply = _run_index_rpc(
        handler,
        lambda s: s.SearchLitVar(literature_pb2.SearchLitVarRequest(caid='CA1', max_pmids_per_entity=2)),
    )
    assert [entity.id for entity in reply.entities] == entity_ids
    assert [list(entity.pmids) for entity in reply.entities] == [['11', '22'], ['11', '22'], ['11', '22']]


def test_live_list_litvar_entities_forwards_gene_and_contains_without_swapping() -> None:
    listing = (
        "{'_id': 'litvar@#77#p.A355T', 'pmids_count': 9}\n"
        "{'_id': 'litvar@#77#p.A355S', 'pmids_count': 4}\n"
        "{'_id': 'litvar@#77#p.R100Q', 'pmids_count': 12}\n"
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        if '/gene/GENE1' not in request.url.path:
            return httpx2.Response(404, text='wrong gene')
        return httpx2.Response(200, text=listing)

    reply = _run_index_rpc(
        handler,
        lambda s: s.ListLitVarEntities(
            literature_pb2.ListLitVarEntitiesRequest(gene='GENE1', contains='a355', max_results=1)
        ),
    )
    assert [entity.id for entity in reply.entities] == ['litvar@#77#p.A355T']
    assert (reply.total_in_gene, reply.total_matched) == (3, 2)
