"""The literature interface backend: litcache resolution + quote location (docs/design/document-pane.md).

The backend owns everything litcache-specific: naming the GCS object for a paper's rendering, PDF, or
associated file, and locating a citation's quote within a representation. The servicer depends on the
abstract ``LiteratureBackend`` port, so the same server runs offline (``FixtureBackend``, in-memory) and
deployed (the litcache-reading adapter, B2). Real quote location via anchorite (markdown offsets now;
PDF page regions, B4) lands behind ``locate``/``validate`` without a servicer change.

Port methods are ``async``: the servicer runs on ``grpc.aio``, so a real adapter (GCS, anchorite)
offloads its blocking I/O rather than stalling the single event loop.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses
from collections.abc import Mapping, Sequence

from themis.rpc import literature_pb2

# Default cadence for the AwaitFullText poll loop. Readiness derives from GCS with no push channel, so
# the wait is a bounded poll; the total wait is capped by the caller's timeout regardless.
_AWAIT_POLL_INTERVAL_SECONDS = 2.0


class UnknownPaperError(Exception):
    """No paper with the given canonical doc_id — the servicer maps this to NOT_FOUND."""


class MissingContentError(Exception):
    """The paper lacks the selected object (no such rendering / PDF / file) — NOT_FOUND."""


class RepresentationUnavailableError(Exception):
    """The paper has no rendering in the requested representation — FAILED_PRECONDITION."""


class PdfLocationUnavailableError(Exception):
    """No PDF quote matcher yet (B4, anchorite) — the servicer maps this to UNIMPLEMENTED.

    Distinct from a ``not_located`` result: the quote is not absent, the location cannot be
    computed. A false ``not_located`` would mislabel every PDF quote as unlocatable.
    """


# What object of a paper ResolveContent names — the decoded ResolveContentRequest.selector oneof.
@dataclasses.dataclass(frozen=True)
class MarkdownContent:
    pass


@dataclasses.dataclass(frozen=True)
class PdfContent:
    pass


@dataclasses.dataclass(frozen=True)
class FileContent:
    name: str


ContentSelector = MarkdownContent | PdfContent | FileContent


class LiteratureBackend(abc.ABC):
    @abc.abstractmethod
    async def describe_paper(self, doc_id: str) -> literature_pb2.PaperInfo:
        """The paper's representations and files, or raise ``UnknownPaperError``."""
        ...

    @abc.abstractmethod
    async def resolve_content(self, doc_id: str, selector: ContentSelector) -> literature_pb2.ContentLocation:
        """Name the GCS object for ``selector``.

        Raises:
            UnknownPaperError: no such doc_id.
            MissingContentError: the paper lacks the selected object.
        """
        ...

    @abc.abstractmethod
    async def locate(
        self, doc_id: str, quote: str, representation: literature_pb2.Representation
    ) -> literature_pb2.LocateResponse:
        """Locate ``quote`` within ``representation``.

        Returns a ``not_located`` result when the quote is absent (a first-class outcome, not an
        error).

        Raises:
            UnknownPaperError: no such doc_id.
            RepresentationUnavailableError: the paper has no rendering in ``representation``.
        """
        ...

    @abc.abstractmethod
    async def validate(self, doc_id: str, quote: str) -> literature_pb2.ValidateResponse:
        """Whether ``quote`` locates in any representation — the agent's authoring-time check.

        Never raises for an unknown doc_id or an absent quote: both are ``ok=false`` with a reason,
        the forgiving answer the agent tool wants.
        """
        ...

    @abc.abstractmethod
    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        """Per-doc_id full-text readiness (READY / PENDING / terminal / unknown).

        Never raises for an unknown doc_id — it is ``FULL_TEXT_STATE_UNKNOWN_PAPER`` in the result, so
        one bad id never fails the batch.
        """
        ...

    async def await_full_text_readiness(
        self,
        doc_ids: Sequence[str],
        timeout_seconds: float,
        *,
        poll_interval_seconds: float = _AWAIT_POLL_INTERVAL_SECONDS,
    ) -> dict[str, literature_pb2.FullTextState]:
        """Block until no id is PENDING (all settled) or ``timeout_seconds`` elapses; return readiness.

        Polls :meth:`full_text_readiness` — readiness derives from GCS, there is no push channel to
        wake on — returning as soon as nothing is PENDING or the deadline passes (whichever first). The
        total wait is bounded by ``timeout_seconds`` regardless of ``poll_interval_seconds``. Concrete
        over the abstract probe so both backends share one loop; a backend with a real completion
        signal can override.

        Each cycle re-polls only the ids still PENDING, carrying settled states forward: a poll costs a
        read per id, and only PENDING can still transition — the same monotonicity the settle
        short-circuit already relies on. Duplicate ids collapse to one readiness.
        """
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        pending = list(dict.fromkeys(doc_ids))
        settled: dict[str, literature_pb2.FullTextState] = {}
        while True:
            states = await self.full_text_readiness(pending)
            settled.update(
                (doc_id, state) for doc_id, state in states.items() if state != literature_pb2.FULL_TEXT_STATE_PENDING
            )
            pending = [doc_id for doc_id, state in states.items() if state == literature_pb2.FULL_TEXT_STATE_PENDING]
            if not pending:
                return settled
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return settled | dict.fromkeys(pending, literature_pb2.FULL_TEXT_STATE_PENDING)
            await asyncio.sleep(min(poll_interval_seconds, remaining))


