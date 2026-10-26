"""Behaviour tests for the litcache-reading store over a fake-gcs-server bucket.

Docker-gated via the shared ``gcs_bucket`` fixture: the store works against a real
``google.cloud.storage.Bucket``, so the tests seed real ``manifest.pb`` / ``metadata.pb`` /
rendering objects and exercise the same reads the deploy uses.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import time
import typing
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping, Sequence

import grpc
import grpc.aio
import httpx2
import pg8000.dbapi
import pg8000.exceptions
import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import storage, tasks_v2
from google.protobuf import timestamp_pb2
from pubmed_proto import pubmed_pb2

from themis.common import sql
from themis.litcache import efetch, enqueue, ingest, mirror, paper_metadata, resolve, writer
from themis.litcache import outcome as litcache_outcome
from themis.litcache.models import litcache_pb2
from themis.rpc import literature_pb2, literature_pb2_grpc
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import discovery as discovery_mod
from themis.services.evidence.literature import litcache as litcache_store
from themis.services.evidence.literature import live as live_mod
from themis.services.evidence.literature import servicer as servicer_mod
from themis.services.evidence.tests import authz
from themis.testing import in_process_grpc

_DOC = '000006fa-e679-4f46-a052-8fb0e69f280c'
_DOC_PENDING = '000006fa-e679-4f46-a052-8fb0e69f281d'  # a second paper: source, no rendering
_DOC_TERMINAL = '000006fa-e679-4f46-a052-8fb0e69f282e'  # a third paper: settled without a rendering
_MARKDOWN = '# A title\n\nThe channel showed markedly reduced ATP sensitivity in vitro.\n'
_QUOTE = 'markedly reduced ATP sensitivity'
_CAPTURED = datetime.datetime(2026, 6, 29, tzinfo=datetime.UTC)


def _ts(when: datetime.datetime) -> timestamp_pb2.Timestamp:
    stamp = timestamp_pb2.Timestamp()
    stamp.FromDatetime(when)
    return stamp


def _pdf_source(*, captured: datetime.datetime = _CAPTURED) -> litcache_pb2.Source:
    return litcache_pb2.Source(
        handle='pdf',
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
        licence='https://creativecommons.org/licenses/by/4.0/',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED,
        access=litcache_pb2.Access(free_to_read=litcache_pb2.FreeToRead()),
        revisions=[
            litcache_pb2.Revision(
                hash='pdfrev', kind=litcache_pb2.SourceKind.SOURCE_KIND_SEED, captured_at=_ts(captured)
            )
        ],
    )


def _xml_source() -> litcache_pb2.Source:
    return litcache_pb2.Source(
        handle='xml',
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_XML,
        licence='https://creativecommons.org/licenses/by/4.0/',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT,
        access=litcache_pb2.Access(free_to_read=litcache_pb2.FreeToRead()),
        revisions=[
            litcache_pb2.Revision(
                hash='xmlrev', kind=litcache_pb2.SourceKind.SOURCE_KIND_PMC_OA_S3, captured_at=_ts(_CAPTURED)
            )
        ],
    )


def _rendering(from_source: str, converter: litcache_pb2.Converter) -> litcache_pb2.Record:
    """A rendering record made from `from_source`'s one revision (`_pdf_source` and `_xml_source` name theirs so)."""
    construction = litcache_pb2.RenderingConstruction(converter=converter, converter_version='0.4.0')
    if converter == litcache_pb2.Converter.CONVERTER_LLM_OCR:
        construction.model = 'claude-sonnet-5'
    return litcache_pb2.Record(deps=[f'{from_source}rev'], created_at=_ts(_CAPTURED), rendering=construction)


def _metadata(title: str) -> bytes:
    """A journal record's ``metadata.pb``: PubMed's field, in its journal arm."""
    metadata = litcache_pb2.PaperMetadata()
    metadata.pubmed.article.medline_citation.article.article_title.value = title
    return metadata.SerializeToString()


def _crossref_metadata(title: str) -> bytes:
    """A Crossref-resolved paper's ``metadata.pb``: the envelope's ``crossref`` field alone."""
    metadata = litcache_pb2.PaperMetadata()
    metadata.crossref.title.append(title)
    return metadata.SerializeToString()


def _book_metadata(*, chapter_title: str | None, book_title: str) -> bytes:
    """A book record's ``metadata.pb``: PubMed's field, in its book arm; ``chapter_title=None`` is a whole book."""
    metadata = litcache_pb2.PaperMetadata()
    document = metadata.pubmed.book_article.book_document
    document.book.book_title.value = book_title
    if chapter_title is not None:
        document.article_title.value = chapter_title
    return metadata.SerializeToString()


def _seed_paper(
    bucket: storage.Bucket,
    *,
    doc_id: str = _DOC,
    sources: Sequence[litcache_pb2.Source] = (),
    rendering: litcache_pb2.Record | None = None,
    markdown: str | None = _MARKDOWN,
    files: Sequence[litcache_pb2.AssociatedFile] = (),
    metadata: bytes | None = _metadata('A title'),
) -> str:
    """Write a paper directory into the bucket; return the markdown rendering hash (if any).

    ``metadata=None`` omits ``metadata.pb`` — the not-yet-resolved-metadata case.
    """
    manifest = litcache_pb2.Manifest(
        doc_id=doc_id,
        external_ids=litcache_pb2.ExternalIds(doi='10.1/x'),
        sources=list(sources),
        files=list(files),
    )
    rendering_hash = ''
    if markdown is not None:
        rendering_hash = hashlib.sha256(markdown.encode('utf-8')).hexdigest()
        # The record's key is the markdown's own hash, so the caller passes just the record, and the markdown ref
        # names it.
        manifest.records[rendering_hash].CopyFrom(
            rendering or _rendering('xml', litcache_pb2.Converter.CONVERTER_LITDOWN)
        )
        manifest.refs['markdown'] = rendering_hash
        bucket.blob(f'papers/{doc_id}/renderings/{rendering_hash}.md').upload_from_string(markdown)
    bucket.blob(f'papers/{doc_id}/manifest.pb').upload_from_string(manifest.SerializeToString())
    if metadata is not None:
        bucket.blob(f'papers/{doc_id}/metadata.pb').upload_from_string(metadata)
    return rendering_hash


# The reads under test issue no HTTP call, so the client is never touched; the ingest tests below pass
# a real one alongside a fake metadata ladder.
_NO_HTTP = typing.cast('httpx2.AsyncClient', None)


def _store(bucket: storage.Bucket) -> litcache_store.Store:
    return litcache_store.Store(bucket, _NO_HTTP)


def test_describe_prefers_markdown_when_xml_derived(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_pdf_source(), _xml_source()], metadata=_metadata(r'K\_ATP channel study'))
    info = asyncio.run(_store(gcs_bucket).describe_paper(_DOC))
    assert info.title == 'K_ATP channel study'
    assert info.has_markdown
    assert info.has_pdf
    assert info.markdown_from_xml
    assert info.default_representation == literature_pb2.REPRESENTATION_MARKDOWN


