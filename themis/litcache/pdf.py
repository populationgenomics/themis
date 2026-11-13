"""Read a pdf source: page rasters, a recoverable text layer, and any embedded DOI.

A `pdf-derived` rendering reconstructs structure from the pdf, but recovering a
source quote's bounding box in that rendering needs the pdf's character layer:
positioned glyphs the extractor can map a quote back to. An image-only pdf
(scanned page, rasterized text) has no such layer, so quote→bbox is infeasible
and the paper is flagged for follow-up.

This module records `has_text_layer` per pdf source (a diagnostic only), and
harvests a DOI from the pdf's embedded metadata (`doi_from_metadata`) — a
publisher-populated id that identity uses when the bucket key and Docling origin
carry no external scheme (e.g. an Elsevier deposit keyed only by its PII). The DOI
is read from the XMP packet (`prism:doi` / `dc:identifier` — the authoritative
publisher field) in preference to a document-info string, and never from the body
text (a body match is as likely to be a cited reference as the work's own id).
XML-backed papers map a quote to the XML, so the text-layer probe does not apply to
them; the caller omits it when XML is the source of truth (`Source.has_text_layer`
stays unset).

`render_pages` rasterizes for the vision models. The size to render a page at is the
provider's to choose, not this module's, so it comes in as a `PageFit`.

pdfium keeps state for the whole process, is not safe to call from two threads at once, and can
crash or exhaust memory on a malformed pdf. So every operation here is a job, run one at a time in a
child process that owns pdfium. Each public function submits its job and returns a
`concurrent.futures.Future`: a synchronous caller takes `.result()`, and an async caller awaits
`asyncio.wrap_future(...)`, which leaves its event loop free while the job runs. The future raises
what the job raised. If the child dies or hangs during a job, that job's future raises
`PdfiumCrashedError` and a fresh child takes the jobs queued behind it, so a pdf that crashes pdfium
costs only its own job, and the server that submitted it lives on. A child that cannot be started
fails the job with `PdfiumUnavailableError` instead, which says nothing about the pdf
(`docs/design/evidence-fulltext.md`).
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import datetime
import html
import io
import multiprocessing
import multiprocessing.connection
import multiprocessing.process
import pathlib
import queue
import re
import signal
import threading
from collections.abc import Callable, Generator
from typing import TypeVar

import pypdfium2

_T = TypeVar('_T')

# Written in the child so that, when its container runs out of memory, the kernel kills the child
# rendering a pdf rather than the server holding every other request. Absent off Linux.
_OOM_SCORE_ADJ = pathlib.Path('/proc/self/oom_score_adj')
_PREFERRED_OOM_VICTIM = '1000'
# The child's first message: bootstrapped and ready for a job, so a death after it is a job's doing.
_READY = 'ready'
# How long a child may take to start: a spawned interpreter re-imports the parent's main module.
_START_TIMEOUT = datetime.timedelta(minutes=2)
# How long one job may run. A render of a long, heavy paper takes seconds to a minute; a job still
# running at this is a pdf that hangs pdfium.
_JOB_TIMEOUT = datetime.timedelta(minutes=10)
# A job running when the child was SIGKILLed is run again this many times before it is blamed.
_KILLED_RETRIES = 1
# How many fresh children a job may go through without one ever receiving it, before the process is
# taken to be failing for reasons of its own.
_UNSEEN_REPLACEMENTS = 2
# How long to wait for a child to exit at each step of stopping it.
_EXIT_GRACE = datetime.timedelta(seconds=5)

# A pdf has a text layer if pypdfium2 recovers at least one positioned glyph
# across its pages. Image-only pdfs (text drawn as pixels, no text operators)
# yield zero; any genuine text layer yields a positive count.
_MIN_TEXT_CHARS = 1

# A DOI in a document-info string (e.g. an Elsevier `Subject`: "…doi:10.1016/j.scr.2025.103712").
_DOI_RE = re.compile(r'10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+')

# The document-info keys a publisher embeds a DOI in, in preference order.
_INFO_DOI_KEYS = ('DOI', 'Subject', 'Title', 'Keywords')

# The XMP metadata tags carrying the DOI, in preference order. `prism:doi` is the
# journal-DOI field; `dc:identifier` is more general (its value may be prefixed
# `doi:`). The packet is a plaintext XML stream in the pdf, so it is matched against
# the raw bytes — scoped to these tag names, not a DOI anywhere in the file.
_XMP_DOI_TAGS = (b'prism:doi', b'dc:identifier')


PageFit = Callable[[float, float], tuple[int, int]]
"""Maps a page's `(width, height)` in points to the pixel size to rasterize it at.

