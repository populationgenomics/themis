"""The literature interface backend: litcache resolution + quote location (docs/design/document-pane.md).

The backend owns everything litcache-specific: naming the GCS object for a paper's rendering, PDF, or
associated file, and locating a citation's quote within a representation. The servicer depends on the
abstract ``LiteratureBackend`` port, so the same server runs offline (``FixtureBackend``, in-memory) and
deployed (``litcache.LitcacheBackend``, which locates quotes with anchorite).

Port methods are ``async``: the servicer runs on ``grpc.aio``, so a real adapter (GCS, anchorite)
offloads its blocking I/O rather than stalling the single event loop.

The fixture's seed format and its parser live here too, with the dataclasses they build: the seed is the
``FixtureBackend``'s input schema, and a caller names the env var it read the JSON from.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses
import json
from collections.abc import Mapping, Sequence
from typing import override

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
    """No PDF quote matcher yet — the servicer maps this to UNIMPLEMENTED.

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

    @override
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

    @override
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

    @override
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

    @override
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

    @override
    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        # The seed models no terminal marker and no conversion queue, so only READY and PENDING are
        # expressible here and a PENDING paper never advances.
        result: dict[str, literature_pb2.FullTextState] = {}
        for doc_id in doc_ids:
            paper = self._papers.get(doc_id)
            if paper is None:
                result[doc_id] = literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER
            elif paper.markdown_gcs_uri is not None:
                result[doc_id] = literature_pb2.FULL_TEXT_STATE_READY
            else:
                result[doc_id] = literature_pb2.FULL_TEXT_STATE_PENDING
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


# --- Seed parsing: JSON to the corpus above -------------------------------------------------------

_FILE_ROLES = {
    'FIGURE': literature_pb2.FILE_ROLE_FIGURE,
    'SUPPLEMENTARY': literature_pb2.FILE_ROLE_SUPPLEMENTARY,
}


def fixture_backend_from_json(raw: str | None, *, var_name: str) -> FixtureBackend:
    """Build a ``FixtureBackend`` from a JSON corpus, or ``SystemExit`` naming ``var_name``.

    The seed is a JSON object mapping each canonical doc_id to a paper:

        {"<doc_id>": {
            "title": "...",
            "markdown": {"gcs_uri": "gs://...", "from_xml": true},   // optional
            "pdf": {"gcs_uri": "gs://..."},                          // optional
            "files": [{"name": "f1.png", "role": "FIGURE", "media_type": "image/png",
                       "gcs_uri": "gs://..."}],
            "markdown_locations": {"<quote>": [start, end]},
            "pdf_locations": {"<quote>": {"page": 0, "rects": [[x, y, w, h]]}}
        }}

    An unknown field is rejected rather than dropped: a typo'd key would otherwise seed a paper
    missing exactly the data the test or deploy meant to give it.

    Args:
        raw: The JSON string. ``None`` (an unset env var) is an operator error; pass ``"{}"`` for an
            explicit empty corpus.
        var_name: The source env var, named in the fail-loud error messages.

    Returns:
        A backend over the seeded corpus.

    Raises:
        SystemExit: ``raw`` is absent, is not JSON, or does not match the schema above.
    """
    if raw is None:
        raise SystemExit(
            f'{var_name} is required for the fixture backend: a JSON object of doc_id -> paper, '
            'or "{}" for an explicit empty corpus'
        )
    try:
        seeds = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f'{var_name} is not valid JSON: {e}') from e
    if not isinstance(seeds, dict):
        raise SystemExit(f'{var_name} must be a JSON object of doc_id -> paper, got {type(seeds).__name__}')
    return FixtureBackend({doc_id: _parse_paper(var_name, doc_id, paper) for doc_id, paper in seeds.items()})


def _parse_paper(var_name: str, doc_id: str, paper: object) -> SeededPaper:
    if not isinstance(paper, dict):
        raise SystemExit(f'{var_name} paper {doc_id!r} must be a JSON object')
    unknown = set(paper) - {'title', 'files', 'markdown', 'markdown_locations', 'pdf', 'pdf_locations'}
    if unknown:
        raise SystemExit(f'{var_name} paper {doc_id!r} has unknown field(s) {sorted(unknown)}')
    title = paper.get('title')
    if not isinstance(title, str) or not title:
        raise SystemExit(f'{var_name} paper {doc_id!r} must set a non-empty "title"')
    return SeededPaper(
        title=title,
        files=tuple(
            _parse_file(var_name, doc_id, f) for f in _as_list(var_name, doc_id, 'files', paper.get('files', []))
        ),
        markdown_gcs_uri=_rendering_uri(var_name, doc_id, 'markdown', paper.get('markdown')),
        markdown_from_xml=_markdown_from_xml(var_name, doc_id, paper.get('markdown')),
        pdf_gcs_uri=_rendering_uri(var_name, doc_id, 'pdf', paper.get('pdf')),
        markdown_locations=_parse_offsets(var_name, doc_id, paper.get('markdown_locations', {})),
        pdf_locations=_parse_pdf_locations(var_name, doc_id, paper.get('pdf_locations', {})),
    )