def test_describe_defaults_to_pdf_when_rendering_is_pdf_derived(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(
        gcs_bucket,
        sources=[_pdf_source()],
        rendering=_rendering('pdf', litcache_pb2.Converter.CONVERTER_DOCLING),
    )
    info = asyncio.run(_store(gcs_bucket).describe_paper(_DOC))
    assert info.has_markdown
    assert info.has_pdf
    assert not info.markdown_from_xml
    assert info.default_representation == literature_pb2.REPRESENTATION_PDF


def test_describe_lists_files_with_inferred_media_types(gcs_bucket: storage.Bucket) -> None:
    files = [
        litcache_pb2.AssociatedFile(
            role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_SUPPLEMENTARY,
            name='table.xlsx',
            path='supplementary/abc.xlsx',
        )
    ]
    _seed_paper(gcs_bucket, sources=[_xml_source()], files=files)
    info = asyncio.run(_store(gcs_bucket).describe_paper(_DOC))
    (file,) = info.files
    assert file.name == 'table.xlsx'
    assert file.role == literature_pb2.FILE_ROLE_SUPPLEMENTARY
    assert file.media_type == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def test_describe_unknown_paper_raises(gcs_bucket: storage.Bucket) -> None:
    with pytest.raises(literature_backend.UnknownPaperError):
        asyncio.run(_store(gcs_bucket).describe_paper('no-such-doc'))


def test_resolve_names_the_rendering_pdf_and_file_objects(gcs_bucket: storage.Bucket) -> None:
    rendering_hash = _seed_paper(
        gcs_bucket,
        sources=[_pdf_source(), _xml_source()],
        files=[
            litcache_pb2.AssociatedFile(
                role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_FIGURE,
                name='fig1.jpg',
                path='supplementary/fig1hex.jpg',
            )
        ],
    )
    backend = _store(gcs_bucket)
    md = asyncio.run(backend.resolve_content(_DOC, literature_backend.MarkdownContent()))
    assert md.gcs_uri == f'gs://{gcs_bucket.name}/papers/{_DOC}/renderings/{rendering_hash}.md'
    assert md.media_type == 'text/markdown'
    pdf = asyncio.run(backend.resolve_content(_DOC, literature_backend.PdfContent()))
    assert pdf.gcs_uri == f'gs://{gcs_bucket.name}/papers/{_DOC}/sources/pdf/pdfrev.pdf'
    assert pdf.media_type == 'application/pdf'
    fig = asyncio.run(backend.resolve_content(_DOC, literature_backend.FileContent(name='fig1.jpg')))
    assert fig.gcs_uri == f'gs://{gcs_bucket.name}/papers/{_DOC}/supplementary/fig1hex.jpg'
    assert fig.media_type == 'image/jpeg'


def test_resolve_pdf_names_the_newest_revision_by_captured_at_not_array_order(gcs_bucket: storage.Bucket) -> None:
    # Recency is captured_at, never array order: the newest revision sits neither first nor last, so a
    # revisions[0] or revisions[-1] shortcut in _current_revision would name the wrong object.
    source = _pdf_source()
    source.revisions[0].hash = 'oldest'
    source.revisions[0].captured_at.CopyFrom(_ts(_CAPTURED))
    source.revisions.append(
        litcache_pb2.Revision(
            hash='newest',
            kind=litcache_pb2.SourceKind.SOURCE_KIND_SEED,
            captured_at=_ts(_CAPTURED + datetime.timedelta(days=2)),
        )
    )
    source.revisions.append(
        litcache_pb2.Revision(
            hash='middling',
            kind=litcache_pb2.SourceKind.SOURCE_KIND_SEED,
            captured_at=_ts(_CAPTURED + datetime.timedelta(days=1)),
        )
    )
    _seed_paper(gcs_bucket, sources=[source])
    pdf = asyncio.run(_store(gcs_bucket).resolve_content(_DOC, literature_backend.PdfContent()))
    assert pdf.gcs_uri.endswith('/sources/pdf/newest.pdf')


def test_resolve_missing_pdf_raises_missing_content(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    with pytest.raises(literature_backend.MissingContentError):
        asyncio.run(_store(gcs_bucket).resolve_content(_DOC, literature_backend.PdfContent()))


def test_resolve_unfetched_file_raises_missing_content(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(
        gcs_bucket,
        sources=[_xml_source()],
        files=[
            litcache_pb2.AssociatedFile(
                role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_SUPPLEMENTARY,
                name='big.zip',
                source_url='https://example.org/big.zip',
            )
        ],
    )
    with pytest.raises(literature_backend.MissingContentError):
        asyncio.run(_store(gcs_bucket).resolve_content(_DOC, literature_backend.FileContent(name='big.zip')))


def test_locate_markdown_returns_offsets(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    result = asyncio.run(_store(gcs_bucket).locate(_DOC, _QUOTE, literature_pb2.REPRESENTATION_MARKDOWN))
    assert result.WhichOneof('result') == 'offsets'
    assert _MARKDOWN[result.offsets.start : result.offsets.end] == _QUOTE


def test_locate_absent_quote_is_not_located(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    result = asyncio.run(_store(gcs_bucket).locate(_DOC, 'never in the text', literature_pb2.REPRESENTATION_MARKDOWN))
    assert result.WhichOneof('result') == 'not_located'


def test_validate_reports_markdown_when_located(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    result = asyncio.run(_store(gcs_bucket).validate(_DOC, _QUOTE))
    assert result.ok
    assert list(result.located_in) == [literature_pb2.REPRESENTATION_MARKDOWN]


def test_validate_unknown_doc_is_not_ok(gcs_bucket: storage.Bucket) -> None:
    result = asyncio.run(_store(gcs_bucket).validate('no-such-doc', _QUOTE))
    assert not result.ok
    assert 'unknown doc_id' in result.reason


def test_locate_and_validate_do_not_need_metadata(gcs_bucket: storage.Bucket) -> None:
    # metadata.pb resolves only the title; locate/validate must work without it.
    _seed_paper(gcs_bucket, sources=[_xml_source()], metadata=None)
    backend = _store(gcs_bucket)
    located = asyncio.run(backend.locate(_DOC, _QUOTE, literature_pb2.REPRESENTATION_MARKDOWN))
    assert located.WhichOneof('result') == 'offsets'
    assert asyncio.run(backend.validate(_DOC, _QUOTE)).ok


def test_describe_without_metadata_falls_back_to_an_external_id(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()], metadata=None)
    info = asyncio.run(_store(gcs_bucket).describe_paper(_DOC))
    assert info.title == '10.1/x'  # the manifest's DOI, not a crash on the missing metadata


def test_describe_titles_a_bookshelf_chapter_by_its_chapter_title(gcs_bucket: storage.Bucket) -> None:
    metadata = _book_metadata(chapter_title='A synthetic chapter', book_title='A synthetic review series')
    _seed_paper(gcs_bucket, sources=[_xml_source()], metadata=metadata)
    info = asyncio.run(_store(gcs_bucket).describe_paper(_DOC))
    assert info.title == 'A synthetic chapter'


def test_describe_maps_a_corrupt_metadata_pb_to_internal(gcs_bucket: storage.Bucket) -> None:
    # The writer validates every envelope it writes, so bytes that do not read as one are the store's
    # own fault — INTERNAL, not an UNKNOWN unexpected-exception, and not a paper without a title.
    _seed_paper(gcs_bucket, sources=[_xml_source()], metadata=b'\xff\xff\xff')
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run_over_grpc(gcs_bucket, lambda s: s.DescribePaper(literature_pb2.DescribePaperRequest(doc_id=_DOC)))
    assert exc.value.code() is grpc.StatusCode.INTERNAL


def test_describe_titles_a_crossref_resolved_paper_by_its_crossref_title(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()], metadata=_crossref_metadata('A Crossref title'))
    info = asyncio.run(_store(gcs_bucket).describe_paper(_DOC))
    assert info.title == 'A Crossref title'


def test_describe_titles_a_whole_book_record_by_its_book_title(gcs_bucket: storage.Bucket) -> None:
    # A record for a whole book states no chapter title; the book's title is the record's own.
    metadata = _book_metadata(chapter_title=None, book_title='A synthetic review series')
    _seed_paper(gcs_bucket, sources=[_xml_source()], metadata=metadata)
    info = asyncio.run(_store(gcs_bucket).describe_paper(_DOC))
    assert info.title == 'A synthetic review series'


def test_describe_fails_on_metadata_with_no_record(gcs_bucket: storage.Bucket) -> None:
    # An envelope with no record set is a corrupt blob: the reader fails rather than reading it as a
    # paper without metadata (which would fall back to an external id and hide the corruption).
    _seed_paper(gcs_bucket, sources=[_xml_source()], metadata=litcache_pb2.PaperMetadata().SerializeToString())
    with pytest.raises(literature_backend.CorruptMetadataError, match='no record set'):
        asyncio.run(_store(gcs_bucket).describe_paper(_DOC))


def test_validate_pdf_only_paper_is_honest_not_a_false_negative(gcs_bucket: storage.Bucket) -> None:
    # No markdown rendering, and PDF validation is not implemented. The reason must not claim the quote is
    # absent "in any representation" — only markdown was (not) checkable.
    _seed_paper(gcs_bucket, sources=[_pdf_source()], markdown=None)
    result = asyncio.run(_store(gcs_bucket).validate(_DOC, _QUOTE))
    assert not result.ok
    assert 'not yet available' in result.reason
    assert 'not located' not in result.reason


def test_describe_tolerates_an_unspecified_file_role(gcs_bucket: storage.Bucket) -> None:
    files = [litcache_pb2.AssociatedFile(name='mystery.bin', path='supplementary/x.bin')]  # role unset (0)
    _seed_paper(gcs_bucket, sources=[_xml_source()], files=files)
    info = asyncio.run(_store(gcs_bucket).describe_paper(_DOC))
    (file,) = info.files
    assert file.role == literature_pb2.FILE_ROLE_UNSPECIFIED


def _no_upstream(request: httpx2.Request) -> httpx2.Response:
    """The index half's transport here: these are store tests, and a dial out is the failure."""
    raise AssertionError(f'a store test reached an upstream: {request.url}')


def _run_over_grpc[T](
    bucket: storage.Bucket, call: Callable[[literature_pb2_grpc.LiteratureAsyncStub], Awaitable[T]]
) -> T:
    """Drive one read against a real in-process server, over a resolver the read may not reach."""

    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(_no_upstream)) as http_client:
            store = litcache_store.Store(bucket, http_client)
            backend = live_mod.LiveBackend(store, discovery_mod.Indexes(http_client))
            servicer = servicer_mod.Servicer(backend)
            async with in_process_grpc.serving(
                lambda server: literature_pb2_grpc.add_LiteratureServicer_to_server(servicer, server),
                interceptors=(authz.InjectMetadata(authz.ADMITTED), authz.InjectStreamMetadata(authz.ADMITTED)),
                server_interceptors=authz.server_interceptors(),
            ) as channel:
                return await call(literature_pb2_grpc.LiteratureStub(channel))

    return asyncio.run(run())


async def _drained(
    responses: AsyncIterable[literature_pb2.ReadMarkdownResponse],
) -> list[literature_pb2.ReadMarkdownResponse]:
    return [response async for response in responses]


def _read_over_grpc(bucket: storage.Bucket, doc_id: str = _DOC) -> list[literature_pb2.ReadMarkdownResponse]:
    return _run_over_grpc(bucket, lambda s: _drained(s.ReadMarkdown(literature_pb2.ReadMarkdownRequest(doc_id=doc_id))))


def _opened(bucket: storage.Bucket) -> literature_backend.Rendering | literature_pb2.FullTextUnavailable:
    return asyncio.run(_store(bucket).read_markdown(_DOC))


def _provenance_of(bucket: storage.Bucket) -> literature_pb2.TextProvenance:
    opened = _opened(bucket)
    assert isinstance(opened, literature_backend.Rendering)
    return opened.provenance


def test_locate_pdf_is_unimplemented_over_grpc(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_pdf_source(), _xml_source()])
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run_over_grpc(
            gcs_bucket,
            lambda s: s.Locate(
                literature_pb2.LocateRequest(
                    doc_id=_DOC, quote=_QUOTE, representation=literature_pb2.REPRESENTATION_PDF
                )
            ),
        )
    assert exc.value.code() is grpc.StatusCode.UNIMPLEMENTED


def test_locate_pdf_without_a_pdf_is_failed_precondition_over_grpc(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run_over_grpc(
            gcs_bucket,
            lambda s: s.Locate(
                literature_pb2.LocateRequest(
                    doc_id=_DOC, quote=_QUOTE, representation=literature_pb2.REPRESENTATION_PDF
                )
            ),
        )
    assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION


def test_source_by_format_picks_the_newest_pdf_lineage_and_skips_revision_less() -> None:
    def _pdf(handle: str, day: int) -> litcache_pb2.Source:
        return litcache_pb2.Source(
            handle=handle,
            media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
            revisions=[litcache_pb2.Revision(hash=handle, captured_at=_ts(_CAPTURED + datetime.timedelta(days=day)))],
        )

    revisionless = litcache_pb2.Source(handle='empty', media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF)
    manifest = litcache_pb2.Manifest(sources=[_pdf('old', 0), revisionless, _pdf('new', 2), _pdf('mid', 1)])
    picked = litcache_store._source_by_format(manifest, litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF)
    assert picked is not None
    assert picked.handle == 'new'  # newest captured_at, not array order
    only_empty = litcache_pb2.Manifest(sources=[revisionless])  # a revision-less lineage alone is None, not a crash
    assert litcache_store._source_by_format(only_empty, litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF) is None


def test_locate_maps_a_missing_rendering_blob_to_internal(gcs_bucket: storage.Bucket) -> None:
    # The manifest lists a rendering hash whose `.md` blob is absent (partial write / lifecycle-deleted):
    # the store broke its own invariant, so this is INTERNAL — not an UNKNOWN unexpected-exception, and
    # not the NOT_FOUND that would file a fault as an answer about the paper.
    rendering_hash = _seed_paper(gcs_bucket)
    gcs_bucket.blob(f'papers/{_DOC}/renderings/{rendering_hash}.md').delete()
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run_over_grpc(
            gcs_bucket,
            lambda s: s.Locate(
                literature_pb2.LocateRequest(
                    doc_id=_DOC, quote=_QUOTE, representation=literature_pb2.REPRESENTATION_MARKDOWN
                )
            ),
        )
    assert exc.value.code() is grpc.StatusCode.INTERNAL


def test_read_markdown_streams_the_canonical_rendering_under_its_record_key(gcs_bucket: storage.Bucket) -> None:
    rendering_hash = _seed_paper(gcs_bucket, sources=[_xml_source()])
    responses = _read_over_grpc(gcs_bucket)
    header = responses[0].header
    assert header.rendering_key == rendering_hash
    assert header.size_bytes == len(_MARKDOWN.encode('utf-8'))
    assert header.provenance == literature_pb2.TEXT_PROVENANCE_OPEN_ACCESS
    assert b''.join(response.chunk for response in responses[1:]).decode('utf-8') == _MARKDOWN


def test_read_markdown_streams_a_rendering_larger_than_one_read_whole(gcs_bucket: storage.Bucket) -> None:
    # Past one ranged read and past one message, with multi-byte characters a read boundary can split.
    oversized = 'The channel showed markedly reduced ATP sensitivity — in vitro, ü.\n' * 40_000
    assert len(oversized.encode('utf-8')) > 2 * litcache_store._READ_BYTES
    _seed_paper(gcs_bucket, sources=[_xml_source()], markdown=oversized)
    responses = _read_over_grpc(gcs_bucket)
    assert len(responses) > 3  # the header, and more than one chunk
    assert b''.join(response.chunk for response in responses[1:]).decode('utf-8') == oversized


def test_read_markdown_reports_a_failed_conversion(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_pdf_source()], markdown=None)
    litcache_outcome.write_outcome(
        gcs_bucket,
        _DOC,
        litcache_outcome.FetchOutcome(
            kind=litcache_outcome.OutcomeKind.FAILED, at=_CAPTURED, error='ocr refused the scan'
        ),
    )
    assert _opened(gcs_bucket) == literature_pb2.FullTextUnavailable(state=literature_pb2.FULL_TEXT_STATE_FAILED)


def test_read_markdown_reports_a_paper_the_ladder_found_no_source_for(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, markdown=None)
    litcache_outcome.write_outcome(
        gcs_bucket,
        _DOC,
        litcache_outcome.FetchOutcome(kind=litcache_outcome.OutcomeKind.NO_FULL_TEXT, at=_CAPTURED),
    )
    assert _opened(gcs_bucket) == literature_pb2.FullTextUnavailable(state=literature_pb2.FULL_TEXT_STATE_NO_FULL_TEXT)


def test_read_markdown_reports_a_source_still_convertible_as_pending(gcs_bucket: storage.Bucket) -> None:
    # A PDF with no rendering and no terminal marker: the conversion can still produce one, so this
    # is not a paper without full text — a caller that gave up here would be giving up early.
    _seed_paper(gcs_bucket, sources=[_pdf_source()], markdown=None)
    responses = _read_over_grpc(gcs_bucket)
    assert [response.WhichOneof('part') for response in responses] == ['unavailable']
    assert responses[0].unavailable.state == literature_pb2.FULL_TEXT_STATE_PENDING


def test_read_markdown_for_an_unknown_paper_is_not_found_over_grpc(gcs_bucket: storage.Bucket) -> None:
    # A doc_id no manifest answers is a broken reference, never the modelled unavailable arm.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _read_over_grpc(gcs_bucket, 'no-such-doc')
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


def _drain_after(bucket: storage.Bucket, change: Callable[[storage.Blob], object]) -> bytes:
    """Open the rendering, apply `change` to its object, then read every byte the open rendering yields."""

    async def read() -> bytes:
        opened = await _store(bucket).read_markdown(_DOC)
        assert isinstance(opened, literature_backend.Rendering)
        change(bucket.blob(f'papers/{_DOC}/renderings/{opened.key}.md'))
        return b''.join([block async for block in opened.blocks])

    return asyncio.run(read())


def test_a_rendering_deleted_after_it_was_opened_is_missing_rather_than_cut_short(gcs_bucket: storage.Bucket) -> None:
    # The emulator ignores a generation precondition on a download, so the rewritten case — the same name, other
    # bytes — is not provable here; the servicer's hash check is what refuses those bytes offline.
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    with pytest.raises(literature_backend.MissingRenderingBlobError):
        _drain_after(gcs_bucket, lambda blob: blob.delete())


def test_an_empty_rendering_is_read_as_no_bytes(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()], markdown='')
    responses = _read_over_grpc(gcs_bucket)
    assert [response.WhichOneof('part') for response in responses] == ['header']
    assert responses[0].header.size_bytes == 0


def test_read_markdown_maps_a_missing_rendering_blob_to_internal(gcs_bucket: storage.Bucket) -> None:
    rendering_hash = _seed_paper(gcs_bucket)
    gcs_bucket.blob(f'papers/{_DOC}/renderings/{rendering_hash}.md').delete()
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _read_over_grpc(gcs_bucket)
    assert exc.value.code() is grpc.StatusCode.INTERNAL


def _captured_by_an_institution() -> litcache_pb2.Source:
    source = _pdf_source()
    source.access.CopyFrom(litcache_pb2.Access(institution_captured=litcache_pb2.InstitutionCaptured()))
    return source


def _uploaded_free_to_read() -> litcache_pb2.Source:
    source = _pdf_source()
    source.revisions[0].kind = litcache_pb2.SourceKind.SOURCE_KIND_UPLOAD
    return source


def _uploaded() -> litcache_pb2.Source:
    source = _uploaded_free_to_read()
    source.access.CopyFrom(litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess()))
    return source