# --- Fixture seed --------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SeededFile:
    name: str
    role: literature_pb2.FileRole
    media_type: str
    gcs_uri: str


@dataclasses.dataclass(frozen=True)
class SeededPdfLocation:
    page: int  # 0-based
    rects: tuple[tuple[float, float, float, float], ...]  # (x, y, width, height), page fractions


@dataclasses.dataclass(frozen=True)
class SeededPaper:
    """One paper's fixture data: its representations, files, and per-representation quote locations."""

    title: str
    files: tuple[SeededFile, ...] = ()
    markdown_gcs_uri: str | None = None
    markdown_from_xml: bool = False
    pdf_gcs_uri: str | None = None
    markdown_locations: Mapping[str, tuple[int, int]] = dataclasses.field(default_factory=dict)
    pdf_locations: Mapping[str, SeededPdfLocation] = dataclasses.field(default_factory=dict)


class FixtureBackend(LiteratureBackend):
    """In-memory backend over an explicit ``SeededPaper`` corpus, for offline use and tests."""

    def __init__(self, papers: Mapping[str, SeededPaper]) -> None:
        self._papers = dict(papers)

    def _paper(self, doc_id: str) -> SeededPaper:
        try:
            return self._papers[doc_id]
        except KeyError:
            raise UnknownPaperError(doc_id) from None

    async def describe_paper(self, doc_id: str) -> literature_pb2.PaperInfo:
        paper = self._paper(doc_id)
        has_markdown = paper.markdown_gcs_uri is not None
        has_pdf = paper.pdf_gcs_uri is not None
        return literature_pb2.PaperInfo(
            doc_id=doc_id,
            title=paper.title,
            has_markdown=has_markdown,
            markdown_from_xml=paper.markdown_from_xml,
            has_pdf=has_pdf,
            default_representation=default_representation(has_markdown, paper.markdown_from_xml, has_pdf),
            files=[literature_pb2.FileInfo(name=f.name, role=f.role, media_type=f.media_type) for f in paper.files],
        )

    async def resolve_content(self, doc_id: str, selector: ContentSelector) -> literature_pb2.ContentLocation:
        paper = self._paper(doc_id)
        match selector:
            case MarkdownContent():
                if paper.markdown_gcs_uri is None:
                    raise MissingContentError(f'{doc_id} has no markdown rendering')
                return literature_pb2.ContentLocation(gcs_uri=paper.markdown_gcs_uri, media_type='text/markdown')
            case PdfContent():
                if paper.pdf_gcs_uri is None:
                    raise MissingContentError(f'{doc_id} has no PDF')
                return literature_pb2.ContentLocation(gcs_uri=paper.pdf_gcs_uri, media_type='application/pdf')
            case FileContent(name=name):
                for f in paper.files:
                    if f.name == name:
                        return literature_pb2.ContentLocation(gcs_uri=f.gcs_uri, media_type=f.media_type)
                raise MissingContentError(f'{doc_id} has no file {name!r}')

    async def locate(
        self, doc_id: str, quote: str, representation: literature_pb2.Representation
    ) -> literature_pb2.LocateResponse:
        paper = self._paper(doc_id)
        if representation == literature_pb2.REPRESENTATION_MARKDOWN:
            if paper.markdown_gcs_uri is None:
                raise RepresentationUnavailableError(f'{doc_id} has no markdown rendering')
            offsets = paper.markdown_locations.get(quote)
            if offsets is None:
                return literature_pb2.LocateResponse(not_located=literature_pb2.QuoteNotLocated())
            start, end = offsets
            return literature_pb2.LocateResponse(offsets=literature_pb2.TextOffsets(start=start, end=end))
        if representation == literature_pb2.REPRESENTATION_PDF:
            if paper.pdf_gcs_uri is None:
                raise RepresentationUnavailableError(f'{doc_id} has no PDF')
            location = paper.pdf_locations.get(quote)
            if location is None:
                return literature_pb2.LocateResponse(not_located=literature_pb2.QuoteNotLocated())
            return literature_pb2.LocateResponse(region=_pdf_region(location))
        raise ValueError(f'unsupported representation {representation!r}')

    async def validate(self, doc_id: str, quote: str) -> literature_pb2.ValidateResponse:
        paper = self._papers.get(doc_id)
        if paper is None:
            return literature_pb2.ValidateResponse(ok=False, reason=f'unknown doc_id {doc_id!r}')
        located_in: list[literature_pb2.Representation] = []
        if quote in paper.markdown_locations:
            located_in.append(literature_pb2.REPRESENTATION_MARKDOWN)
        if quote in paper.pdf_locations:
            located_in.append(literature_pb2.REPRESENTATION_PDF)
        if not located_in:
            return literature_pb2.ValidateResponse(ok=False, reason='quote not located in any representation')
        return literature_pb2.ValidateResponse(ok=True, located_in=located_in)

    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        # A seeded markdown rendering is READY; a PDF-only paper is PENDING (it could be converted); a
        # paper with neither has no full text. The fixture has no conversion queue, so PENDING never
        # advances here — it exercises the wire shape, not the async lane.
        result: dict[str, literature_pb2.FullTextState] = {}
        for doc_id in doc_ids:
            paper = self._papers.get(doc_id)
            if paper is None:
                result[doc_id] = literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER
            elif paper.markdown_gcs_uri is not None:
                result[doc_id] = literature_pb2.FULL_TEXT_STATE_READY
            elif paper.pdf_gcs_uri is not None:
                result[doc_id] = literature_pb2.FULL_TEXT_STATE_PENDING
            else:
                result[doc_id] = literature_pb2.FULL_TEXT_STATE_NO_FULL_TEXT
        return result


def default_representation(has_markdown: bool, markdown_from_xml: bool, has_pdf: bool) -> literature_pb2.Representation:
    """Markdown when a high-fidelity source-XML rendering exists; else the PDF; else the markdown."""
    if has_markdown and markdown_from_xml:
        return literature_pb2.REPRESENTATION_MARKDOWN
    if has_pdf:
        return literature_pb2.REPRESENTATION_PDF
    if has_markdown:
        return literature_pb2.REPRESENTATION_MARKDOWN
    return literature_pb2.REPRESENTATION_UNSPECIFIED


def _pdf_region(location: SeededPdfLocation) -> literature_pb2.PdfRegion:
    return literature_pb2.PdfRegion(
        page=location.page,
        rects=[literature_pb2.Rect(x=x, y=y, width=w, height=h) for (x, y, w, h) in location.rects],
    )
