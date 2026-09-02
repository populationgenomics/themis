"""Behaviour tests for the literature servicer over an in-process grpc.aio server."""

from __future__ import annotations

import asyncio
import datetime
import typing
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import override

import grpc
import grpc.aio
import pytest

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, literature_pb2, literature_pb2_grpc
from themis.services.evidence import errors, serving
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import fixture as fixture_mod
from themis.services.evidence.literature import servicer as servicer_mod
from themis.services.evidence.literature import variants
from themis.services.evidence.upstreams import europe_pmc, litvar, pubmed
from themis.testing import in_process_grpc

_GOOD_TOKEN = (('x-themis-session-token', 'good'),)
_BAD_TOKEN = (('x-themis-session-token', 'bad'),)

DOC_XML = 'doc-xml'  # a source-XML-derived markdown rendering + a PDF
DOC_OCR = 'doc-ocr'  # a PDF whose only rendering is a lossy OCR (no markdown)
DOC_BARE = 'doc-bare'  # claimed, nothing fetched yet — neither representation
DOI_XML = 'doi:10.1/xml'
PMID_XML = 'pmid:12345'
DOI_OCR = 'doi:10.1/ocr'
QUOTE_MD = 'markedly reduced ATP sensitivity'  # verbatim in MARKDOWN_XML: located, not seeded
QUOTE_PDF = 'a quote locatable in the pdf'
MARKDOWN_XML = '# An XML-derived paper\n\nThe channel showed markedly reduced ATP sensitivity.\n'
# Past grpc's 16 KiB hard metadata limit, where a trailer is dropped whole. Between the 8 KiB soft
# limit and it rejection is random, so a shorter field would catch an unclipped echo only sometimes.
FIELD_PAST_TRAILER_LIMIT = 'x' * 20_000
# The two records the seeded live index holds, as the index keys them — bare digits, unqualified.
# The first names the paper the store holds as DOC_XML: an index identifier and a doc_id meet through
# MaybeIngestPapers and nowhere else. The second is indexed and states no abstract.
INDEXED_PMID = PMID_XML.removeprefix('pmid:')
INDEXED_PMID_NO_ABSTRACT = '31234568'
BOOK_PMID = '20301288'  # answered with a book record: a kind of record, never absence
BOOK = fixture_mod.SeededBook(
    pmid=BOOK_PMID,
    nbk='NBK900001',
    title='A synthetic chapter',
    book_title='A synthetic review series',
    publisher='A university press',
    contribution_date=datetime.date(2010, 3, 23),
    date_revised=datetime.date(2024, 1, 4),
    authors=('Doe J',),
)

SEED: Mapping[str, fixture_mod.SeededPaper] = {
    DOC_XML: fixture_mod.SeededPaper(
        title='An XML-derived paper',
        external_ids=(DOI_XML, PMID_XML),  # one paper, two ids — the ordinary crosswalk case
        files=(
            fixture_mod.SeededFile(
                name='figure1.png',
                role=literature_pb2.FILE_ROLE_FIGURE,
                media_type='image/png',
                gcs_uri='gs://fulltext/doc-xml/figure1.png',
            ),
            fixture_mod.SeededFile(
                name='supp.xlsx',
                role=literature_pb2.FILE_ROLE_SUPPLEMENTARY,
                media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                gcs_uri='gs://fulltext/doc-xml/supp.xlsx',
            ),
        ),
        markdown_gcs_uri='gs://fulltext/doc-xml/rendering.md',
        markdown_from_xml=True,
        markdown_text=MARKDOWN_XML,
        pdf_gcs_uri='gs://fulltext/doc-xml/doc.pdf',
        pdf_locations={QUOTE_PDF: fixture_mod.SeededPdfLocation(page=1, rects=((0.1, 0.2, 0.3, 0.02),))},
    ),
    DOC_BARE: fixture_mod.SeededPaper(title='A freshly-minted paper'),
    DOC_OCR: fixture_mod.SeededPaper(
        title='A scan-only paper',
        external_ids=(DOI_OCR,),
        pdf_gcs_uri='gs://fulltext/doc-ocr/doc.pdf',
        pdf_locations={QUOTE_PDF: fixture_mod.SeededPdfLocation(page=0, rects=((0.0, 0.0, 0.5, 0.05),))},
    ),
}


async def _session_resolver(session_token: str) -> auth_pb2.SessionContext:
    if session_token == 'good':
        return auth_pb2.SessionContext(project_id='proj', analysis_id='ana')
    raise session_mod.UnresolvedSessionError


RECORDS = (
    europe_pmc.Record(
        pmid=INDEXED_PMID,
        title='An XML-derived paper',
        authors=('Doe J', 'Roe R'),
        journal='J Med Genet',
        year='2021',
        doi='10.1/x',
        abstract='A truncating variant in GENE1.',
        pmcid='PMC900001',
    ),
    europe_pmc.Record(
        pmid=INDEXED_PMID_NO_ABSTRACT,
        title='A scan-only paper',
        authors=('Ng A',),
        journal='Hum Mutat',
        year='2019',
        doi='10.2/y',
        abstract='',  # indexed, and states no abstract — a letter or a comment upstream
        pmcid='',
    ),
)

