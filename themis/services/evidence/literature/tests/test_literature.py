"""Behaviour tests for the literature servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import typing
from collections.abc import Awaitable, Callable, Mapping, Sequence

import grpc
import grpc.aio
import pytest
from google.protobuf import duration_pb2

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


def _run[T](
    stub_call: Callable[[literature_pb2_grpc.LiteratureStub], Awaitable[T]],
    *,
    backend: literature_backend.LiteratureBackend | None = None,
) -> T:
    """Drive one call against a real in-process server + stub over the SEED corpus."""

    async def run() -> T:
        servicer = servicer_mod.Servicer(backend or literature_backend.FixtureBackend(SEED))
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


def _readiness(response: literature_pb2.EnsureFullTextResponse) -> dict[str, literature_pb2.FullTextState]:
    return {r.doc_id: r.state for r in response.readiness}


def test_ensure_full_text_reports_per_id_state() -> None:
    # DOC_XML has a markdown rendering (READY); DOC_OCR has only a PDF, no rendering (PENDING);
    # an unknown id is UNKNOWN_PAPER — all in one batch, no whole-call abort.
    response = _run(
        lambda s: s.EnsureFullText(literature_pb2.EnsureFullTextRequest(doc_ids=[DOC_XML, DOC_OCR, 'nope']))
    )
    assert _readiness(response) == {
        DOC_XML: literature_pb2.FULL_TEXT_STATE_READY,
        DOC_OCR: literature_pb2.FULL_TEXT_STATE_PENDING,
        'nope': literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER,
    }


def test_ensure_full_text_of_an_empty_batch_is_empty() -> None:
    response = _run(lambda s: s.EnsureFullText(literature_pb2.EnsureFullTextRequest(doc_ids=[])))
    assert list(response.readiness) == []


def _await_readiness(response: literature_pb2.AwaitFullTextResponse) -> dict[str, literature_pb2.FullTextState]:
    return {r.doc_id: r.state for r in response.readiness}


def test_await_full_text_returns_settled_states_over_grpc() -> None:
    # The wire mapping: a batch of already-settled ids (DOC_XML READY, 'nope' UNKNOWN_PAPER) round-trips
    # to the correct per-id readiness. Promptness of the settle short-circuit is guarded at the backend
    # level (test_litcache.test_await_returns_immediately_when_nothing_is_pending); a small timeout here
    # so a settle-predicate regression fails fast instead of running to the deadline.
    response = _run(
        lambda s: s.AwaitFullText(
            literature_pb2.AwaitFullTextRequest(
                doc_ids=[DOC_XML, 'nope'], timeout=duration_pb2.Duration(nanos=50_000_000)
            )
        )
    )
    assert _await_readiness(response) == {
        DOC_XML: literature_pb2.FULL_TEXT_STATE_READY,
        'nope': literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER,
    }


def test_await_full_text_times_out_returning_the_pending_state() -> None:
    # DOC_OCR is PENDING and the fixture has no conversion queue to advance it, so the wait runs to the
    # (short) deadline and reports the still-PENDING state rather than blocking forever.
    response = _run(
        lambda s: s.AwaitFullText(
            literature_pb2.AwaitFullTextRequest(doc_ids=[DOC_OCR], timeout=duration_pb2.Duration(nanos=50_000_000))
        )
    )
    assert _await_readiness(response) == {DOC_OCR: literature_pb2.FULL_TEXT_STATE_PENDING}


def test_await_full_text_without_a_timeout_is_invalid_argument() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.AwaitFullText(literature_pb2.AwaitFullTextRequest(doc_ids=[DOC_XML])))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_await_full_text_with_a_negative_timeout_is_invalid_argument() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.AwaitFullText(
                literature_pb2.AwaitFullTextRequest(doc_ids=[DOC_XML], timeout=duration_pb2.Duration(seconds=-1))
            )
        )
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.parametrize('rpc', ['EnsureFullText', 'AwaitFullText'])
def test_an_oversized_batch_is_invalid_argument(rpc: str) -> None:
    # The batch bound is server-side: one request must not be able to occupy the shared thread
    # executor for an unbounded number of reads, whatever the caller asks for.
    doc_ids = [f'doc-{i}' for i in range(servicer_mod._MAX_DOC_IDS + 1)]
    requests = {
        'EnsureFullText': literature_pb2.EnsureFullTextRequest(doc_ids=doc_ids),
        'AwaitFullText': literature_pb2.AwaitFullTextRequest(doc_ids=doc_ids, timeout=duration_pb2.Duration(nanos=1)),
    }
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s, rpc=rpc: getattr(s, rpc)(requests[rpc]))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


class _RecordingWaitBackend(literature_backend.FixtureBackend):
    """Records the wait the servicer asked for, so the server-side clamp can be asserted."""

    def __init__(self, papers: Mapping[str, literature_backend.SeededPaper]) -> None:
        super().__init__(papers)
        self.waits: list[float] = []

    async def await_full_text_readiness(
        self, doc_ids: Sequence[str], timeout_seconds: float, **kwargs: object
    ) -> dict[str, literature_pb2.FullTextState]:
        del kwargs
        self.waits.append(timeout_seconds)
        return await super().full_text_readiness(doc_ids)


def test_an_out_of_range_timeout_is_clamped_not_an_internal_error() -> None:
    # Duration.seconds is an unvalidated int64 on the wire, so a client can send a value that
    # overflows timedelta. That must clamp like any other over-long wait, not surface as UNKNOWN.
    backend = _RecordingWaitBackend(SEED)
    response = _run(
        lambda s: s.AwaitFullText(
            literature_pb2.AwaitFullTextRequest(doc_ids=[DOC_XML], timeout=duration_pb2.Duration(seconds=2**62))
        ),
        backend=backend,
    )
    assert _await_readiness(response) == {DOC_XML: literature_pb2.FULL_TEXT_STATE_READY}
    assert backend.waits == [servicer_mod._MAX_AWAIT_SECONDS]


def test_a_wait_longer_than_the_ceiling_is_clamped() -> None:
    # An over-long wait is served, not rejected — clamped to the ceiling, so the caller gets its
    # readiness back and calls again rather than holding a serving slot for the duration it named.
    backend = _RecordingWaitBackend(SEED)
    response = _run(
        lambda s: s.AwaitFullText(
            literature_pb2.AwaitFullTextRequest(doc_ids=[DOC_XML], timeout=duration_pb2.Duration(seconds=86_400))
        ),
        backend=backend,
    )
    assert _await_readiness(response) == {DOC_XML: literature_pb2.FULL_TEXT_STATE_READY}
    assert backend.waits == [servicer_mod._MAX_AWAIT_SECONDS]


class _CountingBackend(literature_backend.FixtureBackend):
    """Records the ids each readiness poll asks for, so the poll set can be asserted."""

    def __init__(self, papers: Mapping[str, literature_backend.SeededPaper]) -> None:
        super().__init__(papers)
        self.polls: list[list[str]] = []

    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        self.polls.append(list(doc_ids))
        return await super().full_text_readiness(doc_ids)


def test_await_stops_polling_an_id_once_it_has_settled() -> None:
    # A poll costs a read per id, so a settled id must drop out of the poll set; only DOC_OCR
    # (PENDING, never advanced by the fixture) is asked for again.
    backend = _CountingBackend(SEED)

    states = asyncio.run(
        backend.await_full_text_readiness([DOC_XML, DOC_OCR, 'nope'], timeout_seconds=0.05, poll_interval_seconds=0.01)
    )

    assert states == {
        DOC_XML: literature_pb2.FULL_TEXT_STATE_READY,
        'nope': literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER,
        DOC_OCR: literature_pb2.FULL_TEXT_STATE_PENDING,
    }
    assert backend.polls[0] == [DOC_XML, DOC_OCR, 'nope']
    assert len(backend.polls) > 1  # it did keep waiting on the pending id
    assert all(poll == [DOC_OCR] for poll in backend.polls[1:])


def test_await_collapses_duplicate_doc_ids() -> None:
    # The response carries one readiness per distinct id, and a duplicate is not read twice.
    backend = _CountingBackend(SEED)

    states = asyncio.run(
        backend.await_full_text_readiness([DOC_XML, DOC_XML], timeout_seconds=0.05, poll_interval_seconds=0.01)
    )

    assert states == {DOC_XML: literature_pb2.FULL_TEXT_STATE_READY}
    assert backend.polls == [[DOC_XML]]