def _publisher_licensed() -> litcache_pb2.Source:
    source = _pdf_source()
    source.access.CopyFrom(litcache_pb2.Access(licensed=litcache_pb2.Licensed(publisher='Elsevier')))
    return source


def _access_unknown() -> litcache_pb2.Source:
    source = _pdf_source()
    source.access.CopyFrom(litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess()))
    return source


@pytest.mark.parametrize(
    ('source', 'provenance'),
    [
        (_pdf_source, literature_pb2.TEXT_PROVENANCE_OPEN_ACCESS),
        (_captured_by_an_institution, literature_pb2.TEXT_PROVENANCE_SUPPLIED),
        (_uploaded, literature_pb2.TEXT_PROVENANCE_SUPPLIED),
        (_uploaded_free_to_read, literature_pb2.TEXT_PROVENANCE_OPEN_ACCESS),
        (_publisher_licensed, literature_pb2.TEXT_PROVENANCE_UNSPECIFIED),
        (_access_unknown, literature_pb2.TEXT_PROVENANCE_UNSPECIFIED),
    ],
    ids=[
        'free-to-read',
        'institution-captured',
        'uploaded-revision',
        'uploaded-free-to-read',
        'publisher-licensed',
        'access-unknown',
    ],
)
def test_provenance_states_only_what_the_lineage_supports(
    gcs_bucket: storage.Bucket,
    source: Callable[[], litcache_pb2.Source],
    provenance: literature_pb2.TextProvenance,
) -> None:
    # Free to read is OPEN_ACCESS even when uploaded; otherwise human capture or an upload, each alone,
    # is SUPPLIED. A lineage with neither signal gets no answer rather than the permissive one:
    # OPEN_ACCESS is a claim about the terms the text is held under, not a default for an unclear lineage.
    _seed_paper(gcs_bucket, sources=[source()], rendering=_rendering('pdf', litcache_pb2.Converter.CONVERTER_LLM_OCR))
    assert _provenance_of(gcs_bucket) == provenance