# One entity per identifier a request can carry, so a servicer-level case can reach each: the
# rsID-keyed entity, and the gene+change-keyed one it shares no record with.
ENTITIES = (
    fixture_mod.SeededEntity(
        labels=litvar.EntityLabels(
            id='litvar@rs00##', rsid='rs00', caids=('CA1000',), genes=('GENE1',), change='c.1063G>A'
        ),
        pmids=(INDEXED_PMID, INDEXED_PMID_NO_ABSTRACT),
        total_records=5,
    ),
    fixture_mod.SeededEntity(
        labels=litvar.EntityLabels(id='litvar@#77#p.A355T', rsid='', caids=(), genes=('GENE1',), change='p.A355T'),
        pmids=(INDEXED_PMID_NO_ABSTRACT,),
        total_records=1,
    ),
)


def _seeded() -> fixture_mod.FixtureBackend:
    """The whole seeded corpus: the SEED papers, and the records and entities the index holds."""
    return fixture_mod.FixtureBackend(SEED, RECORDS, ENTITIES, book_articles=(BOOK,))


def _run_over[T](
    backend: literature_backend.LiteratureBackend,
    stub_call: Callable[[literature_pb2_grpc.LiteratureAsyncStub], Awaitable[T]],
) -> T:
    """Drive one call against a real in-process server + stub over `backend`.

    The stub attaches no session token unless `stub_call` asks for one, so a call driven through here
    reaches the servicer unauthorized by default — which is what a read here is entitled to be.
    """

    async def run() -> T:
        servicer = servicer_mod.Servicer(backend, _session_resolver)
        async with in_process_grpc.serving(
            lambda server: literature_pb2_grpc.add_LiteratureServicer_to_server(servicer, server)
        ) as channel:
            return await stub_call(literature_pb2_grpc.LiteratureStub(channel))

    return asyncio.run(run())


def _run[T](
    stub_call: Callable[[literature_pb2_grpc.LiteratureAsyncStub], Awaitable[T]],
    *,
    backend: literature_backend.LiteratureBackend | None = None,
) -> T:
    return _run_over(backend or _seeded(), stub_call)


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
    assert md.gcs_uri == 'gs://fulltext/doc-xml/rendering.md'
    assert md.media_type == 'text/markdown'
    pdf = _run(
        lambda s: s.ResolveContent(
            literature_pb2.ResolveContentRequest(doc_id=DOC_XML, pdf=literature_pb2.PdfSelector())
        )
    )
    assert pdf.gcs_uri == 'gs://fulltext/doc-xml/doc.pdf'
    assert pdf.media_type == 'application/pdf'
    fig = _run(
        lambda s: s.ResolveContent(
            literature_pb2.ResolveContentRequest(doc_id=DOC_XML, file=literature_pb2.FileSelector(name='figure1.png'))
        )
    )
    assert fig.gcs_uri == 'gs://fulltext/doc-xml/figure1.png'
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


def test_a_not_found_echoing_a_long_file_name_still_carries_its_reason() -> None:
    # The NOT_FOUND names the file the caller asked for, at whatever length the caller chose; unclipped
    # past the trailer limit, it would reach the caller as RESOURCE_EXHAUSTED with the name gone.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.ResolveContent(
                literature_pb2.ResolveContentRequest(
                    doc_id=DOC_XML, file=literature_pb2.FileSelector(name=FIELD_PAST_TRAILER_LIMIT)
                )
            )
        )
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND
    assert 'has no file' in (exc.value.details() or '')


def test_locate_markdown_returns_offsets() -> None:
    result = _run(
        lambda s: s.Locate(
            literature_pb2.LocateRequest(
                doc_id=DOC_XML, quote=QUOTE_MD, representation=literature_pb2.REPRESENTATION_MARKDOWN
            )
        )
    )
    assert result.WhichOneof('result') == 'offsets'
    # The offsets are the matcher's own answer over the served text — what the pane will highlight.
    assert MARKDOWN_XML[result.offsets.start : result.offsets.end] == QUOTE_MD


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


def test_an_id_the_store_does_not_know_is_an_empty_doc_id() -> None:
    # A miss is per-id and modelled: an empty doc_id with UNKNOWN_PAPER, never a call-level error and
    # never a minted doc_id that would name no manifest.
    response = _run(
        lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=['doi:10.1/never-seen']))
    )
    assert _paper_readiness(response) == {'doi:10.1/never-seen': ('', literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER)}