A model that shrinks an oversized image reads the shrunken one, so the size it would have chosen is
the size at which a page is read as rendered. It travels with the render job, so a caller passes one
that pickles — a module-level function, or an instance of a module-level class — for the job to stay
movable to another process."""


@dataclasses.dataclass(frozen=True)
class PageImage:
    """One rasterized pdf page.

    Attributes:
        number: The 1-based page number, as printed in a citation rather than an array index.
        width: The page area's width in pixels.
        height: The page area's height in pixels.
        png: The encoded image.
    """

    number: int
    width: int
    height: int
    png: bytes


@dataclasses.dataclass(frozen=True)
class PageLayout:
    """One pdf page's size and text layer.

    Attributes:
        number: The 1-based page number.
        width: The page's width in points.
        height: The page's height in points.
        text: The page's text layer; empty for a scanned page with none.
    """

    number: int
    width: float
    height: float
    text: str


class PdfiumCrashedError(pypdfium2.PdfiumError):
    """This job's pdf crashes pdfium, hangs it, or exhausts memory rendering.

    A `pypdfium2.PdfiumError`, so a caller that settles a pdf it cannot load settles this one too: the
    same bytes would do the same again.
    """


class PdfiumUnavailableError(RuntimeError):
    """The pdfium process could not be started; nothing is known about the pdf.

    Deliberately not a `pypdfium2.PdfiumError`: a broken environment must fail the request, to be
    retried, never settle a paper as unreadable.
    """


class _ChildGoneError(Exception):
    """The child died. `ran` says whether it had been handed the job, and so whether the job can be blamed."""

    def __init__(self, exitcode: int | None, *, ran: bool) -> None:
        super().__init__(f'exit code {exitcode}')
        self.exitcode = exitcode
        self.ran = ran


def _become_oom_victim() -> None:
    if _OOM_SCORE_ADJ.exists():
        _OOM_SCORE_ADJ.write_text(_PREFERRED_OOM_VICTIM)


def _child_main(conn: multiprocessing.connection.Connection) -> None:
    """Say ready, then run each job `conn` delivers, answering `(True, result)` or `(False, exception)`."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # a terminal's Ctrl-C is the parent's to handle
    _become_oom_victim()
    conn.send(_READY)
    while True:
        try:
            job, args = conn.recv()
        except EOFError:
            return
        except Exception as e:  # noqa: BLE001 — a job that will not unpickle here fails that job, not the child
            conn.send((False, RuntimeError(f'the job could not be rebuilt in the pdfium process: {e!r}')))
            continue
        try:
            reply: tuple[bool, object] = (True, job(*args))
        except Exception as e:  # noqa: BLE001 — any failure is the submitter's, carried back to its future
            reply = (False, e)
        try:
            conn.send(reply)
        except Exception as e:  # noqa: BLE001 — an answer that will not pickle fails its job, not the child
            conn.send((False, RuntimeError(f'the job answered with something that does not pickle: {e!r}')))