def test_provenance_of_a_rendering_the_manifest_cannot_place_is_unspecified(gcs_bucket: storage.Bucket) -> None:
    # A manifest that cannot say where its text came from must not be read as open access: the
    # permissive answer is the one a supplied paper would be mislabelled with.
    rendering = _rendering('pdf', litcache_pb2.Converter.CONVERTER_LLM_OCR)
    rendering.deps[:] = ['gone']
    _seed_paper(gcs_bucket, sources=[_uploaded()], rendering=rendering)
    assert _provenance_of(gcs_bucket) == literature_pb2.TEXT_PROVENANCE_UNSPECIFIED


def test_validate_aborts_on_a_missing_rendering_blob_rather_than_answering(gcs_bucket: storage.Bucket) -> None:
    # A rendering hash whose .md blob is absent is a store fault, not a verdict on the quote: answering
    # ok=false would tell an agent its citation is bad and send it to rewrite a quote that is fine.
    rendering_hash = _seed_paper(gcs_bucket)
    gcs_bucket.blob(f'papers/{_DOC}/renderings/{rendering_hash}.md').delete()
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run_over_grpc(gcs_bucket, lambda s: s.Validate(literature_pb2.ValidateRequest(doc_id=_DOC, quote=_QUOTE)))
    assert exc.value.code() is grpc.StatusCode.INTERNAL