@pytest.mark.parametrize(
    'external_id',
    [
        '10.1/xml',
        '12345',
        'isbn:9780000000000',
        '',
        'doi',
        'doi:',
        ':10.1/xml',
        pytest.param(FIELD_PAST_TRAILER_LIMIT, id='past-trailer-limit'),
    ],
)
def test_an_unqualified_external_id_is_invalid_argument(external_id: str) -> None:
    # Guessing a scheme resolves to another paper when it guesses wrong, so the boundary rejects. A
    # bare scheme or an empty value is rejected here too: reaching the crosswalk it would miss and come
    # back as UNKNOWN_PAPER, reporting a malformed request as a settled fact about the store. The
    # refusal echoes the entry, so the long one holds it to the same code past the trailer limit.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[external_id])))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_an_oversized_external_id_batch_is_invalid_argument() -> None:
    external_ids = [f'doi:10.1/{i}' for i in range(servicer_mod._MAX_EXTERNAL_IDS + 1)]
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=external_ids)))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_the_external_id_bound_is_on_what_the_request_carries() -> None:
    # The proto states the bound on the repeated field, as PollFullTexts applies it. Counting after
    # dedup, a caller sending the same id ten thousand times would spend the message budget and the
    # walk over it and be answered, which is the cost the bound exists to refuse.
    repeated = [DOI_XML] * (servicer_mod._MAX_EXTERNAL_IDS + 1)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=repeated)))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


class _UnreachableCrosswalkBackend(fixture_mod.FixtureBackend):
    """A backend whose crosswalk is down, to pin the whole-batch failure mapping."""

    @override
    async def resolve_external_ids(self, external_ids: Sequence[str]) -> dict[str, str]:
        del external_ids
        raise literature_backend.CrosswalkUnavailableError('connection refused')


class _NoCrosswalkBackend(fixture_mod.FixtureBackend):
    """A backend deployed without a crosswalk, to pin the permanent-condition mapping."""

    @override
    async def resolve_external_ids(self, external_ids: Sequence[str]) -> dict[str, str]:
        del external_ids
        raise literature_backend.CrosswalkNotConfiguredError('no crosswalk is configured')


def test_an_unreachable_crosswalk_is_unavailable_for_the_whole_call() -> None:
    # Not a per-id empty doc_id: a caller reading an outage per-id would write every one of these
    # papers off as absent from the store, which is the transient-as-terminal trap.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML])),
            backend=_UnreachableCrosswalkBackend(SEED, RECORDS, ENTITIES),
        )
    assert exc.value.code() is grpc.StatusCode.UNAVAILABLE


def test_an_unconfigured_crosswalk_is_failed_precondition_not_unavailable() -> None:
    # A deployment wiring no crosswalk can never answer this call, and UNAVAILABLE is retried by
    # gRPC's default policy — so the caller would spend its whole retry budget on a permanent fact.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(
            lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML])),
            backend=_NoCrosswalkBackend(SEED, RECORDS, ENTITIES),
        )
    assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION


class _ConversionRecordingBackend(fixture_mod.FixtureBackend):
    """A backend that records which papers a conversion was asked for."""

    def __init__(
        self,
        papers: Mapping[str, fixture_mod.SeededPaper],
        records: Sequence[europe_pmc.Record],
        entities: Sequence[fixture_mod.SeededEntity],
    ) -> None:
        super().__init__(papers, records, entities)
        self.requested: list[list[str]] = []

    @override
    async def request_conversions(self, doc_ids: Sequence[str]) -> None:
        self.requested.append(list(doc_ids))


class _FailingConversionBackend(fixture_mod.FixtureBackend):
    """A backend whose conversion lane fails a given way, to pin the status mapping."""

    def __init__(
        self,
        papers: Mapping[str, fixture_mod.SeededPaper],
        records: Sequence[europe_pmc.Record],
        entities: Sequence[fixture_mod.SeededEntity],
        error: Exception,
    ) -> None:
        super().__init__(papers, records, entities)
        self._error = error

    @override
    async def request_conversions(self, doc_ids: Sequence[str]) -> None:
        del doc_ids
        raise self._error


def test_maybe_ingest_papers_asks_for_a_conversion_of_the_pending_papers_only() -> None:
    # READY needs nothing, and an id the corpus does not know has no manifest for a producer to read
    # and no task to name — asking for either would be a conversion nothing could perform.
    backend = _ConversionRecordingBackend(SEED, RECORDS, ENTITIES)
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
    backend = _ConversionRecordingBackend(SEED, RECORDS, ENTITIES)
    _run(
        lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML])),
        backend=backend,
    )
    assert backend.requested == []


def test_a_repeated_request_asks_again_and_lets_the_task_name_dedup() -> None:
    # One servicer, two calls: the instance keeps no memory of what it enqueued, because it is one of
    # many and scales to zero, so the only durable dedup is the task name. A servicer that remembered
    # would drop the second request of a caller whose first task has since been deleted.
    backend = _ConversionRecordingBackend(SEED, RECORDS, ENTITIES)
    servicer = servicer_mod.Servicer(backend, _session_resolver)
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
            backend=_FailingConversionBackend(SEED, RECORDS, ENTITIES, error),
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
            backend=_FailingConversionBackend(
                SEED, RECORDS, ENTITIES, literature_backend.ConversionUnavailableError('down')
            ),
        )