@dataclasses.dataclass(frozen=True)
class _Child:
    """A running pdfium process and the parent's end of its pipe."""

    process: multiprocessing.process.BaseProcess
    conn: multiprocessing.connection.Connection

    @classmethod
    def start(cls) -> _Child:
        """Start a child and wait until it is ready for a job.

        Raises:
            PdfiumUnavailableError: The child could not be started, or died or stalled before it was ready.
        """
        # Spawned, not forked: the parent has threads, and a fork copies whatever locks they hold.
        context = multiprocessing.get_context('spawn')
        parent_end, child_end = context.Pipe()
        process = context.Process(target=_CHILD_TARGET, args=(child_end,), name='pdfium', daemon=True)
        try:
            process.start()
        except Exception as e:
            parent_end.close()
            raise PdfiumUnavailableError(f'the pdfium process would not start: {e!r}') from e
        finally:
            child_end.close()
        child = cls(process=process, conn=parent_end)
        try:
            ready = parent_end.poll(_START_TIMEOUT.total_seconds()) and parent_end.recv() == _READY
        except (EOFError, OSError):
            ready = False
        if not ready:
            child.stop(exiting=True)
            raise PdfiumUnavailableError(f'the pdfium process was not ready (exit code {process.exitcode})')
        return child

    def run(self, job: Callable[..., object], args: tuple[object, ...]) -> tuple[bool, object]:
        """Run one job in the child.

        Raises:
            _ChildGoneError: The child was dead before the job reached it, or died running it.
            PdfiumCrashedError: The job outlasted `_JOB_TIMEOUT`; the child has been stopped.
            Exception: The job or its arguments do not pickle; the child never saw it.
        """
        if not self.process.is_alive():
            self.stop(exiting=True)
            raise _ChildGoneError(self.process.exitcode, ran=False)
        try:
            self.conn.send((job, args))
        except (BrokenPipeError, ConnectionResetError, EOFError) as e:
            self.stop(exiting=True)
            raise _ChildGoneError(self.process.exitcode, ran=False) from e
        try:
            answered = self.conn.poll(_JOB_TIMEOUT.total_seconds())
        except (EOFError, OSError):
            answered = True  # the read below reports the death
        if not answered:
            self.stop(exiting=False)
            raise PdfiumCrashedError(f'pdfium did not finish this job within {_JOB_TIMEOUT}')
        try:
            return self.conn.recv()
        except (EOFError, OSError) as e:
            self.stop(exiting=True)
            raise _ChildGoneError(self.process.exitcode, ran=True) from e

    def stop(self, *, exiting: bool) -> None:
        """End the child, if it is still running, and release its pipe.

        A child `exiting` (its pipe has closed) is given a moment to finish, so its own exit code is the
        one reported. One that ignores SIGTERM is killed; one that will not die even then is abandoned
        rather than left to block the dispatcher.
        """
        if exiting:
            self.process.join(_EXIT_GRACE.total_seconds())
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(_EXIT_GRACE.total_seconds())
        if self.process.is_alive():
            self.process.kill()
            self.process.join(_EXIT_GRACE.total_seconds())
        self.conn.close()


class _PdfiumProcess:
    """Runs jobs one at a time in a child process, starting a fresh child after one dies.

    A dispatcher thread hands the child one job and waits for its answer before the next, so when the
    child dies the job it was running is known, and only that job's future fails. Both the thread and
    the child start on the first submit. The dispatcher settles every future it takes, whatever goes
    wrong, so no caller waits forever.
    """

    def __init__(self) -> None:
        self._jobs: queue.SimpleQueue[
            tuple[concurrent.futures.Future[object], Callable[..., object], tuple[object, ...]]
        ] = queue.SimpleQueue()
        self._start = threading.Lock()
        self._dispatcher: threading.Thread | None = None
        self._child: _Child | None = None

    def submit(self, job: Callable[..., _T], /, *args: object) -> concurrent.futures.Future[_T]:
        future: concurrent.futures.Future[_T] = concurrent.futures.Future()
        self._jobs.put((future, job, args))  # pyright: ignore[reportArgumentType]
        with self._start:
            if self._dispatcher is None:
                self._dispatcher = threading.Thread(target=self._dispatch, name='pdfium-dispatch', daemon=True)
                self._dispatcher.start()
        return future

    def _dispatch(self) -> None:
        while True:
            future, job, args = self._jobs.get()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                ok, value = self._run(job, args)
            except Exception as e:  # noqa: BLE001 — every failure belongs to this job's future, never to the thread
                future.set_exception(e)
                continue
            if ok:
                future.set_result(value)
            else:
                future.set_exception(value)  # pyright: ignore[reportArgumentType]

    def _run(self, job: Callable[..., object], args: tuple[object, ...]) -> tuple[bool, object]:
        """Run a job, replacing the child as often as the job's outcome allows.

        A child found dead before it saw the job is replaced and the job resent. A child killed outright
        (SIGKILL: out of memory, or killed from outside) while running the job may have been killed for
        the server's memory rather than the job's, so the job is run once more before it is blamed. Any
        other death, such as a segfault or an abort, is the job's.
        """
        killed = 0
        replaced = 0
        while True:
            if self._child is None:
                self._child = _Child.start()
            try:
                return self._child.run(job, args)
            except _ChildGoneError as e:
                self._child = None
                if not e.ran:
                    replaced += 1
                    if replaced > _UNSEEN_REPLACEMENTS:
                        raise PdfiumUnavailableError(
                            f'the pdfium process died {replaced} times before this job reached it ({e})'
                        ) from e
                    continue
                if e.exitcode == -signal.SIGKILL and killed < _KILLED_RETRIES:
                    killed += 1
                    continue
                raise PdfiumCrashedError(f'pdfium died running this job ({e})') from e
            except PdfiumCrashedError:
                self._child = None
                raise


