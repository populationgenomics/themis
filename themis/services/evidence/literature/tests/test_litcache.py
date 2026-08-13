"""Behaviour tests for the litcache-reading literature backend over a fake-gcs-server bucket.

Docker-gated via the shared ``gcs_bucket`` fixture: the backend works against a real
``google.cloud.storage.Bucket``, so the tests seed real ``manifest.pb`` / ``metadata.pb`` /
rendering objects and exercise the same reads the deploy uses.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
from collections.abc import Awaitable, Callable, Sequence

import grpc
import grpc.aio
import pytest
from google.cloud import storage
from google.protobuf import timestamp_pb2
from pubmed_proto import pubmed_pb2

from themis.litcache import outcome
from themis.litcache.models import litcache_pb2
from themis.rpc import literature_pb2, literature_pb2_grpc
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import litcache as litcache_backend
from themis.services.evidence.literature import servicer as servicer_mod
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


def _rendering(from_source: str, converter: litcache_pb2.Converter) -> litcache_pb2.Rendering:
    return litcache_pb2.Rendering(
        from_source=from_source,
        from_revision=f'{from_source}rev',
        converter=converter,
        converter_version='0.4.0',
        created_at=_ts(_CAPTURED),
    )


def _metadata(title: str) -> bytes:
    article = pubmed_pb2.PubmedArticle()
    article.medline_citation.article.article_title.value = title
    return article.SerializeToString()


def _seed_paper(
    bucket: storage.Bucket,
    *,
    doc_id: str = _DOC,
    sources: Sequence[litcache_pb2.Source] = (),
    rendering: litcache_pb2.Rendering | None = None,
    markdown: str | None = _MARKDOWN,
    files: Sequence[litcache_pb2.AssociatedFile] = (),
    title: str | None = 'A title',
) -> str:
    """Write a paper directory into the bucket; return the markdown rendering hash (if any).

    ``title=None`` omits ``metadata.pb`` — the not-yet-resolved-metadata case.
    """
    manifest = litcache_pb2.Manifest(
        doc_id=doc_id, external_ids=litcache_pb2.ExternalIds(doi='10.1/x'), sources=list(sources), files=list(files)
    )
    rendering_hash = ''
    if markdown is not None:
        rendering_hash = hashlib.sha256(markdown.encode('utf-8')).hexdigest()
        # The manifest key is the markdown's own hash, so the caller passes just the Rendering, not a map.
        manifest.renderings[rendering_hash].CopyFrom(
            rendering or _rendering('xml', litcache_pb2.Converter.CONVERTER_LITDOWN)
        )
        bucket.blob(f'papers/{doc_id}/renderings/{rendering_hash}.md').upload_from_string(markdown)
    bucket.blob(f'papers/{doc_id}/manifest.pb').upload_from_string(manifest.SerializeToString())
    if title is not None:
        bucket.blob(f'papers/{doc_id}/metadata.pb').upload_from_string(_metadata(title))
    return rendering_hash


def _backend(bucket: storage.Bucket) -> litcache_backend.LitcacheBackend:
    return litcache_backend.LitcacheBackend(bucket)


def test_describe_prefers_markdown_when_xml_derived(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_pdf_source(), _xml_source()], title='K_ATP channel study')
    info = asyncio.run(_backend(gcs_bucket).describe_paper(_DOC))
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
    info = asyncio.run(_backend(gcs_bucket).describe_paper(_DOC))
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
    info = asyncio.run(_backend(gcs_bucket).describe_paper(_DOC))
    (file,) = info.files
    assert file.name == 'table.xlsx'
    assert file.role == literature_pb2.FILE_ROLE_SUPPLEMENTARY
    assert file.media_type == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def test_describe_unknown_paper_raises(gcs_bucket: storage.Bucket) -> None:
    with pytest.raises(literature_backend.UnknownPaperError):
        asyncio.run(_backend(gcs_bucket).describe_paper('no-such-doc'))


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
    backend = _backend(gcs_bucket)
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
    pdf = asyncio.run(_backend(gcs_bucket).resolve_content(_DOC, literature_backend.PdfContent()))
    assert pdf.gcs_uri.endswith('/sources/pdf/newest.pdf')


def test_resolve_missing_pdf_raises_missing_content(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    with pytest.raises(literature_backend.MissingContentError):
        asyncio.run(_backend(gcs_bucket).resolve_content(_DOC, literature_backend.PdfContent()))


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
        asyncio.run(_backend(gcs_bucket).resolve_content(_DOC, literature_backend.FileContent(name='big.zip')))


def test_locate_markdown_returns_offsets(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    result = asyncio.run(_backend(gcs_bucket).locate(_DOC, _QUOTE, literature_pb2.REPRESENTATION_MARKDOWN))
    assert result.WhichOneof('result') == 'offsets'
    assert _MARKDOWN[result.offsets.start : result.offsets.end] == _QUOTE


def test_locate_absent_quote_is_not_located(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    result = asyncio.run(_backend(gcs_bucket).locate(_DOC, 'never in the text', literature_pb2.REPRESENTATION_MARKDOWN))
    assert result.WhichOneof('result') == 'not_located'


def test_validate_reports_markdown_when_located(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    result = asyncio.run(_backend(gcs_bucket).validate(_DOC, _QUOTE))
    assert result.ok
    assert list(result.located_in) == [literature_pb2.REPRESENTATION_MARKDOWN]


def test_validate_unknown_doc_is_not_ok(gcs_bucket: storage.Bucket) -> None:
    result = asyncio.run(_backend(gcs_bucket).validate('no-such-doc', _QUOTE))
    assert not result.ok
    assert 'unknown doc_id' in result.reason


def test_locate_and_validate_do_not_need_metadata(gcs_bucket: storage.Bucket) -> None:
    # metadata.pb resolves only the title; locate/validate must work without it.
    _seed_paper(gcs_bucket, sources=[_xml_source()], title=None)
    backend = _backend(gcs_bucket)
    located = asyncio.run(backend.locate(_DOC, _QUOTE, literature_pb2.REPRESENTATION_MARKDOWN))
    assert located.WhichOneof('result') == 'offsets'
    assert asyncio.run(backend.validate(_DOC, _QUOTE)).ok


def test_describe_without_metadata_falls_back_to_an_external_id(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, sources=[_xml_source()], title=None)
    info = asyncio.run(_backend(gcs_bucket).describe_paper(_DOC))
    assert info.title == '10.1/x'  # the manifest's DOI, not a crash on the missing metadata


def test_validate_pdf_only_paper_is_honest_not_a_false_negative(gcs_bucket: storage.Bucket) -> None:
    # No markdown rendering, and PDF validation is not implemented. The reason must not claim the quote is
    # absent "in any representation" — only markdown was (not) checkable.
    _seed_paper(gcs_bucket, sources=[_pdf_source()], markdown=None)
    result = asyncio.run(_backend(gcs_bucket).validate(_DOC, _QUOTE))
    assert not result.ok
    assert 'not yet available' in result.reason
    assert 'not located' not in result.reason


def test_describe_tolerates_an_unspecified_file_role(gcs_bucket: storage.Bucket) -> None:
    files = [litcache_pb2.AssociatedFile(name='mystery.bin', path='supplementary/x.bin')]  # role unset (0)
    _seed_paper(gcs_bucket, sources=[_xml_source()], files=files)
    info = asyncio.run(_backend(gcs_bucket).describe_paper(_DOC))
    (file,) = info.files
    assert file.role == literature_pb2.FILE_ROLE_UNSPECIFIED


def test_select_rendering_breaks_a_converter_tie_toward_the_newer() -> None:
    older = _rendering('xml', litcache_pb2.Converter.CONVERTER_LITDOWN)
    newer = _rendering('xml', litcache_pb2.Converter.CONVERTER_LITDOWN)
    newer.created_at.CopyFrom(_ts(_CAPTURED + datetime.timedelta(days=1)))
    manifest = litcache_pb2.Manifest(doc_id=_DOC)
    manifest.renderings['older-hash'].CopyFrom(older)
    manifest.renderings['newer-hash'].CopyFrom(newer)
    selected = litcache_backend._select_rendering(manifest)
    assert selected is not None
    assert selected[0] == 'newer-hash'


def _run_over_grpc[T](bucket: storage.Bucket, call: Callable[[literature_pb2_grpc.LiteratureStub], Awaitable[T]]) -> T:
    async def run() -> T:
        servicer = servicer_mod.Servicer(_backend(bucket))
        async with in_process_grpc.serving(
            lambda server: literature_pb2_grpc.add_LiteratureServicer_to_server(servicer, server)
        ) as channel:
            return await call(literature_pb2_grpc.LiteratureStub(channel))

    return asyncio.run(run())


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
    picked = litcache_backend._source_by_format(manifest, litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF)
    assert picked is not None
    assert picked.handle == 'new'  # newest captured_at, not array order
    only_empty = litcache_pb2.Manifest(sources=[revisionless])  # a revision-less lineage alone is None, not a crash
    assert litcache_backend._source_by_format(only_empty, litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF) is None


def test_locate_maps_a_missing_rendering_blob_to_not_found(gcs_bucket: storage.Bucket) -> None:
    # The manifest lists a rendering hash whose `.md` blob is absent (partial write / lifecycle-deleted):
    # MissingContentError must surface as NOT_FOUND, not an UNKNOWN unexpected-exception.
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
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


def test_validate_reports_a_missing_rendering_blob_as_a_fault(gcs_bucket: storage.Bucket) -> None:
    # A rendering hash whose .md blob is absent is a corpus fault, distinct from the benign no-rendering case.
    rendering_hash = _seed_paper(gcs_bucket)
    gcs_bucket.blob(f'papers/{_DOC}/renderings/{rendering_hash}.md').delete()
    result = asyncio.run(_backend(gcs_bucket).validate(_DOC, _QUOTE))
    assert result.ok is False
    assert 'missing' in result.reason


def test_full_text_readiness_over_the_litcache_layout(gcs_bucket: storage.Bucket) -> None:
    # A paper with a markdown rendering is READY; a PDF-source paper with no rendering is PENDING; an
    # unknown doc_id is UNKNOWN_PAPER — the real GCS-derived readiness, in one batch.
    _seed_paper(gcs_bucket, sources=[_xml_source()])  # _DOC: has a rendering
    _seed_paper(gcs_bucket, doc_id=_DOC_PENDING, sources=[_pdf_source()], markdown=None)
    states = asyncio.run(_backend(gcs_bucket).full_text_readiness([_DOC, _DOC_PENDING, 'no-such-doc']))
    assert states == {
        _DOC: literature_pb2.FULL_TEXT_STATE_READY,
        _DOC_PENDING: literature_pb2.FULL_TEXT_STATE_PENDING,
        'no-such-doc': literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER,
    }


@pytest.mark.parametrize(
    ('kind', 'expected'),
    [
        (outcome.OutcomeKind.FAILED, literature_pb2.FULL_TEXT_STATE_FAILED),
        (outcome.OutcomeKind.NO_FULL_TEXT, literature_pb2.FULL_TEXT_STATE_NO_FULL_TEXT),
    ],
)
def test_a_terminal_sidecar_marker_reaches_the_wire(
    gcs_bucket: storage.Bucket, kind: outcome.OutcomeKind, expected: literature_pb2.FullTextState
) -> None:
    # The sidecar is the one readiness input that the manifest cannot express, so nothing else would
    # catch a dropped mapping entry — which raises KeyError rather than reporting a terminal state.
    _seed_paper(gcs_bucket, doc_id=_DOC_TERMINAL, sources=[_pdf_source()], markdown=None)
    outcome.write_outcome(gcs_bucket, _DOC_TERMINAL, outcome.FetchOutcome(kind=kind, at=_CAPTURED))
    states = asyncio.run(_backend(gcs_bucket).full_text_readiness([_DOC_TERMINAL]))
    assert states == {_DOC_TERMINAL: expected}


def test_a_paper_no_source_served_is_no_full_text(gcs_bucket: storage.Bucket) -> None:
    # No sources and no rendering: settled with nothing to convert, without needing a sidecar.
    _seed_paper(gcs_bucket, doc_id=_DOC_TERMINAL, sources=[], markdown=None)
    states = asyncio.run(_backend(gcs_bucket).full_text_readiness([_DOC_TERMINAL]))
    assert states == {_DOC_TERMINAL: literature_pb2.FULL_TEXT_STATE_NO_FULL_TEXT}


def test_readiness_collapses_duplicate_doc_ids(gcs_bucket: storage.Bucket) -> None:
    # One readiness per distinct id, so a caller batching the same id twice pays one GCS read.
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    states = asyncio.run(_backend(gcs_bucket).full_text_readiness([_DOC, _DOC]))
    assert states == {_DOC: literature_pb2.FULL_TEXT_STATE_READY}


def test_await_returns_immediately_when_nothing_is_pending(gcs_bucket: storage.Bucket) -> None:
    # A settled id (READY) short-circuits the poll: with a poll interval far longer than the wrapping
    # timeout, only a first-probe-then-return can finish inside 1s — a loop that always sleeps cannot.
    _seed_paper(gcs_bucket, sources=[_xml_source()])
    states = asyncio.run(
        asyncio.wait_for(
            _backend(gcs_bucket).await_full_text_readiness([_DOC], 3600.0, poll_interval_seconds=3600.0), timeout=1.0
        )
    )
    assert states == {_DOC: literature_pb2.FULL_TEXT_STATE_READY}


def test_await_times_out_while_a_paper_stays_pending(gcs_bucket: storage.Bucket) -> None:
    _seed_paper(gcs_bucket, doc_id=_DOC_PENDING, sources=[_pdf_source()], markdown=None)
    states = asyncio.run(
        asyncio.wait_for(
            _backend(gcs_bucket).await_full_text_readiness([_DOC_PENDING], 0.05, poll_interval_seconds=0.02),
            timeout=1.0,
        )
    )
    assert states == {_DOC_PENDING: literature_pb2.FULL_TEXT_STATE_PENDING}


def test_await_observes_a_pending_to_ready_transition(gcs_bucket: storage.Bucket) -> None:
    # The poll picks up a rendering written by another worker mid-wait: it starts PENDING, a concurrent
    # task adds the rendering, and the await returns READY well inside its deadline.
    _seed_paper(gcs_bucket, doc_id=_DOC_PENDING, sources=[_pdf_source()], markdown=None)
    backend = _backend(gcs_bucket)

    async def scenario() -> dict[str, literature_pb2.FullTextState]:
        async def convert() -> None:
            await asyncio.sleep(0.05)
            await asyncio.to_thread(_seed_paper, gcs_bucket, doc_id=_DOC_PENDING, sources=[_pdf_source()])

        states, _ = await asyncio.gather(
            backend.await_full_text_readiness([_DOC_PENDING], 2.0, poll_interval_seconds=0.02), convert()
        )
        return states

    assert asyncio.run(scenario()) == {_DOC_PENDING: literature_pb2.FULL_TEXT_STATE_READY}