def test_a_conversion_needs_a_session_token() -> None:
    # A conversion spends Anthropic tokens, so a caller that cannot name a session must not be able to
    # start one — and the refusal has to reach the caller rather than answering PENDING with no task
    # placed, which is the dead end a failed enqueue would leave.
    backend = _ConversionRecordingBackend(SEED, RECORDS, ENTITIES)
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
    backend = _ConversionRecordingBackend(SEED, RECORDS, ENTITIES)
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
    backend = _ConversionRecordingBackend(SEED, RECORDS, ENTITIES)
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
    backend = _ConversionRecordingBackend(SEED, RECORDS, ENTITIES)
    _run(
        lambda s: s.MaybeIngestPapers(
            literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_OCR]), metadata=_GOOD_TOKEN
        ),
        backend=backend,
    )
    assert backend.requested == [[DOC_OCR]]


def test_a_pmid_resolves_under_any_of_its_spellings() -> None:
    # The crosswalk keys pmids by their digits, so the padded and `PMID:`-prefixed spellings of one
    # identifier reach one row — a spelling passed through as written would read as a paper the
    # store does not hold.
    response = _run(
        lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[f'pmid:00{INDEXED_PMID}']))
    )
    (readiness,) = response.readiness
    assert readiness.external_id == f'pmid:00{INDEXED_PMID}'  # echoed as supplied
    assert readiness.doc_id == DOC_XML


def test_a_pmid_value_that_is_not_one_is_refused() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=['pmid:PMC123'])))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_resolving_an_id_needs_no_session_when_there_is_nothing_to_produce() -> None:
    # The corpus is shared, so reading it is ungated — including the crosswalk read that turns a DOI
    # into a doc_id. Only the enqueue is gated, so a batch with nothing PENDING answers without a token.
    response = _run(lambda s: s.MaybeIngestPapers(literature_pb2.MaybeIngestPapersRequest(external_ids=[DOI_XML])))
    assert _paper_readiness(response) == {DOI_XML: (DOC_XML, literature_pb2.FULL_TEXT_STATE_READY)}


def test_poll_full_texts_asks_for_no_conversion() -> None:
    # The query stays a query: polling that enqueues lets a caller re-drive work it already asked for
    # on every poll.
    backend = _ConversionRecordingBackend(SEED, RECORDS, ENTITIES)
    _run(
        lambda s: s.PollFullTexts(literature_pb2.PollFullTextsRequest(doc_ids=[DOC_OCR, DOC_BARE])),
        backend=backend,
    )
    assert backend.requested == []


def test_the_fixture_backend_converts_nothing_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    # The offline corpus has no queue, so a PENDING paper never advances; a caller watching one has to
    # be able to see that nothing was ever going to convert it.
    backend = fixture_mod.FixtureBackend(SEED, RECORDS, ENTITIES)
    with caplog.at_level('INFO', logger=fixture_mod.__name__):
        asyncio.run(backend.request_conversions([DOC_OCR]))
    assert DOC_OCR in caplog.text


def test_get_markdown_serves_the_rendering_text() -> None:
    result = _run(lambda s: s.GetMarkdown(literature_pb2.GetMarkdownRequest(doc_id=DOC_XML)))
    assert result.WhichOneof('result') == 'content'
    assert result.content.markdown == MARKDOWN_XML
    assert result.content.total_chars == len(MARKDOWN_XML) == len(result.content.markdown)  # served whole


def test_get_markdown_cuts_to_the_requested_budget_and_reports_the_whole_length() -> None:
    # The census is what tells a whole paper from a clipped one: a caller reading only the text
    # cannot tell that it was cut, and quoting past a cut is exactly what the marker exists to stop.
    long_markdown = '# A long paper\n\n' + 'A line of prose that fills the rendering.\n' * 80
    backend = fixture_mod.FixtureBackend(
        {
            'doc-long': fixture_mod.SeededPaper(
                title='A long paper', markdown_gcs_uri='gs://fulltext/doc-long/r.md', markdown_text=long_markdown
            )
        },
        RECORDS,
        ENTITIES,
    )
    asked = servicer_mod._MAX_CHARS_FLOOR  # under the rendering, so the cut is reached
    result = _run_over(
        backend, lambda s: s.GetMarkdown(literature_pb2.GetMarkdownRequest(doc_id='doc-long', max_chars=asked))
    )
    assert result.content.total_chars == len(long_markdown) > asked  # how much lies past the cut
    assert len(result.content.markdown) <= asked  # the marker rides inside the budget
    kept, marker = result.content.markdown.split('\n\n---\n\n', 1)
    assert long_markdown.startswith(kept)
    assert 'cannot be quoted or cited' in marker