# What the child runs; a module global so a test can start a child that fails before it is ready.
_CHILD_TARGET: Callable[[multiprocessing.connection.Connection], None] = _child_main

# The one process pdfium is ever called from: pdfium's state is per process, so a second runner would
# be a second caller inside it.
_PDFIUM = _PdfiumProcess()


@contextlib.contextmanager
def _document(pdf_bytes: bytes) -> Generator[pypdfium2.PdfDocument]:
    """Open a pdf; called only from a job, so only in the pdfium process."""
    with pypdfium2.PdfDocument(pdf_bytes) as doc:
        yield doc


def _render_page(page: pypdfium2.PdfPage, number: int, fit: PageFit) -> PageImage:
    """Rasterize one open page to a png at the size `fit` asks for."""
    width_pt, height_pt = page.get_size()
    if width_pt <= 0 or height_pt <= 0:
        raise ValueError(f'page {number} is {width_pt}x{height_pt} points')
    width_px, height_px = fit(width_pt, height_pt)
    if width_px <= 0 or height_px <= 0:
        raise ValueError(f'fit returned a {width_px}x{height_px} raster for page {number}')
    # Rendered at the target rather than rendered large and downsampled: page content is mostly
    # vector, which re-renders sharp at any size. pdfium rounds the height its own way, so the
    # result is snapped to the fitted size — a resample of a pixel or two, not of a scale factor.
    image = page.render(scale=width_px / width_pt).to_pil().resize((width_px, height_px))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return PageImage(number=number, width=width_px, height=height_px, png=buffer.getvalue())


def _text_layer(pdf_bytes: bytes) -> str:
    parts: list[str] = []
    with _document(pdf_bytes) as doc:
        for page in doc:
            with contextlib.closing(page), contextlib.closing(page.get_textpage()) as textpage:
                parts.append(textpage.get_text_bounded())
    return '\n'.join(parts)


def _render_region(pdf_bytes: bytes, page: int, box: tuple[float, float, float, float], max_edge: int) -> PageImage:
    left, top, right, bottom = box
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError(f'the box {box} is not a region of the page')
    if max_edge <= 0:
        raise ValueError(f'max_edge {max_edge} is not positive')
    with _document(pdf_bytes) as doc:
        if not 1 <= page <= len(doc):
            raise ValueError(f'the pdf has no page {page}')
        with contextlib.closing(doc[page - 1]) as pdf_page:
            width_pt, height_pt = pdf_page.get_size()
            region_w, region_h = (right - left) * width_pt, (bottom - top) * height_pt
            scale = max_edge / max(region_w, region_h)
            # pdfium crops in points from each edge, before scaling.
            crop = (left * width_pt, (1 - bottom) * height_pt, (1 - right) * width_pt, top * height_pt)
            image = pdf_page.render(scale=scale, crop=crop).to_pil()
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return PageImage(number=page, width=image.width, height=image.height, png=buffer.getvalue())