def test_full_text_readiness_over_the_litcache_layout(gcs_bucket: storage.Bucket) -> None:
    # A paper with a markdown rendering is READY; a PDF-source paper with no rendering is PENDING; an
    # unknown doc_id is UNKNOWN_PAPER — the real GCS-derived readiness, in one batch.
    _seed_paper(gcs_bucket, sources=[_xml_source()])  # _DOC: has a rendering
    _seed_paper(gcs_bucket, doc_id=_DOC_PENDING, sources=[_pdf_source()], markdown=None)
    states = asyncio.run(_store(gcs_bucket).full_text_readiness([_DOC, _DOC_PENDING, 'no-such-doc']))
    assert states == {
        _DOC: literature_pb2.FULL_TEXT_STATE_READY,
        _DOC_PENDING: literature_pb2.FULL_TEXT_STATE_PENDING,
        'no-such-doc': literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER,
    }


@pytest.mark.parametrize(
    ('kind', 'expected'),
    [
        (litcache_outcome.OutcomeKind.FAILED, literature_pb2.FULL_TEXT_STATE_FAILED),
        (litcache_outcome.OutcomeKind.NO_FULL_TEXT, literature_pb2.FULL_TEXT_STATE_NO_FULL_TEXT),
    ],
)
def test_a_terminal_sidecar_marker_reaches_the_wire(
    gcs_bucket: storage.Bucket, kind: litcache_outcome.OutcomeKind, expected: literature_pb2.FullTextState
) -> None:
    # The sidecar is the one readiness input that the manifest cannot express, so nothing else would
    # catch a dropped mapping entry — which raises KeyError rather than reporting a terminal state.
    _seed_paper(gcs_bucket, doc_id=_DOC_TERMINAL, sources=[_pdf_source()], markdown=None)
    litcache_outcome.write_outcome(gcs_bucket, _DOC_TERMINAL, litcache_outcome.FetchOutcome(kind=kind, at=_CAPTURED))
    states = asyncio.run(_store(gcs_bucket).full_text_readiness([_DOC_TERMINAL]))
    assert states == {_DOC_TERMINAL: expected}


def test_a_paper_with_no_sources_is_pending_not_terminal(gcs_bucket: storage.Bucket) -> None:
    # Only a marker settles a paper: with no sources, no rendering and no sidecar, nothing has been
    # tried yet, so the wire must not report a terminal state.
    _seed_paper(gcs_bucket, doc_id=_DOC_TERMINAL, sources=[], markdown=None)
    states = asyncio.run(_store(gcs_bucket).full_text_readiness([_DOC_TERMINAL]))
    assert states == {_DOC_TERMINAL: literature_pb2.FULL_TEXT_STATE_PENDING}


def test_readiness_collapses_duplicate_doc_ids(gcs_bucket: storage.Bucket) -> None:
    # One readiness per distinct id, so a caller batching the same id twice pays one GCS read.
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    states = asyncio.run(_store(gcs_bucket).full_text_readiness([_DOC, _DOC]))
    assert states == {_DOC: literature_pb2.FULL_TEXT_STATE_READY}


# --- Conversion requests: what a failed enqueue is reported as ------------------------------------

_CONVERT_TARGET = enqueue.ConversionTarget(
    queue_path='projects/p/locations/r/queues/themis-convert',
    worker_url='https://themis-convert-worker-abc-ts.a.run.app',
    invoker_service_account_email='themis-convert-invoker@p.iam.gserviceaccount.com',
)


class _FakeTasksClient:
    """A ``CloudTasksClient`` stand-in: records each create, or fails it per doc_id.

    ``delays`` makes one create finish later than another, so a test can decide which failure a
    concurrent batch would otherwise have surfaced first.
    """

    def __init__(
        self,
        *,
        raises: Exception | None = None,
        per_doc: Mapping[str, Exception] | None = None,
        delays: Mapping[str, float] | None = None,
    ) -> None:
        self.names: list[str] = []
        self._raises = raises
        self._per_doc = dict(per_doc or {})
        self._delays = dict(delays or {})

    def create_task(self, *, parent: str, task: object) -> object:
        del parent
        name = str(getattr(task, 'name', ''))
        doc_id = name.rpartition('/')[2].partition('--')[0]
        time.sleep(self._delays.get(doc_id, 0.0))
        error = self._per_doc.get(doc_id, self._raises)
        if error is not None:
            raise error
        self.names.append(name)
        return task


