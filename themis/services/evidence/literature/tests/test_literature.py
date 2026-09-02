"""Behaviour tests for the literature servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import typing
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import override

import grpc
import grpc.aio
import pytest

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, literature_pb2, literature_pb2_grpc
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import servicer as servicer_mod
from themis.testing import in_process_grpc

_GOOD_TOKEN = (('x-themis-session-token', 'good'),)
_BAD_TOKEN = (('x-themis-session-token', 'bad'),)

DOC_XML = 'doc-xml'  # a source-XML-derived markdown rendering + a PDF
DOC_OCR = 'doc-ocr'  # a PDF whose only rendering is a lossy OCR (no markdown)
DOC_BARE = 'doc-bare'  # claimed, nothing fetched yet — neither representation
DOI_XML = 'doi:10.1/xml'
PMID_XML = 'pmid:12345'
DOI_OCR = 'doi:10.1/ocr'
QUOTE_MD = 'a quote locatable in the markdown'
QUOTE_PDF = 'a quote locatable in the pdf'

SEED: Mapping[str, literature_backend.SeededPaper] = {
    DOC_XML: literature_backend.SeededPaper(
        title='An XML-derived paper',
        external_ids=(DOI_XML, PMID_XML),  # one paper, two ids — the ordinary crosswalk case
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
    DOC_BARE: literature_backend.SeededPaper(title='A freshly-minted paper'),
    DOC_OCR: literature_backend.SeededPaper(
        title='A scan-only paper',
        external_ids=(DOI_OCR,),
        pdf_gcs_uri='gs://corpus/doc-ocr/doc.pdf',
        pdf_locations={QUOTE_PDF: literature_backend.SeededPdfLocation(page=0, rects=((0.0, 0.0, 0.5, 0.05),))},
    ),
}


async def _session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='proj', analysis_id='ana')
    raise session_mod.UnresolvedSessionError


def _run[T](
    stub_call: Callable[[literature_pb2_grpc.LiteratureAsyncStub], Awaitable[T]],
    *,
    backend: literature_backend.LiteratureBackend | None = None,
) -> T:
    """Drive one call against a real in-process server + stub over the SEED corpus.

    The stub attaches no session token unless `stub_call` asks for one, so a call driven through here
    reaches the servicer unauthorized by default — which is what a read here is entitled to be.
    """

    async def run() -> T:
        servicer = servicer_mod.Servicer(  # pyright: ignore[reportAbstractUsage]
            backend or literature_backend.FixtureBackend(SEED), _session_resolver
        )
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


def _readiness(response: literature_pb2.PollFullTextsResponse) -> dict[str, literature_pb2.FullTextState]:
    return {r.doc_id: r.state for r in response.readiness}


def test_poll_full_texts_reports_per_id_state() -> None:
    # DOC_XML has a markdown rendering (READY); DOC_OCR has only a PDF and DOC_BARE has nothing at all,
    # and both are PENDING — the seed models no terminal marker, and only a marker settles a paper. An
    # unknown id is UNKNOWN_PAPER. All in one batch, no whole-call abort.
    response = _run(
        lambda s: s.PollFullTexts(literature_pb2.PollFullTextsRequest(doc_ids=[DOC_XML, DOC_OCR, DOC_BARE, 'nope']))
    )
    assert _readiness(response) == {
        DOC_XML: literature_pb2.FULL_TEXT_STATE_READY,
        DOC_OCR: literature_pb2.FULL_TEXT_STATE_PENDING,
        DOC_BARE: literature_pb2.FULL_TEXT_STATE_PENDING,
        'nope': literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER,
    }


def test_poll_full_texts_of_an_empty_batch_is_empty() -> None:
    response = _run(lambda s: s.PollFullTexts(literature_pb2.PollFullTextsRequest(doc_ids=[])))
    assert list(response.readiness) == []


def test_poll_full_texts_collapses_duplicate_doc_ids() -> None:
    # One readiness per distinct id: the response is keyed by doc_id, so a caller batching the same
    # id twice pays one read and gets one entry.
    response = _run(lambda s: s.PollFullTexts(literature_pb2.PollFullTextsRequest(doc_ids=[DOC_XML, DOC_XML])))
    assert _readiness(response) == {DOC_XML: literature_pb2.FULL_TEXT_STATE_READY}


def test_an_oversized_batch_is_invalid_argument() -> None:
    # The batch bound is server-side: one request must not be able to occupy the shared thread
    # executor for an unbounded number of reads, whatever the caller asks for.
    doc_ids = [f'doc-{i}' for i in range(servicer_mod._MAX_DOC_IDS + 1)]
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.PollFullTexts(literature_pb2.PollFullTextsRequest(doc_ids=doc_ids)))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def _paper_readiness(
    response: literature_pb2.MaybeIngestPapersResponse,
) -> dict[str, tuple[str, literature_pb2.FullTextState]]:
    return {r.external_id: (r.doc_id, r.state) for r in response.readiness}


def test_maybe_ingest_papers_resolves_ids_to_papers_and_readiness() -> None:
    # A token, because the batch holds a PENDING paper: seeing PENDING in the response means the
    # conversion for it was asked for, and that step is gated.
    response = _run(
        lambda s: s.MaybeIngestPapers(
            literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML, DOI_OCR]), metadata=_GOOD_TOKEN
        )
    )
    assert _paper_readiness(response) == {
        DOI_XML: (DOC_XML, literature_pb2.FULL_TEXT_STATE_READY),
        DOI_OCR: (DOC_OCR, literature_pb2.FULL_TEXT_STATE_PENDING),
    }


def test_two_ids_naming_one_paper_both_report_the_shared_doc_id() -> None:
    # A DOI and its PMID are one crosswalk row each and one paper; the response is keyed by
    # external_id, so both entries appear rather than collapsing to whichever was asked first.
    response = _run(
        lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML, PMID_XML]))
    )
    assert _paper_readiness(response) == {
        DOI_XML: (DOC_XML, literature_pb2.FULL_TEXT_STATE_READY),
        PMID_XML: (DOC_XML, literature_pb2.FULL_TEXT_STATE_READY),
    }


def test_a_doi_resolves_under_any_spelling() -> None:
    # DOI names are case-insensitive by specification and the crosswalk holds them folded, so the
    # fixture must answer a mixed-case spelling exactly as the litcache backend does — a caller that
    # develops against the fixture would otherwise see UNKNOWN_PAPER where production returns a hit.
    spelled = DOI_XML.upper().replace('DOI:', 'doi:')
    response = _run(lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[spelled])))
    assert _paper_readiness(response) == {spelled: (DOC_XML, literature_pb2.FULL_TEXT_STATE_READY)}


def test_an_id_the_corpus_does_not_know_is_an_empty_doc_id() -> None:
    # A miss is per-id and modelled: an empty doc_id with UNKNOWN_PAPER, never a call-level error and
    # never a minted doc_id that would name no manifest.
    response = _run(
        lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=['doi:10.1/never-seen']))
    )
    assert _paper_readiness(response) == {'doi:10.1/never-seen': ('', literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER)}


@pytest.mark.parametrize(
    'external_id',
    ['10.1/xml', '12345', 'isbn:9780000000000', '', 'doi', 'doi:', ':10.1/xml'],
)
def test_an_unqualified_external_id_is_invalid_argument(external_id: str) -> None:
    # Guessing a scheme resolves to another paper when it guesses wrong, so the boundary rejects. A
    # bare scheme or an empty value is rejected here too: reaching the crosswalk it would miss and come
    # back as UNKNOWN_PAPER, reporting a malformed request as a settled fact about the corpus.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[external_id])))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_an_oversized_external_id_batch_is_invalid_argument() -> None:
    external_ids = [f'doi:10.1/{i}' for i in range(servicer_mod._MAX_EXTERNAL_IDS + 1)]
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=external_ids)))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


class _UnreachableCrosswalkBackend(literature_backend.FixtureBackend):
    """A backend whose crosswalk is down, to pin the whole-batch failure mapping."""

    @override
    async def resolve_external_ids(self, external_ids: Sequence[str]) -> dict[str, str]:
        del external_ids
        raise literature_backend.CrosswalkUnavailableError('connection refused')


class _NoCrosswalkBackend(literature_backend.FixtureBackend):
    """A backend deployed without a crosswalk, to pin the permanent-condition mapping."""

    @override
    async def resolve_external_ids(self, external_ids: Sequence[str]) -> dict[str, str]:
        del external_ids
        raise literature_backend.CrosswalkNotConfiguredError('no crosswalk is configured')


def test_an_unreachable_crosswalk_is_unavailable_for_the_whole_call() -> None:
    # Not a per-id empty doc_id: a caller reading an outage per-id would write every one of these
    # papers off as absent from the corpus, which is the transient-as-terminal trap.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML])),
            backend=_UnreachableCrosswalkBackend(SEED),
        )
    assert exc.value.code() is grpc.StatusCode.UNAVAILABLE


def test_an_unconfigured_crosswalk_is_failed_precondition_not_unavailable() -> None:
    # A deployment wiring no crosswalk can never answer this call, and UNAVAILABLE is retried by
    # gRPC's default policy — so the caller would spend its whole retry budget on a permanent fact.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML])),
            backend=_NoCrosswalkBackend(SEED),
        )
    assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION


class _RecordingBackend(literature_backend.FixtureBackend):
    """A backend that records which papers a conversion was asked for."""

    def __init__(self, papers: Mapping[str, literature_backend.SeededPaper]) -> None:
        super().__init__(papers)
        self.requested: list[list[str]] = []

    @override
    async def request_conversions(self, doc_ids: Sequence[str]) -> None:
        self.requested.append(list(doc_ids))


class _FailingConversionBackend(literature_backend.FixtureBackend):
    """A backend whose conversion lane fails a given way, to pin the status mapping."""

    def __init__(self, papers: Mapping[str, literature_backend.SeededPaper], error: Exception) -> None:
        super().__init__(papers)
        self._error = error

    @override
    async def request_conversions(self, doc_ids: Sequence[str]) -> None:
        del doc_ids
        raise self._error


def test_maybe_ingest_papers_asks_for_a_conversion_of_the_pending_papers_only() -> None:
    # READY needs nothing, and an id the corpus does not know has no manifest for a producer to read
    # and no task to name — asking for either would be a conversion nothing could perform.
    backend = _RecordingBackend(SEED)
    _run(
        lambda s: s.MaybeIngestPapers(
            literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML, DOI_OCR, 'doi:10.1/never-seen']),
            metadata=_GOOD_TOKEN,
        ),
        backend=backend,
    )
    assert backend.requested == [[DOC_OCR]]


def test_maybe_ingest_papers_asks_for_nothing_when_every_paper_is_settled() -> None:
    # A batch with nothing to produce must not reach the queue at all: on a deployment with no
    # conversion lane that call would otherwise fail for want of something it did not need.
    backend = _RecordingBackend(SEED)
    _run(
        lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML])),
        backend=backend,
    )
    assert backend.requested == []


def test_a_repeated_request_asks_again_and_lets_the_task_name_dedup() -> None:
    # One servicer, two calls: the instance keeps no memory of what it enqueued, because it is one of
    # many and scales to zero, so the only durable dedup is the task name. A servicer that remembered
    # would drop the second request of a caller whose first task has since been deleted.
    backend = _RecordingBackend(SEED)
    servicer = servicer_mod.Servicer(backend, _session_resolver)  # pyright: ignore[reportAbstractUsage]
    request = literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_OCR])

    async def run() -> None:
        async with in_process_grpc.serving(
            lambda server: literature_pb2_grpc.add_LiteratureServicer_to_server(servicer, server)
        ) as channel:
            stub = literature_pb2_grpc.LiteratureStub(channel)
            await stub.MaybeIngestPapers(request, metadata=_GOOD_TOKEN)
            await stub.MaybeIngestPapers(request, metadata=_GOOD_TOKEN)

    asyncio.run(run())
    assert backend.requested == [[DOC_OCR], [DOC_OCR]]


@pytest.mark.parametrize(
    ('error', 'expected'),
    [
        (literature_backend.ConversionNotConfiguredError('no queue'), grpc.StatusCode.FAILED_PRECONDITION),
        (literature_backend.ConversionUnavailableError('cloud tasks is down'), grpc.StatusCode.UNAVAILABLE),
        (literature_backend.ConversionEnqueueFailedError('permission denied'), grpc.StatusCode.INTERNAL),
    ],
)
def test_an_enqueue_failure_maps_to_the_status_that_matches_its_remedy(
    error: Exception, expected: grpc.StatusCode
) -> None:
    # UNAVAILABLE is the only one gRPC's default policy retries, so a permanent fault carrying it would
    # spend the caller's whole budget on a deployment it cannot repair.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.MaybeIngestPapers(
                literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_OCR]), metadata=_GOOD_TOKEN
            ),
            backend=_FailingConversionBackend(SEED, error),
        )
    assert exc.value.code() is expected


def test_an_enqueue_failure_fails_the_call_rather_than_reporting_readiness() -> None:
    # PENDING is indistinguishable from "a conversion is under way", so a caller told PENDING after a
    # failed enqueue would have no reason to ask again and the paper would never be converted.
    with pytest.raises(grpc.aio.AioRpcError):
        _run(
            lambda s: s.MaybeIngestPapers(
                literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML, DOI_OCR]), metadata=_GOOD_TOKEN
            ),
            backend=_FailingConversionBackend(SEED, literature_backend.ConversionUnavailableError('down')),
        )


def test_a_conversion_needs_a_session_token() -> None:
    # A conversion spends Anthropic tokens, so a caller that cannot name a session must not be able to
    # start one — and the refusal has to reach the caller rather than answering PENDING with no task
    # placed, which is the dead end a failed enqueue would leave.
    backend = _RecordingBackend(SEED)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_OCR])),
            backend=backend,
        )
    assert exc.value.code() is grpc.StatusCode.UNAUTHENTICATED
    assert backend.requested == []


def test_a_conversion_needs_a_token_the_authorizer_resolves() -> None:
    # A token the authorizer rejects is PERMISSION_DENIED, not UNAUTHENTICATED: the caller presented
    # one, so re-presenting the same one is not the remedy.
    backend = _RecordingBackend(SEED)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.MaybeIngestPapers(
                literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_OCR]), metadata=_BAD_TOKEN
            ),
            backend=backend,
        )
    assert exc.value.code() is grpc.StatusCode.PERMISSION_DENIED
    assert backend.requested == []


def test_a_mixed_batch_with_no_session_is_refused_whole() -> None:
    # The refusal is not per-paper: a settled paper alongside a PENDING one does not buy a readiness
    # answer with the enqueue quietly skipped, which is the dead end a failed enqueue would leave.
    backend = _RecordingBackend(SEED)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML, DOI_OCR])),
            backend=backend,
        )
    assert exc.value.code() is grpc.StatusCode.UNAUTHENTICATED
    assert backend.requested == []


def test_a_resolved_session_may_start_a_conversion() -> None:
    # The other half of the gate: it refuses the caller who cannot name a session without also refusing
    # the one who can.
    backend = _RecordingBackend(SEED)
    _run(
        lambda s: s.MaybeIngestPapers(
            literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_OCR]), metadata=_GOOD_TOKEN
        ),
        backend=backend,
    )
    assert backend.requested == [[DOC_OCR]]


def test_resolving_an_id_needs_no_session_when_there_is_nothing_to_produce() -> None:
    # The corpus is shared, so reading it is ungated — including the crosswalk read that turns a DOI
    # into a doc_id. Only the enqueue is gated, so a batch with nothing PENDING answers without a token.
    response = _run(lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML])))
    assert _paper_readiness(response) == {DOI_XML: (DOC_XML, literature_pb2.FULL_TEXT_STATE_READY)}


def test_poll_full_texts_asks_for_no_conversion() -> None:
    # The query stays a query: polling that enqueues lets a caller re-drive work it already asked for
    # on every poll.
    backend = _RecordingBackend(SEED)
    _run(
        lambda s: s.PollFullTexts(literature_pb2.PollFullTextsRequest(doc_ids=[DOC_OCR, DOC_BARE])),
        backend=backend,
    )
    assert backend.requested == []


def test_the_fixture_backend_converts_nothing_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    # The offline corpus has no queue, so a PENDING paper never advances; a caller watching one has to
    # be able to see that nothing was ever going to convert it.
    backend = literature_backend.FixtureBackend(SEED)
    with caplog.at_level('INFO', logger=literature_backend.__name__):
        asyncio.run(backend.request_conversions([DOC_OCR]))
    assert DOC_OCR in caplog.text