def _doi_from_xmp(pdf_bytes: bytes) -> str | None:
    """Read a DOI from the XMP packet's `prism:doi` / `dc:identifier`, if present."""
    for tag in _XMP_DOI_TAGS:
        # Element (`>value`) or attribute (`="value"`) form; an optional `doi:` prefix
        # (dc:identifier). Anchored to the tag, so a body-text DOI can't match.
        match = re.search(tag + rb'\s*[>=]\s*["\']?(?:doi:)?\s*(10\.\d{4,9}/[^\s"\'<]+)', pdf_bytes)
        if match is not None:
            # XMP is XML, so a SICI DOI's `<` and `>` arrive escaped.
            return html.unescape(match.group(1).rstrip(b'.').decode('ascii', 'ignore'))
    return None


def _doi_from_info_dict(pdf_bytes: bytes) -> str | None:
    """Read a DOI from the pdf document-info fields (`_INFO_DOI_KEYS`), if present."""
    with _document(pdf_bytes) as doc:
        for key in _INFO_DOI_KEYS:
            value = doc.get_metadata_value(key)
            match = _DOI_RE.search(value) if value else None
            if match is not None:
                return match.group(0).rstrip('.')
    return None


def _count_chars(pdf_bytes: bytes) -> int:
    """Total positioned characters across all pages of the pdf."""
    total = 0
    with _document(pdf_bytes) as doc:
        for page in doc:
            with contextlib.closing(page):
                textpage = page.get_textpage()
                with contextlib.closing(textpage):
                    total += textpage.count_chars()
    return total


def _page_count(pdf_bytes: bytes) -> int:
    with _document(pdf_bytes) as doc:
        return len(doc)


def _page_layouts(pdf_bytes: bytes) -> list[PageLayout]:
    layouts: list[PageLayout] = []
    with _document(pdf_bytes) as doc:
        for index, page in enumerate(doc, start=1):
            with contextlib.closing(page), contextlib.closing(page.get_textpage()) as textpage:
                width, height = page.get_size()
                layouts.append(PageLayout(number=index, width=width, height=height, text=textpage.get_text_bounded()))
    return layouts


def _render_pages(pdf_bytes: bytes, fit: PageFit) -> list[PageImage]:
    pages: list[PageImage] = []
    with _document(pdf_bytes) as doc:
        for index, page in enumerate(doc, start=1):
            with contextlib.closing(page):
                pages.append(_render_page(page, index, fit))
    return pages


def _probe_has_text_layer(pdf_bytes: bytes) -> bool:
    return _count_chars(pdf_bytes) >= _MIN_TEXT_CHARS


def _leading_text(pdf_bytes: bytes, pages: int) -> str:
    texts = []
    with _document(pdf_bytes) as doc:
        for index in range(min(pages, len(doc))):
            page = doc[index]
            with contextlib.closing(page):
                textpage = page.get_textpage()
                with contextlib.closing(textpage):
                    texts.append(textpage.get_text_bounded())
    return '\n'.join(texts)


def _doi_from_metadata(pdf_bytes: bytes) -> str | None:
    if not pdf_bytes:  # a degenerate empty source has no metadata to read
        return None
    return _doi_from_xmp(pdf_bytes) or _doi_from_info_dict(pdf_bytes)


def page_count(pdf_bytes: bytes) -> concurrent.futures.Future[int]:
    """How many pages a pdf has.

    Read before rasterizing: a provider's page size can depend on how many images the request will
    carry, which it cannot know from one page's dimensions.

    Returns:
        A future of the page count. It raises `pypdfium2.PdfiumError` if the bytes are not a loadable
        pdf.
    """
    return _PDFIUM.submit(_page_count, pdf_bytes)