def _conversion_backend(client: _FakeTasksClient) -> litcache_store.Store:
    """A backend whose only reachable collaborator is the tasks client — the bucket is never touched."""
    enqueuer = enqueue.Enqueuer(typing.cast('tasks_v2.CloudTasksClient', client), _CONVERT_TARGET)
    return litcache_store.Store(typing.cast('storage.Bucket', None), _NO_HTTP, enqueuer=enqueuer)


def test_a_conversion_request_names_a_main_text_task_per_distinct_paper() -> None:
    # The agent's request is the decision to spend on the paper's main text, so it names that target.
    client = _FakeTasksClient()
    asyncio.run(_conversion_backend(client).request_conversions([_DOC, _DOC_PENDING, _DOC]))
    assert sorted(client.names) == sorted(
        f'{_CONVERT_TARGET.queue_path}/tasks/{doc_id}--main_text' for doc_id in (_DOC, _DOC_PENDING)
    )


def test_an_empty_batch_reaches_no_queue() -> None:
    # Nothing to convert must not be an error even where no queue is wired, or a deployment without
    # the lane could not answer for a corpus that happens to be entirely settled.
    backend = litcache_store.Store(typing.cast('storage.Bucket', None), _NO_HTTP)
    asyncio.run(backend.request_conversions([]))


def test_an_unwired_lane_is_a_permanent_condition() -> None:
    backend = litcache_store.Store(typing.cast('storage.Bucket', None), _NO_HTTP)
    with pytest.raises(literature_backend.ConversionNotConfiguredError):
        asyncio.run(backend.request_conversions([_DOC]))


def test_a_deduped_task_is_not_a_failure() -> None:
    client = _FakeTasksClient(raises=api_exceptions.AlreadyExists('task exists'))
    asyncio.run(_conversion_backend(client).request_conversions([_DOC]))


@pytest.mark.parametrize(
    'error',
    [
        api_exceptions.ServiceUnavailable('cloud tasks is down'),
        api_exceptions.ResourceExhausted('queue create rate'),
        api_exceptions.TooManyRequests('queue create rate'),
        api_exceptions.DeadlineExceeded('create timed out'),
        api_exceptions.InternalServerError('backend error'),
        api_exceptions.Unknown('unknown'),
        api_exceptions.Aborted('conflict'),
        api_exceptions.RetryError('gave up', cause=None),
    ],
)
def test_a_failure_worth_repeating_is_reported_as_transient(error: Exception) -> None:
    # Reported as permanent, the caller would stop asking and the paper would sit PENDING until a
    # corpus scan that does not exist yet found it.
    client = _FakeTasksClient(raises=error)
    with pytest.raises(literature_backend.ConversionUnavailableError):
        asyncio.run(_conversion_backend(client).request_conversions([_DOC]))


@pytest.mark.parametrize(
    'error',
    [
        api_exceptions.PermissionDenied('no cloudtasks.enqueuer on the queue'),
        api_exceptions.Unauthenticated('no credentials'),
        api_exceptions.NotFound('no such queue'),
        api_exceptions.InvalidArgument('dispatch deadline out of range'),
        api_exceptions.FailedPrecondition('the queue is disabled'),
        api_exceptions.BadRequest('malformed task'),
    ],
)
def test_a_failure_no_retry_repairs_is_reported_as_permanent(error: Exception) -> None:
    # UNAVAILABLE is retried by gRPC's default policy, so misreporting a missing grant as transient
    # spends the caller's whole retry budget and buries the deploy that caused it.
    client = _FakeTasksClient(raises=error)
    with pytest.raises(literature_backend.ConversionEnqueueFailedError):
        asyncio.run(_conversion_backend(client).request_conversions([_DOC]))


def test_a_permanent_refusal_outranks_a_transient_one_in_the_same_batch() -> None:
    # Whichever create finished first must not decide the answer: reported transient, the caller
    # retries forever against a missing grant, and the refusal is never seen again.
    client = _FakeTasksClient(
        per_doc={
            _DOC: api_exceptions.ServiceUnavailable('cloud tasks is down'),
            _DOC_PENDING: api_exceptions.PermissionDenied('no cloudtasks.enqueuer on the queue'),
        },
        delays={_DOC_PENDING: 0.05},
    )
    with pytest.raises(literature_backend.ConversionEnqueueFailedError):
        asyncio.run(_conversion_backend(client).request_conversions([_DOC, _DOC_PENDING]))


def test_every_failure_in_a_batch_is_logged_not_only_the_one_raised(caplog: pytest.LogCaptureFixture) -> None:
    # The unraised failures are the ones that would otherwise vanish: nothing downstream sees them,
    # and the papers they belong to stay PENDING with no record of why.
    client = _FakeTasksClient(
        per_doc={
            _DOC: api_exceptions.ServiceUnavailable('cloud tasks is down'),
            _DOC_PENDING: api_exceptions.PermissionDenied('no cloudtasks.enqueuer on the queue'),
        }
    )
    with (
        caplog.at_level('INFO', logger=litcache_store.__name__),
        pytest.raises(literature_backend.ConversionEnqueueFailedError),
    ):
        asyncio.run(_conversion_backend(client).request_conversions([_DOC, _DOC_PENDING]))
    assert _DOC in caplog.text
    assert _DOC_PENDING in caplog.text


def test_a_dedup_is_countable_in_the_batch_log(caplog: pytest.LogCaptureFixture) -> None:
    # An enqueue that did not happen has to be countable above the enqueuer, not only inferable from
    # a paper that never advances.
    client = _FakeTasksClient(raises=api_exceptions.AlreadyExists('task exists'))
    with caplog.at_level('INFO', logger=litcache_store.__name__):
        asyncio.run(_conversion_backend(client).request_conversions([_DOC]))
    assert 'already queued' in caplog.text
    assert _DOC in caplog.text


def test_a_partly_failed_batch_still_places_the_tasks_it_could() -> None:
    # Repeating the call is the caller's remedy, and it must not have undone the creates that worked.
    client = _FakeTasksClient(per_doc={_DOC: api_exceptions.ServiceUnavailable('down')})
    with pytest.raises(literature_backend.ConversionUnavailableError):
        asyncio.run(_conversion_backend(client).request_conversions([_DOC, _DOC_PENDING]))
    assert client.names == [f'{_CONVERT_TARGET.queue_path}/tasks/{_DOC_PENDING}--main_text']


def test_a_failure_that_is_not_the_apis_is_still_classified() -> None:
    # A credential refresh or a bug must not escape the port unmapped, which the servicer answers
    # UNKNOWN to.
    client = _FakeTasksClient(raises=RuntimeError('token refresh failed'))
    with pytest.raises(literature_backend.ConversionEnqueueFailedError):
        asyncio.run(_conversion_backend(client).request_conversions([_DOC]))