def test_get_markdown_without_a_budget_takes_the_service_default() -> None:
    result = _run(lambda s: s.GetMarkdown(literature_pb2.GetMarkdownRequest(doc_id=DOC_XML)))
    assert result.content.markdown == MARKDOWN_XML  # the seeded rendering is far under the default
    assert result.content.total_chars == len(MARKDOWN_XML)


def test_get_markdown_reports_a_paper_without_a_rendering_as_a_fact_about_the_store() -> None:
    result = _run(lambda s: s.GetMarkdown(literature_pb2.GetMarkdownRequest(doc_id=DOC_OCR)))
    assert result.WhichOneof('result') == 'unavailable'
    assert result.unavailable.state == literature_pb2.FULL_TEXT_STATE_PENDING


def test_get_markdown_for_an_unknown_doc_is_not_found() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.GetMarkdown(literature_pb2.GetMarkdownRequest(doc_id='nope')))
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


def test_get_markdown_without_a_doc_id_is_not_found() -> None:
    # An unset doc_id is an id the store holds no paper for, as every sibling rpc answers it: the
    # proto promises NOT_FOUND for an unknown doc_id, and the empty string is one.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.GetMarkdown(literature_pb2.GetMarkdownRequest()))
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


# --- Discovery ------------------------------------------------------------------------------------


def test_search_returns_the_indexed_records() -> None:
    reply = _run(lambda s: s.SearchEuropePmc(literature_pb2.SearchEuropePmcRequest(query='paper', max_results=10)))
    assert [record.pmid for record in reply.records] == [INDEXED_PMID, INDEXED_PMID_NO_ABSTRACT]
    assert reply.records[0].journal == 'J Med Genet'
    assert reply.records[0].year == '2021'
    assert reply.records[0].authors == ['Doe J', 'Roe R']  # one entry per author, the index's order
    assert reply.records[0].pmcid == 'PMC900001'  # the key for a hit no PubMed id names


@pytest.mark.parametrize(('requested', 'expected'), [(0, 10), (5, 5), (25, 25), (100, 25)])
def test_clamp_max_results(requested: int, expected: int) -> None:
    assert servicer_mod._clamp_max_results(requested) == expected


@pytest.mark.parametrize(('requested', 'expected'), [(0, 30), (5, 5), (50, 50), (100, 50)])
def test_clamp_variant_max_results(requested: int, expected: int) -> None:
    assert servicer_mod._clamp_variant_max_results(requested) == expected


@pytest.mark.parametrize(('requested', 'expected'), [(0, 50), (5, 5), (200, 200), (5000, 200)])
def test_clamp_gene_entities(requested: int, expected: int) -> None:
    assert servicer_mod._clamp_gene_entities(requested) == expected


@pytest.mark.parametrize(
    ('requested', 'expected'),
    [(0, 500_000), (500, 1_000), (5_000, 5_000), (1_000_000, 1_000_000), (9_000_000, 1_000_000)],
)
def test_clamp_max_chars(requested: int, expected: int) -> None:
    assert servicer_mod._clamp_max_chars(requested) == expected


def test_a_search_for_nothing_is_refused() -> None:
    # Europe PMC answers an empty query with an HTTP-200 refusal document; refusing it here keeps
    # the round trip unspent and the status the one every other unanswerable request gets.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.SearchEuropePmc(literature_pb2.SearchEuropePmcRequest(query='   ')))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_search_clamps_the_record_count_to_what_was_asked() -> None:
    reply = _run(lambda s: s.SearchEuropePmc(literature_pb2.SearchEuropePmcRequest(query='paper', max_results=1)))
    assert [record.pmid for record in reply.records] == [INDEXED_PMID]


def test_a_clamped_search_states_what_the_index_matched() -> None:
    # Without the census a page cut to the budget reads as the whole of what the query matched, and
    # a caller weighing the literature would take two hits for the whole field.
    reply = _run(lambda s: s.SearchEuropePmc(literature_pb2.SearchEuropePmcRequest(query='paper', max_results=1)))
    assert reply.total_matched == len(RECORDS) > len(reply.records)


def _fetch_pubmed_articles(*pmids: str) -> literature_pb2.FetchPubmedArticlesResponse:
    return _run(lambda s: s.FetchPubmedArticles(literature_pb2.FetchPubmedArticlesRequest(pmids=pmids)))


def test_fetch_pubmed_articles_returns_each_record_whole() -> None:
    reply = _fetch_pubmed_articles(INDEXED_PMID_NO_ABSTRACT, INDEXED_PMID)
    assert [article.medline_citation.pmid.value for article in reply.articles] == [
        INDEXED_PMID_NO_ABSTRACT,
        INDEXED_PMID,
    ]  # the request's order
    fetched = reply.articles[1]
    assert fetched.medline_citation.article.abstract.abstract_text[0].value == 'A truncating variant in GENE1.'
    assert any(article_id.value == '10.1/x' for article_id in fetched.pubmed_data.article_id_list)