def _as_list(var_name: str, doc_id: str, key: str, value: object) -> list[object]:
    if not isinstance(value, list):
        raise SystemExit(f'{var_name} paper {doc_id!r} field {key!r} must be a JSON array')
    return value


def _parse_file(var_name: str, doc_id: str, f: object) -> SeededFile:
    if not isinstance(f, dict):
        raise SystemExit(f'{var_name} paper {doc_id!r} file must be a JSON object')
    role_name = f.get('role')
    if role_name not in _FILE_ROLES:
        raise SystemExit(f'{var_name} paper {doc_id!r} file "role" must be one of {sorted(_FILE_ROLES)}')
    for field in ('name', 'media_type', 'gcs_uri'):
        if not isinstance(f.get(field), str) or not f[field]:
            raise SystemExit(f'{var_name} paper {doc_id!r} file must set a non-empty {field!r}')
    return SeededFile(name=f['name'], role=_FILE_ROLES[role_name], media_type=f['media_type'], gcs_uri=f['gcs_uri'])


def _rendering_uri(var_name: str, doc_id: str, key: str, rendering: object) -> str | None:
    if rendering is None:
        return None
    if not isinstance(rendering, dict) or not isinstance(rendering.get('gcs_uri'), str):
        raise SystemExit(f'{var_name} paper {doc_id!r} {key!r} must be an object with a "gcs_uri"')
    allowed = {'gcs_uri', 'from_xml'} if key == 'markdown' else {'gcs_uri'}
    unknown = set(rendering) - allowed
    if unknown:
        raise SystemExit(f'{var_name} paper {doc_id!r} {key!r} has unknown field(s) {sorted(unknown)}')
    return rendering['gcs_uri']


def _markdown_from_xml(var_name: str, doc_id: str, markdown: object) -> bool:
    if markdown is None:
        return False
    if not isinstance(markdown, dict):
        raise SystemExit(f'{var_name} paper {doc_id!r} "markdown" must be a JSON object')
    from_xml = markdown.get('from_xml', False)
    if not isinstance(from_xml, bool):
        raise SystemExit(f'{var_name} paper {doc_id!r} markdown "from_xml" must be a boolean')
    return from_xml


def _parse_offsets(var_name: str, doc_id: str, locations: object) -> dict[str, tuple[int, int]]:
    if not isinstance(locations, dict):
        raise SystemExit(f'{var_name} paper {doc_id!r} "markdown_locations" must be a JSON object')
    parsed: dict[str, tuple[int, int]] = {}
    for quote, offsets in locations.items():
        if not isinstance(offsets, list) or len(offsets) != 2 or not all(isinstance(n, int) for n in offsets):
            raise SystemExit(f'{var_name} paper {doc_id!r} markdown_locations[{quote!r}] must be [start, end]')
        parsed[quote] = (offsets[0], offsets[1])
    return parsed


def _parse_pdf_locations(var_name: str, doc_id: str, locations: object) -> dict[str, SeededPdfLocation]:
    if not isinstance(locations, dict):
        raise SystemExit(f'{var_name} paper {doc_id!r} "pdf_locations" must be a JSON object')
    parsed: dict[str, SeededPdfLocation] = {}
    for quote, location in locations.items():
        if not isinstance(location, dict) or not isinstance(location.get('page'), int):
            raise SystemExit(f'{var_name} paper {doc_id!r} pdf_locations[{quote!r}] must set an integer "page"')
        rects_raw = location.get('rects', [])
        if not isinstance(rects_raw, list):
            raise SystemExit(f'{var_name} paper {doc_id!r} pdf_locations[{quote!r}] "rects" must be an array')
        rects: list[tuple[float, float, float, float]] = []
        for rect in rects_raw:
            if not isinstance(rect, list) or len(rect) != 4 or not all(isinstance(n, (int, float)) for n in rect):
                raise SystemExit(f'{var_name} paper {doc_id!r} pdf_locations[{quote!r}] rect must be [x, y, w, h]')
            rects.append((float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])))
        parsed[quote] = SeededPdfLocation(page=location['page'], rects=tuple(rects))
    return parsed