@pytest.mark.parametrize(
    'error',
    [api_exceptions.MethodNotImplemented('not implemented'), api_exceptions.DataLoss('data loss')],
)
def test_a_server_error_no_retry_repairs_is_permanent(error: Exception) -> None:
    # Both arrive as 5xx and both are facts about the service rather than its load, so the transient
    # base they sit under must not sweep them up.
    client = _FakeTasksClient(raises=error)
    with pytest.raises(literature_backend.ConversionEnqueueFailedError):
        asyncio.run(_conversion_backend(client).request_conversions([_DOC]))


# --- Ingesting a paper the corpus does not hold ---------------------------------------------------

_FRESH_PMID = 'pmid:29089047'
_FRESH_DOI = 'doi:10.1038/s41586-020-2649-2'
_UNPUBLISHED_DOI = 'doi:10.1/nobody-published-this'


def _raising(error: Exception) -> ingest.MetadataResolver:
    """A metadata ladder that fails a given way for every id."""

    async def resolve_metadata(**_kwargs: object) -> resolve.ResolvedPaper:
        raise error

    return resolve_metadata


def _store_over(resolver: ingest.MetadataResolver) -> litcache_store.Store:
    """A store whose ladder is `resolver`; it fails before the bucket or the crosswalk is reached."""
    return litcache_store.Store(
        typing.cast('storage.Bucket', None),
        _NO_HTTP,
        connect=lambda: typing.cast('sql.Connection', None),
        resolve_metadata=resolver,
    )