def test_fetch_pubmed_articles_separates_a_record_with_no_abstract_from_no_record_at_all() -> None:
    # Both reach a caller reading the abstract alone as absence, and they are different facts: one
    # names a paper to cite, the other says the identifier reaches nothing.
    reply = _fetch_pubmed_articles(INDEXED_PMID_NO_ABSTRACT, '9999999')

    (no_abstract,) = reply.articles
    assert not no_abstract.medline_citation.article.HasField('abstract')
    assert no_abstract.medline_citation.article.article_title.value  # the bibliography outlives the abstract
    assert list(reply.pmids_without_record) == ['9999999']


def test_fetch_pubmed_articles_answers_every_requested_pmid_exactly_once() -> None:
    # Position is not the correlation key — a repeat collapses — so every requested PMID lands in
    # exactly one of the three outcomes; none is left for the caller to notice missing.
    reply = _fetch_pubmed_articles(
        INDEXED_PMID, f'0000{INDEXED_PMID}', f'PMID:{INDEXED_PMID_NO_ABSTRACT}', BOOK_PMID, '9999999'
    )
    assert [article.medline_citation.pmid.value for article in reply.articles] == [
        INDEXED_PMID,
        INDEXED_PMID_NO_ABSTRACT,
    ]
    (book,) = reply.book_articles
    assert book.book_document.pmid.value == BOOK_PMID
    assert [i.value for i in book.book_document.article_id_list] == [BOOK.nbk]  # the record, whole
    assert list(reply.pmids_without_record) == ['9999999']


@pytest.mark.parametrize(
    'pmids',
    [
        [],
        ['not-a-pmid'],
        ['0'],
        [INDEXED_PMID, 'PMC7654321'],
        [INDEXED_PMID, f'{INDEXED_PMID} OR EXT_ID:{INDEXED_PMID_NO_ABSTRACT}'],
        [str(pmid) for pmid in range(10_000_000, 10_000_051)],
    ],
    ids=['empty', 'malformed', 'zero', 'not-a-pmid', 'carrying-query-syntax', 'over-the-batch-ceiling'],
)
def test_an_abstract_batch_the_service_will_not_answer_whole_is_refused(pmids: list[str]) -> None:
    # Trimming to the ceiling, or dropping the malformed entry, would answer about fewer records than
    # were asked about, and every PMID it dropped would read as one nothing is indexed under.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _fetch_pubmed_articles(*pmids)
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def _search_litvar(**fields: object) -> literature_pb2.SearchLitVarResponse:
    return _run(
        lambda s: s.SearchLitVar(
            literature_pb2.SearchLitVarRequest(**fields)  # pyright: ignore[reportArgumentType]
        )
    )


def test_search_litvar_reports_each_entity_with_its_verdicts_and_pmids() -> None:
    reply = _search_litvar(gene='GENE1', rsid='rs00', max_pmids_per_entity=10)
    (entity,) = reply.entities
    assert entity.id == 'litvar@rs00##'
    assert entity.agreement.gene == literature_pb2.IdentifierAgreement.AGREEMENT_AGREES
    assert entity.agreement.rsid == literature_pb2.IdentifierAgreement.AGREEMENT_AGREES
    assert entity.agreement.caid == literature_pb2.IdentifierAgreement.AGREEMENT_UNCOMPARED
    assert list(entity.pmids) == [INDEXED_PMID, INDEXED_PMID_NO_ABSTRACT]


def test_search_litvar_census_is_read_per_entity() -> None:
    # A budget's cut and the index's whole count are both legible on the entity itself, so the two
    # next moves — a larger max_results, or nothing because the index has no more — stay tellable.
    cut = _search_litvar(gene='GENE1', rsid='rs00', max_pmids_per_entity=1)
    (entity,) = cut.entities
    assert len(entity.pmids) == 1 < entity.total_records

    whole = _search_litvar(entity_id='litvar@#77#p.A355T', max_pmids_per_entity=10)
    assert whole.entities[0].total_records == len(whole.entities[0].pmids)


def test_search_litvar_reports_a_shared_pmid_under_each_entity() -> None:
    # The entity sets are not a partition, so a PMID under two entities is under both here; the
    # caller deduplicates before counting anything.
    reply = _search_litvar(gene='GENE1', rsid='rs00', protein_change='p.A355T', max_pmids_per_entity=10)
    listed = [pmid for entity in reply.entities for pmid in entity.pmids]
    assert sorted(listed) == sorted([INDEXED_PMID, INDEXED_PMID_NO_ABSTRACT, INDEXED_PMID_NO_ABSTRACT])