def render_pages(pdf_bytes: bytes, *, fit: PageFit) -> concurrent.futures.Future[list[PageImage]]:
    """Rasterize every page of a pdf at the size its reader wants.

    Args:
        pdf_bytes: The raw pdf source bytes.
        fit: Maps a page's size in points to the pixel size to render it at (`PageFit`).

    Returns:
        A future of one `PageImage` per page, in page order. It raises `pypdfium2.PdfiumError` if the
        bytes are not a loadable pdf, and `ValueError` if a page has a non-positive size or `fit`
        returns a non-positive dimension.
    """
    return _PDFIUM.submit(_render_pages, pdf_bytes, fit)


def probe_has_text_layer(pdf_bytes: bytes) -> concurrent.futures.Future[bool]:
    """Whether a pdf carries a recoverable text layer.

    Args:
        pdf_bytes: The raw pdf source bytes.

    Returns:
        A future of True if pypdfium2 recovers any positioned characters (quote→bbox feasible), or
        False for an image-only pdf with no text layer. It raises `pypdfium2.PdfiumError` if the
        bytes are not a loadable pdf.
    """
    return _PDFIUM.submit(_probe_has_text_layer, pdf_bytes)


def leading_text(pdf_bytes: bytes, *, pages: int) -> concurrent.futures.Future[str]:
    """The text layer of the pdf's first `pages` pages, a page to a line; "" for an image-only pdf.

    Args:
        pdf_bytes: The raw pdf source bytes.
        pages: How many pages to read from the start; a shorter pdf is read whole.

    Returns:
        A future of the text. It raises `pypdfium2.PdfiumError` if the bytes are not a loadable pdf.
    """
    return _PDFIUM.submit(_leading_text, pdf_bytes, pages)


def doi_from_metadata(pdf_bytes: bytes) -> concurrent.futures.Future[str | None]:
    """Harvest a DOI from the pdf's embedded metadata, if one is present.

    Identity enrichment for id-poor deposits: publishers populate the DOI into the
    XMP packet (`prism:doi` / `dc:identifier`) and often a document-info field, so a
    deposit whose only external id is an opaque PII still carries its DOI here. XMP
    is preferred (authoritative); the document-info fields are the fallback.

    Args:
        pdf_bytes: The raw pdf source bytes.

    Returns:
        A future of the embedded DOI (bare, no `doi:` prefix), or of None when the pdf has no bytes
        or carries no DOI in either place. It raises `pypdfium2.PdfiumError` if the (non-empty) bytes
        are not a loadable pdf and no XMP DOI was found (the document-info fallback loads the pdf).
    """
    return _PDFIUM.submit(_doi_from_metadata, pdf_bytes)


def text_layer(pdf_bytes: bytes) -> concurrent.futures.Future[str]:
    """Every page's text layer, in page order; empty for a scan with none.

    Returns:
        A future of the text. It raises `pypdfium2.PdfiumError` if the bytes are not a loadable pdf.
    """
    return _PDFIUM.submit(_text_layer, pdf_bytes)


def page_layouts(pdf_bytes: bytes) -> concurrent.futures.Future[list[PageLayout]]:
    """Each page's size in points and its text layer, in page order.

    Returns:
        A future of the layouts. It raises `pypdfium2.PdfiumError` if the bytes are not a loadable pdf.
    """
    return _PDFIUM.submit(_page_layouts, pdf_bytes)


def render_region(
    pdf_bytes: bytes, *, page: int, box: tuple[float, float, float, float], max_edge: int
) -> concurrent.futures.Future[PageImage]:
    """Rasterize part of one page, enlarged so its longer side is `max_edge` pixels.

    Args:
        pdf_bytes: The raw pdf source bytes.
        page: The 1-based page number.
        box: The region as fractions of the page, `(left, top, right, bottom)`, each in 0..1, measured
            from the page's top-left corner.
        max_edge: The longer side of the result, in pixels.

    Returns:
        A future of the region as a png, its `number` the page's. It raises `pypdfium2.PdfiumError` if
        the bytes are not a loadable pdf, and `ValueError` if the page does not exist, or the box is
        empty or outside the page.
    """
    return _PDFIUM.submit(_render_region, pdf_bytes, page, box, max_edge)