@pytest.mark.parametrize(
    'error',
    [
        efetch.RecordPreconditionError('two Bookshelf accessions'),
        mirror.SchemaDriftError('crossref', 'title: not an array'),
        ingest.UnstorableRecordError('not a PaperMetadata envelope'),
    ],
    ids=['precondition', 'schema-drift', 'unstorable-record'],
)
def test_a_record_the_store_cannot_hold_is_charged_to_its_own_id(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    # Terminal for that id, so the batch around it must survive: failing whole would fail the same way
    # on every retry, and the ids beside it would never be resolved either.
    with caplog.at_level('WARNING', logger=litcache_store.__name__):
        assert asyncio.run(_store_over(_raising(error)).ingest_papers([_FRESH_PMID])) == {}
    assert _FRESH_PMID in caplog.text  # the gap is answerable from the service's own record


@pytest.mark.parametrize(
    'error',
    [
        json.JSONDecodeError('Expecting value', '', 0),
        UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte'),
    ],
    ids=['truncated-body', 'undecodable-body'],
)
def test_a_malformed_upstream_body_is_an_outage_not_a_settled_miss(error: Exception) -> None:
    # Both subclass `ValueError`, which the ladder raises on its way through `response.json()`. Read
    # as a fact about the paper they would write it off on a fault a retry clears.
    with pytest.raises(literature_backend.IngestUnavailableError):
        asyncio.run(_store_over(_raising(error)).ingest_papers([_FRESH_DOI]))


@pytest.mark.parametrize(
    'error',
    [
        # The payload a real refusal carries, read off Postgres 16 through pg8000: SQLSTATE 42501.
        pg8000.exceptions.DatabaseError(
            {'C': '42501', 'M': 'permission denied for table crosswalk', 'F': 'aclchk.c', 'R': 'aclcheck_error'}
        ),
        api_exceptions.Forbidden('themis-evidence@p.iam does not have storage.objects.create access'),
    ],
    ids=['crosswalk-insert', 'bucket-create'],
)
def test_a_write_this_deployment_holds_no_grant_for_is_a_permanent_condition(error: Exception) -> None:
    # The grants come from a migration and from the stack, so no retry provisions one. Reported as an
    # outage the caller would retry into it for as long as the deployment lacks them.
    with pytest.raises(literature_backend.IngestNotConfiguredError) as exc:
        asyncio.run(_store_over(_raising(error)).ingest_papers([_FRESH_DOI]))
    # The message reaches the caller — the browser and the sandbox agent — so it carries the condition
    # and not the refusal: no server source location, no service account, no table name.
    for detail in ('aclchk', 'crosswalk', 'themis-evidence@', 'storage.objects'):
        assert detail not in str(exc.value)


def test_a_database_error_that_is_not_a_refused_grant_stays_an_outage() -> None:
    # The match is on the one SQLSTATE: pg8000 raises `DatabaseError` for everything, and a
    # serialization failure is the transient case that must keep its retry.
    error = pg8000.exceptions.DatabaseError({'C': '40001', 'M': 'could not serialize access'})
    with pytest.raises(literature_backend.IngestUnavailableError):
        asyncio.run(_store_over(_raising(error)).ingest_papers([_FRESH_DOI]))


def test_an_upstream_that_did_not_answer_still_fails_the_whole_batch() -> None:
    # The converse, and the reason the classification above is narrow: an outage is not a paper that
    # does not exist, and reading it per-id would write the paper off for good.
    request = httpx2.Request('GET', 'https://api.crossref.org/works/x')
    error = httpx2.HTTPStatusError('boom', request=request, response=httpx2.Response(503, request=request))
    with pytest.raises(literature_backend.IngestUnavailableError):
        asyncio.run(_store_over(_raising(error)).ingest_papers([_FRESH_DOI]))


def _resolving(known: Sequence[str]) -> ingest.MetadataResolver:
    """A metadata ladder that holds `known` (as `{scheme}:{value}` keys) and nothing else."""

    async def resolve_metadata(*, pmid: str | None, doi: str | None, **_kwargs: object) -> resolve.ResolvedPaper:
        asked = f'pmid:{pmid}' if pmid is not None else f'doi:{doi}'
        if asked not in known:
            raise resolve.MetadataUnresolvedError(pmid=pmid, doi=doi)
        article = pubmed_pb2.PubmedArticle()
        article.medline_citation.article.article_title.value = f'The paper behind {asked}'
        scheme, _, value = asked.partition(':')
        return resolve.ResolvedPaper(
            # The envelope the real ladder answers with: `metadata.pb` is a `PaperMetadata`, and the
            # writer keeps nothing else. A bare record here would exercise a shape no rung produces.
            metadata=paper_metadata.to_canonical_bytes(
                litcache_pb2.PaperMetadata(pubmed=litcache_pb2.PubmedRecord(article=article))
            ),
            external_ids=litcache_pb2.ExternalIds(**{scheme: value}),
            publisher=None,
        )

    return resolve_metadata


def _ingesting_store(
    bucket: storage.Bucket,
    connect: Callable[[], pg8000.dbapi.Connection],
    *,
    known_upstream: Sequence[str],
    tasks: _FakeTasksClient | None = None,
) -> litcache_store.Store:
    """The store over a real crosswalk and bucket, with only the upstream ladder faked."""
    enqueuer = (
        enqueue.Enqueuer(typing.cast('tasks_v2.CloudTasksClient', tasks), _CONVERT_TARGET)
        if tasks is not None
        else None
    )
    return litcache_store.Store(
        bucket,
        _NO_HTTP,
        connect=connect,
        enqueuer=enqueuer,
        resolve_metadata=_resolving(known_upstream),
    )


def test_ingesting_an_id_upstream_holds_mints_it_and_commits_a_manifest(
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
    gcs_bucket: storage.Bucket,
) -> None:
    del conn  # requested for the per-test crosswalk truncation; the backend dials its own
    backend = _ingesting_store(gcs_bucket, crosswalk_connect, known_upstream=[_FRESH_PMID])
    ingested = asyncio.run(backend.ingest_papers([_FRESH_PMID]))

    doc_id = ingested[_FRESH_PMID]
    assert asyncio.run(backend.resolve_external_ids([_FRESH_PMID])) == {_FRESH_PMID: doc_id}
    assert gcs_bucket.blob(writer.manifest_path(doc_id)).exists()
    # PENDING is what the readiness plane must report for it, or nothing downstream converts it.
    assert asyncio.run(backend.full_text_readiness([doc_id])) == {doc_id: literature_pb2.FULL_TEXT_STATE_PENDING}


def test_ingesting_an_id_nobody_published_mints_nothing(
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
    gcs_bucket: storage.Bucket,
) -> None:
    del conn  # requested for the per-test crosswalk truncation; the backend dials its own
    backend = _ingesting_store(gcs_bucket, crosswalk_connect, known_upstream=[_FRESH_PMID])
    assert asyncio.run(backend.ingest_papers([_UNPUBLISHED_DOI])) == {}
    assert asyncio.run(backend.resolve_external_ids([_UNPUBLISHED_DOI])) == {}
    assert not list(gcs_bucket.list_blobs(prefix='papers/'))


def test_every_spelling_of_one_id_is_answered_with_the_paper_it_reached(
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
    gcs_bucket: storage.Bucket,
) -> None:
    del conn  # requested for the per-test crosswalk truncation; the store dials its own
    # The fold is for the upstream call, not for the answer: a caller that sent two spellings of one
    # DOI gets both back. One reported UNKNOWN_PAPER would be written off as a paper nobody published.
    store = _ingesting_store(gcs_bucket, crosswalk_connect, known_upstream=[_FRESH_DOI])
    spellings = [_FRESH_DOI, _FRESH_DOI.upper().replace('DOI:', 'doi:')]

    ingested = asyncio.run(store.ingest_papers(spellings))

    assert sorted(ingested) == sorted(spellings)
    assert len(set(ingested.values())) == 1, 'one paper, so one doc_id under both spellings'


def test_a_pmcid_miss_is_not_ingested_and_does_not_fail_the_batch(
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
    gcs_bucket: storage.Bucket,
) -> None:
    del conn  # requested for the per-test crosswalk truncation; the backend dials its own
    # The metadata ladder has no PMCID rung, so the adapter drops it rather than handing litcache an id
    # it would refuse — and the ingestable id beside it still lands.
    backend = _ingesting_store(gcs_bucket, crosswalk_connect, known_upstream=[_FRESH_PMID])
    ingested = asyncio.run(backend.ingest_papers(['pmcid:PMC7759461', _FRESH_PMID]))
    assert list(ingested) == [_FRESH_PMID]


def test_an_unreachable_upstream_is_reported_as_transient_not_as_an_absent_paper(
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
    gcs_bucket: storage.Bucket,
) -> None:
    del conn  # requested for the per-test crosswalk truncation; the backend dials its own
    request = httpx2.Request('GET', 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi')

    async def resolve_metadata(**_kwargs: object) -> resolve.ResolvedPaper:
        raise httpx2.HTTPStatusError('boom', request=request, response=httpx2.Response(503, request=request))

    backend = litcache_store.Store(gcs_bucket, _NO_HTTP, connect=crosswalk_connect, resolve_metadata=resolve_metadata)
    with pytest.raises(literature_backend.IngestUnavailableError):
        asyncio.run(backend.ingest_papers([_FRESH_DOI]))


def test_ingesting_without_a_crosswalk_is_a_permanent_condition(gcs_bucket: storage.Bucket) -> None:
    backend = litcache_store.Store(gcs_bucket, _NO_HTTP)
    with pytest.raises(literature_backend.IngestNotConfiguredError):
        asyncio.run(backend.ingest_papers([_FRESH_DOI]))


def test_an_ingested_paper_reaches_the_conversion_queue_over_grpc(
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
    gcs_bucket: storage.Bucket,
) -> None:
    """The whole lane, end to end: an id in no corpus comes back with a doc_id and a queued task."""
    del conn  # requested for the per-test crosswalk truncation; the backend dials its own
    tasks = _FakeTasksClient()
    store = _ingesting_store(gcs_bucket, crosswalk_connect, known_upstream=[_FRESH_PMID], tasks=tasks)

    async def run() -> literature_pb2.MaybeIngestPapersResponse:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(_no_upstream)) as http_client:
            servicer = servicer_mod.Servicer(live_mod.LiveBackend(store, discovery_mod.Indexes(http_client)))
            async with in_process_grpc.serving(
                lambda server: literature_pb2_grpc.add_LiteratureServicer_to_server(servicer, server),
                interceptors=(authz.InjectMetadata(authz.ADMITTED),),
                server_interceptors=authz.server_interceptors(),
            ) as channel:
                return await literature_pb2_grpc.LiteratureStub(channel).MaybeIngestPapers(
                    literature_pb2.MaybeIngestPapersRequest(external_ids=[_FRESH_PMID])
                )

    response = asyncio.run(run())
    readiness = {r.external_id: (r.doc_id, r.state) for r in response.readiness}
    doc_id, state = readiness[_FRESH_PMID]
    assert state == literature_pb2.FULL_TEXT_STATE_PENDING
    assert tasks.names == [f'{_CONVERT_TARGET.queue_path}/tasks/{doc_id}--main_text']
    assert gcs_bucket.blob(writer.manifest_path(doc_id)).exists()


def test_bytes_held_on_two_sets_of_terms_take_the_least_restrictive(gcs_bucket: storage.Bucket) -> None:
    # The same PDF captured through a library and found open access is one set of bytes: its text is open access.
    supplied = _captured_by_an_institution()
    found_open = _pdf_source()
    found_open.handle = 'pdf-oa'
    found_open.revisions[0].kind = litcache_pb2.SourceKind.SOURCE_KIND_UNPAYWALL
    _seed_paper(
        gcs_bucket,
        sources=[supplied, found_open],
        rendering=_rendering('pdf', litcache_pb2.Converter.CONVERTER_LLM_OCR),
    )
    assert _provenance_of(gcs_bucket) == literature_pb2.TEXT_PROVENANCE_OPEN_ACCESS