class _IndexOnlyBackend(literature_backend.LiteratureBackend):
    """Stages an index answer and refuses every store call.

    The subclasses below drive index rpcs only. A store call arriving here is a test wiring a case it
    does not exercise, which the refusal says outright rather than answering from an empty corpus.
    """

    @override
    async def describe_paper(self, doc_id: str) -> literature_pb2.PaperInfo:
        raise AssertionError('no store call is exercised here')

    @override
    async def get_markdown(self, doc_id: str, max_chars: int) -> literature_pb2.GetMarkdownResponse:
        raise AssertionError('no store call is exercised here')

    @override
    async def resolve_content(
        self, doc_id: str, selector: literature_backend.ContentSelector
    ) -> literature_pb2.ContentLocation:
        raise AssertionError('no store call is exercised here')

    @override
    async def locate(
        self, doc_id: str, quote: str, representation: literature_pb2.Representation
    ) -> literature_pb2.LocateResponse:
        raise AssertionError('no store call is exercised here')

    @override
    async def validate(self, doc_id: str, quote: str) -> literature_pb2.ValidateResponse:
        raise AssertionError('no store call is exercised here')

    @override
    async def resolve_external_ids(self, external_ids: Sequence[str]) -> dict[str, str]:
        raise AssertionError('no store call is exercised here')

    @override
    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        raise AssertionError('no store call is exercised here')

    @override
    async def request_conversions(self, doc_ids: Sequence[str]) -> None:
        raise AssertionError('no store call is exercised here')


class _RecordingBackend(_IndexOnlyBackend):
    """Answers every index call with nothing, and records the budgets the servicer applied."""

    def __init__(self) -> None:
        self.max_results: int | None = None
        self.max_entities: int | None = None

    @override
    async def search_europe_pmc(self, query: str, max_results: int) -> europe_pmc.SearchHits:
        self.max_results = max_results
        return europe_pmc.SearchHits(records=[], total_matched=0)

    @override
    async def fetch_pubmed_articles(self, pmids: Sequence[str]) -> pubmed.FetchedArticles:
        return pubmed.FetchedArticles(articles=[], book_articles=[], pmids_without_record=list(pmids))

    @override
    async def search_litvar(
        self, requested: variants.RequestedVariant, *, max_results: int, max_entities: int
    ) -> variants.VariantCensus:
        self.max_results, self.max_entities = max_results, max_entities
        return variants.VariantCensus(entities=(), total_entities=0)

    @override
    async def list_litvar_entities(self, *, gene: str, contains: str, max_results: int) -> variants.GeneEntities:
        self.max_results = max_results
        return variants.GeneEntities(entities=(), total_in_gene=0, total_matched=0)


def test_the_entity_fan_out_is_the_services_bound_and_no_request_raises_it() -> None:
    # Nothing upstream bounds how many entities autocomplete returns, and each costs its own labels
    # fetch and page walk — the fan-out the record ceiling does not reach. So the request states a
    # record budget and nothing else, and even that is capped before the port ever sees it.
    recorder = _RecordingBackend()
    _run_over(
        recorder, lambda s: s.SearchLitVar(literature_pb2.SearchLitVarRequest(rsid='rs00', max_pmids_per_entity=500))
    )
    assert recorder.max_entities == servicer_mod._MAX_ENTITIES
    assert recorder.max_results == servicer_mod._VARIANT_MAX_RESULTS_CEILING


def test_a_refusal_echoing_a_long_caller_field_still_carries_its_reason() -> None:
    # The abort detail rides a grpc trailer, and an over-limit trailer is dropped whole — the caller
    # would get RESOURCE_EXHAUSTED with the diagnosis gone. Clipping keeps the reason on the wire.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _fetch_pubmed_articles(FIELD_PAST_TRAILER_LIMIT)
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert 'is not a PubMed identifier' in (exc.value.details() or '')


def test_search_litvar_refuses_a_caid_that_is_not_one() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _search_litvar(gene='GENE1', caid='rs00', max_pmids_per_entity=10)
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_search_litvar_refuses_a_request_with_nothing_to_resolve_from() -> None:
    # A gene alone reaches the gene's whole literature, not a variant's; that is what
    # ListLitVarEntities is for, and answering it here would look like a lookup that found nothing.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _search_litvar(gene='GENE1', max_pmids_per_entity=10)
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_search_litvar_is_empty_when_nothing_resolves() -> None:
    assert _search_litvar(gene='GENE1', rsid='rs999999', max_pmids_per_entity=10).entities == []


def test_agreement_verdicts_have_distinct_wire_values() -> None:
    # Two verdicts sharing a value would tell a caller the same thing about opposite findings.
    values = [servicer_mod._AGREEMENT[verdict] for verdict in variants.Agreement]
    assert len(set(values)) == len(values)
    assert literature_pb2.IdentifierAgreement.AGREEMENT_UNSPECIFIED not in values


def test_list_litvar_entities_lists_the_genes_entities_with_its_census() -> None:
    reply = _run(lambda s: s.ListLitVarEntities(literature_pb2.ListLitVarEntitiesRequest(gene='GENE1')))
    assert reply.total_in_gene == reply.total_matched == len(reply.entities) == 2
    counts = [entity.total_records for entity in reply.entities]
    assert counts == sorted(counts, reverse=True)  # most-published first


