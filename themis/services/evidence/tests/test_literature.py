"""Behaviour tests for the literature servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import typing
from collections.abc import Awaitable, Callable, Mapping

import grpc
import grpc.aio
import pytest

from themis.rpc import literature_pb2, literature_pb2_grpc
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import servicer as servicer_mod
from themis.testing import in_process_grpc

DOC_XML = 'doc-xml'  # a source-XML-derived markdown rendering + a PDF
DOC_OCR = 'doc-ocr'  # a PDF whose only rendering is a lossy OCR (no markdown)
QUOTE_MD = 'a quote locatable in the markdown'
QUOTE_PDF = 'a quote locatable in the pdf'

SEED: Mapping[str, literature_backend.SeededPaper] = {
    DOC_XML: literature_backend.SeededPaper(
        title='An XML-derived paper',
        files=(
            literature_backend.SeededFile(
                name='figure1.png',
                role=literature_pb2.FILE_ROLE_FIGURE,
                media_type='image/png',
                gcs_uri='gs://corpus/doc-xml/figure1.png',
            ),
            literature_backend.SeededFile(
                name='supp.xlsx',
                role=literature_pb2.FILE_ROLE_SUPPLEMENTARY,
                media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                gcs_uri='gs://corpus/doc-xml/supp.xlsx',
            ),
        ),
        markdown_gcs_uri='gs://corpus/doc-xml/rendering.md',
        markdown_from_xml=True,
        pdf_gcs_uri='gs://corpus/doc-xml/doc.pdf',
        markdown_locations={QUOTE_MD: (10, 42)},
        pdf_locations={QUOTE_PDF: literature_backend.SeededPdfLocation(page=1, rects=((0.1, 0.2, 0.3, 0.02),))},
    ),
    DOC_OCR: literature_backend.SeededPaper(
        title='A scan-only paper',
        pdf_gcs_uri='gs://corpus/doc-ocr/doc.pdf',
        pdf_locations={QUOTE_PDF: literature_backend.SeededPdfLocation(page=0, rects=((0.0, 0.0, 0.5, 0.05),))},
    ),
}


def _run[T](stub_call: Callable[[literature_pb2_grpc.LiteratureStub], Awaitable[T]]) -> T:
    """Drive one call against a real in-process server + stub over the SEED corpus."""

    async def run() -> T:
        servicer = servicer_mod.Servicer(literature_backend.FixtureBackend(SEED))
        async with in_process_grpc.serving(
            lambda server: literature_pb2_grpc.add_LiteratureServicer_to_server(servicer, server)
        ) as channel:
            return await stub_call(literature_pb2_grpc.LiteratureStub(channel))

    return asyncio.run(run())


def test_describe_paper_prefers_markdown_when_xml_derived() -> None:
    info = _run(lambda s: s.DescribePaper(literature_pb2.DescribePaperRequest(doc_id=DOC_XML)))
    assert info.has_markdown
    assert info.has_pdf
    assert info.markdown_from_xml
    assert info.default_representation == literature_pb2.REPRESENTATION_MARKDOWN
    assert {(f.name, f.role) for f in info.files} == {
        ('figure1.png', literature_pb2.FILE_ROLE_FIGURE),
        ('supp.xlsx', literature_pb2.FILE_ROLE_SUPPLEMENTARY),
    }


def test_describe_paper_defaults_to_pdf_when_ocr_only() -> None:
    info = _run(lambda s: s.DescribePaper(literature_pb2.DescribePaperRequest(doc_id=DOC_OCR)))
    assert info.has_pdf
    assert not info.has_markdown
    assert info.default_representation == literature_pb2.REPRESENTATION_PDF


def test_describe_unknown_paper_is_not_found() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.DescribePaper(literature_pb2.DescribePaperRequest(doc_id='nope')))
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


def test_resolve_content_names_each_object() -> None:
    md = _run(
        lambda s: s.ResolveContent(
            literature_pb2.ResolveContentRequest(doc_id=DOC_XML, markdown=literature_pb2.MarkdownSelector())
        )
    )
    assert md.gcs_uri == 'gs://corpus/doc-xml/rendering.md'
    assert md.media_type == 'text/markdown'
    pdf = _run(
        lambda s: s.ResolveContent(
            literature_pb2.ResolveContentRequest(doc_id=DOC_XML, pdf=literature_pb2.PdfSelector())
        )
    )
    assert pdf.gcs_uri == 'gs://corpus/doc-xml/doc.pdf'
    assert pdf.media_type == 'application/pdf'
    fig = _run(
        lambda s: s.ResolveContent(
            literature_pb2.ResolveContentRequest(doc_id=DOC_XML, file=literature_pb2.FileSelector(name='figure1.png'))
        )
    )
    assert fig.gcs_uri == 'gs://corpus/doc-xml/figure1.png'
    assert fig.media_type == 'image/png'


def test_resolve_content_without_a_selector_is_invalid_argument() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.ResolveContent(literature_pb2.ResolveContentRequest(doc_id=DOC_XML)))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_resolve_content_for_a_missing_object_is_not_found() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.ResolveContent(
                literature_pb2.ResolveContentRequest(doc_id=DOC_OCR, markdown=literature_pb2.MarkdownSelector())
            )
        )
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


def test_locate_markdown_returns_offsets() -> None:
    result = _run(
        lambda s: s.Locate(
            literature_pb2.LocateRequest(
                doc_id=DOC_XML, quote=QUOTE_MD, representation=literature_pb2.REPRESENTATION_MARKDOWN
            )
        )
    )
    assert result.WhichOneof('result') == 'offsets'
    assert (result.offsets.start, result.offsets.end) == (10, 42)


def test_locate_pdf_returns_a_region() -> None:
    result = _run(
        lambda s: s.Locate(
            literature_pb2.LocateRequest(
                doc_id=DOC_XML, quote=QUOTE_PDF, representation=literature_pb2.REPRESENTATION_PDF
            )
        )
    )
    assert result.WhichOneof('result') == 'region'
    assert result.region.page == 1
    assert len(result.region.rects) == 1


def test_locate_absent_quote_is_not_located_not_an_error() -> None:
    result = _run(
        lambda s: s.Locate(
            literature_pb2.LocateRequest(
                doc_id=DOC_XML, quote='never written', representation=literature_pb2.REPRESENTATION_MARKDOWN
            )
        )
    )
    assert result.WhichOneof('result') == 'not_located'


def test_locate_without_a_representation_is_invalid_argument() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.Locate(literature_pb2.LocateRequest(doc_id=DOC_XML, quote=QUOTE_MD)))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_locate_in_an_out_of_range_representation_is_invalid_argument() -> None:
    # Representation is a proto3 open enum: an unknown int on the wire must be a client error, not
    # an unhandled backend ValueError surfacing as UNKNOWN.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.Locate(
                literature_pb2.LocateRequest(
                    doc_id=DOC_XML,
                    quote=QUOTE_MD,
                    representation=typing.cast('literature_pb2.Representation', 99),
                )
            )
        )
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_locate_in_an_unavailable_representation_is_failed_precondition() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.Locate(
                literature_pb2.LocateRequest(
                    doc_id=DOC_OCR, quote=QUOTE_PDF, representation=literature_pb2.REPRESENTATION_MARKDOWN
                )
            )
        )
    assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION


def test_validate_reports_the_representations_a_quote_locates_in() -> None:
    ok = _run(lambda s: s.Validate(literature_pb2.ValidateRequest(doc_id=DOC_XML, quote=QUOTE_MD)))
    assert ok.ok
    assert list(ok.located_in) == [literature_pb2.REPRESENTATION_MARKDOWN]


def test_validate_unknown_doc_is_not_ok_with_a_reason() -> None:
    result = _run(lambda s: s.Validate(literature_pb2.ValidateRequest(doc_id='nope', quote=QUOTE_MD)))
    assert not result.ok
    assert 'unknown doc_id' in result.reason


def test_validate_absent_quote_is_not_ok_with_a_reason() -> None:
    result = _run(lambda s: s.Validate(literature_pb2.ValidateRequest(doc_id=DOC_XML, quote='never written')))
    assert not result.ok
    assert result.reason