def test_list_litvar_entities_narrows_on_the_id_and_says_how_much_it_dropped() -> None:
    reply = _run(
        lambda s: s.ListLitVarEntities(literature_pb2.ListLitVarEntitiesRequest(gene='GENE1', contains='a355'))
    )
    assert [entity.id for entity in reply.entities] == ['litvar@#77#p.A355T']  # case-insensitive
    assert reply.total_matched == 1
    assert reply.total_in_gene > reply.total_matched


def test_list_litvar_entities_requires_a_gene() -> None:
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run(lambda s: s.ListLitVarEntities(literature_pb2.ListLitVarEntitiesRequest(gene='  ')))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


_REFUSAL = 'Europe PMC rejected "GENE1 AND (" (400): unbalanced parentheses'


class _RefusingBackend(_IndexOnlyBackend):
    """A backend whose every index call the upstream answered a 4xx to."""

    @override
    async def search_europe_pmc(self, query: str, max_results: int) -> europe_pmc.SearchHits:
        raise errors.InvalidRequestError(_REFUSAL)

    @override
    async def fetch_pubmed_articles(self, pmids: Sequence[str]) -> pubmed.FetchedArticles:
        raise errors.InvalidRequestError(_REFUSAL)

    @override
    async def search_litvar(
        self, requested: variants.RequestedVariant, *, max_results: int, max_entities: int
    ) -> variants.VariantCensus:
        raise errors.InvalidRequestError(_REFUSAL)

    @override
    async def list_litvar_entities(self, *, gene: str, contains: str, max_results: int) -> variants.GeneEntities:
        raise errors.InvalidRequestError(_REFUSAL)


@pytest.mark.parametrize(
    'call',
    [
        lambda s: s.SearchEuropePmc(literature_pb2.SearchEuropePmcRequest(query='GENE1 AND (')),
        lambda s: s.FetchPubmedArticles(literature_pb2.FetchPubmedArticlesRequest(pmids=[INDEXED_PMID])),
        lambda s: s.SearchLitVar(literature_pb2.SearchLitVarRequest(rsid='rs00')),
        lambda s: s.ListLitVarEntities(literature_pb2.ListLitVarEntitiesRequest(gene='GENE1')),
    ],
    ids=['search', 'abstracts', 'variant', 'entities'],
)
def test_an_upstream_refusing_the_request_reaches_the_caller_as_invalid_argument(
    call: Callable[[literature_pb2_grpc.LiteratureAsyncStub], Awaitable[object]],
) -> None:
    # The index judged the request as issued, so reissuing it cannot answer differently — which is
    # what the guest's retry helper does with the UNKNOWN an unmapped failure becomes. The upstream's
    # own explanation travels with it: it names what to change.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        _run_over(_RefusingBackend(), call)
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert _REFUSAL in (exc.value.details() or '')


class _StalledBackend(fixture_mod.FixtureBackend):
    """A variant lookup that never answers; every other call is the seeded backend's, unchanged."""

    def __init__(self) -> None:
        super().__init__(SEED, RECORDS, ENTITIES)
        self.cancelled = False

    @override
    async def search_litvar(
        self, requested: variants.RequestedVariant, *, max_results: int, max_entities: int
    ) -> variants.VariantCensus:
        stalled = asyncio.Event()  # nothing sets it
        try:
            while True:
                await stalled.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_a_fan_out_that_never_answers_ends_as_this_rpcs_own_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The overrun is this service's own status naming the rpc, and the work behind it is dropped.

    The serial fan-out has no bound of its own — N autocompletes, a labels fetch and a page walk per
    entity, each under the shared upstream timeout — so without this the caller's own cancellation is
    what ends the call, naming nothing. A caller that has given up is not owed the composition still
    running for it, and an rpc that answers in time is untouched by the bound.
    """
    monkeypatch.setattr(serving, '_RPC_DEADLINE_S', 0.05)
    backend = _StalledBackend()

    async def drive(
        stub: literature_pb2_grpc.LiteratureAsyncStub,
    ) -> tuple[grpc.aio.AioRpcError, bool, literature_pb2.PaperInfo]:
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.SearchLitVar(literature_pb2.SearchLitVarRequest(rsid='rs00'))
        # Read while the loop still runs: `asyncio.run` cancels whatever is left at teardown, so a
        # flag read after it cannot tell a dropped fan-out from an abandoned one.
        cancelled = backend.cancelled
        return exc.value, cancelled, await stub.DescribePaper(literature_pb2.DescribePaperRequest(doc_id=DOC_XML))

    failure, cancelled, info = _run_over(backend, drive)
    assert failure.code() is grpc.StatusCode.DEADLINE_EXCEEDED
    assert 'SearchLitVar' in (failure.details() or '')
    assert cancelled
    assert info.has_markdown  # a store rpc under the same bound answers as it always did
