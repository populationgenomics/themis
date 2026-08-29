"""The litcache-reading literature backend (docs/design/document-pane.md §Backend seam).

Resolves a canonical ``doc_id`` against the real litcache GCS layout: reads ``papers/{doc_id}/
manifest.pb``, picks the canonical rendering (xml-faithful over pdf-derived), and names the GCS
object for a rendering, the current PDF revision, or an associated file. ``locate``/``validate`` run
anchorite's quote-to-offset location over the rendering bytes — manifest +
rendering only, never ``metadata.pb`` (that is read solely for ``describe_paper``'s title, and its
absence falls back rather than failing). ``validate`` is markdown-only and forgiving: a PDF-only
paper is reported unknown-not-checked, never a false "not located".

The bucket is read-only: the backend never writes to it (the cache warms via the ingestion pipeline
and the convert worker). Its one write of any kind is ``request_conversions``, which asks
``litcache.enqueue`` for a conversion task per paper; what this adapter adds is deciding which of the
Cloud Tasks failures the servicer should offer a caller a retry for. An
associated file the manifest lists without a ``path`` is not yet fetched; resolving it raises
``MissingContentError`` rather than fetching-and-writing (deferred — the seed corpus has every file
fetched). PDF quote location (anchorite page regions) is not implemented; ``locate`` for a PDF raises
``PdfLocationUnavailableError`` rather than reporting a false ``not_located``.

Blocking GCS I/O is offloaded with ``asyncio.to_thread`` so it never stalls the ``grpc.aio`` loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import mimetypes
from collections.abc import Callable, Sequence
from typing import override

import anchorite
from google.api_core import exceptions as api_exceptions
from google.cloud import storage
from google.protobuf import message as _message
from pubmed_proto import pubmed_pb2

from themis.common import sql
from themis.litcache import crosswalk, enqueue, outcome, writer
from themis.litcache.models import litcache_pb2
from themis.rpc import literature_pb2
from themis.services.evidence.literature import backend as literature_backend

_logger = logging.getLogger(__name__)

# outcome.Readiness -> the wire state; None (no manifest) is an unknown paper.
_READINESS_STATE: dict[outcome.Readiness | None, literature_pb2.FullTextState] = {
    None: literature_pb2.FULL_TEXT_STATE_UNKNOWN_PAPER,
    outcome.Readiness.READY: literature_pb2.FULL_TEXT_STATE_READY,
    outcome.Readiness.PENDING: literature_pb2.FULL_TEXT_STATE_PENDING,
    outcome.Readiness.NO_FULL_TEXT: literature_pb2.FULL_TEXT_STATE_NO_FULL_TEXT,
    outcome.Readiness.FAILED: literature_pb2.FULL_TEXT_STATE_FAILED,
}

_METADATA = 'metadata.pb'

# Server errors that are facts about the service rather than its load, so they are checked ahead of
# the transient base they sit under.
_PERMANENT_SERVER_ERRORS = (api_exceptions.MethodNotImplemented, api_exceptions.DataLoss)
# A failed create repeating could place: refused on load or reachability, not on what it asked for.
_TRANSIENT_ENQUEUE_ERRORS = (
    api_exceptions.ServerError,  # every 5xx and gRPC UNKNOWN: the API's side, not the request's
    api_exceptions.TooManyRequests,  # 429, and `ResourceExhausted` beneath it — the queue's own ceiling
    api_exceptions.Aborted,  # a concurrent write to the same queue lost
    api_exceptions.RetryError,  # api-core gave up on a retryable code
)

# Canonical-rendering preference: an xml-faithful litdown rendering over a pdf-derived one, and
# llm-ocr over the legacy docling route (litcache-manifest.md — fidelity is a read-path policy).
_CONVERTER_RANK = {
    litcache_pb2.Converter.CONVERTER_LITDOWN: 0,
    litcache_pb2.Converter.CONVERTER_LLM_OCR: 1,
    litcache_pb2.Converter.CONVERTER_DOCLING: 2,
}
_CONVERTER_RANK_MISS = max(_CONVERTER_RANK.values()) + 1

# litcache associated-file role -> evidence FileRole (values align; mapped by name to stay explicit).
_FILE_ROLE = {
    litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_FIGURE: literature_pb2.FILE_ROLE_FIGURE,
    litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_SUPPLEMENTARY: literature_pb2.FILE_ROLE_SUPPLEMENTARY,
}


@dataclasses.dataclass(frozen=True)
class _Paper:
    """A paper's parsed litcache record: its manifest and the canonical rendering (if any)."""

    manifest: litcache_pb2.Manifest
    rendering: tuple[str, litcache_pb2.Rendering] | None  # (hash, rendering) — the canonical one


class LitcacheBackend(literature_backend.LiteratureBackend):
    """Serve the literature rpcs by reading the litcache directory for each ``doc_id``."""

    def __init__(
        self,
        bucket: storage.Bucket,
        *,
        connect: Callable[[], sql.Connection] | None = None,
        enqueuer: enqueue.Enqueuer | None = None,
    ) -> None:
        self._bucket = bucket
        self._connect = connect
        self._enqueuer = enqueuer

    @override
    async def describe_paper(self, doc_id: str) -> literature_pb2.PaperInfo:
        paper, metadata = await asyncio.to_thread(self._read_for_describe, doc_id)
        has_markdown = paper.rendering is not None
        has_pdf = _source_by_format(paper.manifest, litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF) is not None
        markdown_from_xml = paper.rendering is not None and _renders_from_xml(paper.manifest, paper.rendering[1])
        return literature_pb2.PaperInfo(
            doc_id=doc_id,
            title=_title(metadata, paper.manifest, doc_id),
            has_markdown=has_markdown,
            markdown_from_xml=markdown_from_xml,
            has_pdf=has_pdf,
            default_representation=literature_backend.default_representation(has_markdown, markdown_from_xml, has_pdf),
            files=[
                literature_pb2.FileInfo(
                    name=f.name,
                    role=_FILE_ROLE.get(f.role, literature_pb2.FILE_ROLE_UNSPECIFIED),
                    media_type=_media_type_for(f.name or f.path),
                )
                for f in paper.manifest.files
            ],
        )

    @override
    async def resolve_content(
        self, doc_id: str, selector: literature_backend.ContentSelector
    ) -> literature_pb2.ContentLocation:
        paper = await asyncio.to_thread(self._read_manifest, doc_id)
        match selector:
            case literature_backend.MarkdownContent():
                if paper.rendering is None:
                    raise literature_backend.MissingContentError(f'{doc_id} has no markdown rendering')
                rendering_hash, _ = paper.rendering
                return self._location(doc_id, f'renderings/{rendering_hash}.md', 'text/markdown')
            case literature_backend.PdfContent():
                source = _source_by_format(paper.manifest, litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF)
                if source is None:
                    raise literature_backend.MissingContentError(f'{doc_id} has no PDF')
                revision = _current_revision(source)
                return self._location(doc_id, f'sources/{source.handle}/{revision.hash}.pdf', 'application/pdf')
            case literature_backend.FileContent(name=name):
                file = next((f for f in paper.manifest.files if f.name == name), None)
                if file is None:
                    raise literature_backend.MissingContentError(f'{doc_id} has no file {name!r}')
                if not file.path:
                    raise literature_backend.MissingContentError(f'{doc_id} file {name!r} is not fetched')
                return self._location(doc_id, file.path, _media_type_for(file.path))
            case _:  # unreachable over the closed ContentSelector union — fail loud if it ever opens
                raise ValueError(f'unhandled content selector {selector!r}')

    @override
    async def locate(
        self, doc_id: str, quote: str, representation: literature_pb2.Representation
    ) -> literature_pb2.LocateResponse:
        if representation == literature_pb2.REPRESENTATION_MARKDOWN:
            offsets = await asyncio.to_thread(self._locate_markdown, doc_id, quote)
            if offsets is None:
                return literature_pb2.LocateResponse(not_located=literature_pb2.QuoteNotLocated())
            start, end = offsets
            return literature_pb2.LocateResponse(offsets=literature_pb2.TextOffsets(start=start, end=end))
        if representation == literature_pb2.REPRESENTATION_PDF:
            paper = await asyncio.to_thread(self._read_manifest, doc_id)
            if _source_by_format(paper.manifest, litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF) is None:
                raise literature_backend.RepresentationUnavailableError(f'{doc_id} has no PDF')
            raise literature_backend.PdfLocationUnavailableError(f'{doc_id}: PDF quote location is not yet available')
        raise ValueError(f'unsupported representation {representation!r}')

    @override
    async def validate(self, doc_id: str, quote: str) -> literature_pb2.ValidateResponse:
        # Markdown-only: PDF quote validation is not implemented, so a PDF-only paper is "unknown",
        # not "absent" — the reason says what was and wasn't checked, never a false "not located".
        try:
            offsets = await asyncio.to_thread(self._locate_markdown, doc_id, quote)
        except literature_backend.UnknownPaperError:
            return literature_pb2.ValidateResponse(ok=False, reason=f'unknown doc_id {doc_id!r}')
        except literature_backend.RepresentationUnavailableError:
            return literature_pb2.ValidateResponse(
                ok=False, reason='no markdown rendering to validate against; PDF validation is not yet available'
            )
        except literature_backend.MissingContentError as e:
            return literature_pb2.ValidateResponse(
                ok=False, reason=f'markdown rendering is missing (corpus fault): {e}'
            )
        if offsets is None:
            return literature_pb2.ValidateResponse(ok=False, reason='quote not located in the markdown rendering')
        return literature_pb2.ValidateResponse(ok=True, located_in=[literature_pb2.REPRESENTATION_MARKDOWN])

    @override
    async def resolve_external_ids(self, external_ids: Sequence[str]) -> dict[str, str]:
        if self._connect is None:
            raise literature_backend.CrosswalkNotConfiguredError('no crosswalk is configured')
        if not external_ids:
            return {}  # no dial for an empty batch; `crosswalk.lookup` rejects one outright
        return await asyncio.to_thread(self._lookup, list(external_ids))

    @override
    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        # One to two GCS reads per distinct id: READY derives from the manifest alone, but anything
        # else probes the sidecar too. Gathered so a batch runs concurrently, not serially.
        distinct = list(dict.fromkeys(doc_ids))
        states = await asyncio.gather(*(asyncio.to_thread(self._readiness, doc_id) for doc_id in distinct))
        return dict(zip(distinct, states, strict=True))

    @override
    async def request_conversions(self, doc_ids: Sequence[str]) -> None:
        if not doc_ids:
            return
        if self._enqueuer is None:
            raise literature_backend.ConversionNotConfiguredError('no conversion queue is configured')
        # A named create pays a duplicate lookup, so the batch runs concurrently rather than serially.
        enqueuer = self._enqueuer
        distinct = list(dict.fromkeys(doc_ids))
        outcomes = await asyncio.gather(
            *(asyncio.to_thread(enqueuer.enqueue, doc_id) for doc_id in distinct), return_exceptions=True
        )
        _report(distinct, outcomes)

    # --- GCS reads (synchronous; called via asyncio.to_thread) -----------------------------------

    def _lookup(self, external_ids: Sequence[str]) -> dict[str, str]:
        """One crosswalk read, on a connection opened and closed per call.

        A fresh connection per call, not a pooled one: the lookup is rare next to the GCS reads, and
        Cloud Run scales to zero, so a held connection would idle against the instance's cap far more
        often than it saves a dial.
        """
        if self._connect is None:
            raise literature_backend.CrosswalkNotConfiguredError('no crosswalk is configured')
        try:
            with contextlib.closing(self._connect()) as conn:
                return crosswalk.lookup(conn, external_ids)
        except Exception as e:
            # Any dial or query failure is an outage of the whole batch. Deliberately broad: pg8000
            # and the Cloud SQL connector raise from several unrelated hierarchies (socket, TLS,
            # Admin API, DBAPI), and mistaking any of them for "this paper is not in the corpus" is
            # the failure this method exists to prevent.
            raise literature_backend.CrosswalkUnavailableError(str(e)) from e

    def _readiness(self, doc_id: str) -> literature_pb2.FullTextState:
        return _READINESS_STATE[outcome.read_readiness(self._bucket, doc_id)]

    def _read_manifest(self, doc_id: str) -> _Paper:
        """The paper's manifest + canonical rendering, or ``UnknownPaperError`` if absent."""
        manifest_bytes = self._download(writer.manifest_path(doc_id))
        if manifest_bytes is None:
            raise literature_backend.UnknownPaperError(doc_id)
        manifest = _parse(litcache_pb2.Manifest(), manifest_bytes)
        return _Paper(manifest=manifest, rendering=_select_rendering(manifest))

    def _read_for_describe(self, doc_id: str) -> tuple[_Paper, bytes | None]:
        """The manifest and the ``metadata.pb`` bytes (absent ⇒ the title falls back)."""
        return self._read_manifest(doc_id), self._download(f'{writer.paper_dir(doc_id)}/{_METADATA}')

    def _locate_markdown(self, doc_id: str, quote: str) -> tuple[int, int] | None:
        paper = self._read_manifest(doc_id)
        if paper.rendering is None:
            raise literature_backend.RepresentationUnavailableError(f'{doc_id} has no markdown rendering')
        rendering_hash, _ = paper.rendering
        markdown = self._download(f'{writer.paper_dir(doc_id)}/renderings/{rendering_hash}.md')
        if markdown is None:
            raise literature_backend.MissingContentError(f'{doc_id} rendering {rendering_hash} is missing')
        return anchorite.locate_quote_span(markdown.decode('utf-8'), quote)

    def _download(self, name: str) -> bytes | None:
        blob = self._bucket.blob(name)
        try:
            return blob.download_as_bytes()
        except api_exceptions.NotFound:
            return None

    def _location(self, doc_id: str, rel_path: str, media_type: str) -> literature_pb2.ContentLocation:
        uri = f'gs://{self._bucket.name}/{writer.paper_dir(doc_id)}/{rel_path}'
        return literature_pb2.ContentLocation(gcs_uri=uri, media_type=media_type)


def _report(doc_ids: Sequence[str], outcomes: Sequence[bool | BaseException]) -> None:
    """Log what the batch did and raise the worst failure in it, or nothing if there was none.

    Every failure is logged before one is raised. Gathering with ``return_exceptions`` and choosing
    here rather than letting the first exception win is what makes the choice severity's rather than
    thread scheduling's: a permanent refusal in a batch that also hit an outage would otherwise be
    discarded unlogged, and the call would report a transient condition the caller retries forever.

    Args:
        doc_ids: The papers a task was attempted for, in the order the outcomes are in.
        outcomes: Per paper, whether a task was created, or the exception the create raised.

    Raises:
        ConversionUnavailableError: some create failed and none failed permanently.
        ConversionEnqueueFailedError: some create failed permanently.
    """
    created = [doc_id for doc_id, outcome in zip(doc_ids, outcomes, strict=True) if outcome is True]
    deduped = [doc_id for doc_id, outcome in zip(doc_ids, outcomes, strict=True) if outcome is False]
    failed = [
        (doc_id, outcome)
        for doc_id, outcome in zip(doc_ids, outcomes, strict=True)
        if isinstance(outcome, BaseException)
    ]
    _logger.info(
        'conversion requests: %d enqueued, %d already queued %s, %d failed',
        len(created),
        len(deduped),
        deduped,
        len(failed),
    )
    worst: Exception | None = None
    for doc_id, error in failed:
        _logger.error('enqueueing a conversion of %s failed', doc_id, exc_info=error)
        candidate = _conversion_error(error)
        if worst is None or isinstance(candidate, literature_backend.ConversionEnqueueFailedError):
            worst = candidate
    if worst is not None:
        raise worst


def _conversion_error(error: BaseException) -> Exception:
    """The port error a create failure is: transient only where repeating the create could place it."""
    if isinstance(error, _PERMANENT_SERVER_ERRORS):
        return literature_backend.ConversionEnqueueFailedError(str(error))
    if isinstance(error, _TRANSIENT_ENQUEUE_ERRORS):
        return literature_backend.ConversionUnavailableError(str(error))
    if isinstance(error, api_exceptions.GoogleAPIError):
        return literature_backend.ConversionEnqueueFailedError(str(error))
    # Not the Cloud Tasks API's error at all — a credential refresh, a bug. Permanent for the same
    # reason: nothing about repeating the create addresses it.
    return literature_backend.ConversionEnqueueFailedError(f'{type(error).__name__}: {error}')


def _parse[M: _message.Message](target: M, data: bytes) -> M:
    target.ParseFromString(data)
    return target


def _select_rendering(manifest: litcache_pb2.Manifest) -> tuple[str, litcache_pb2.Rendering] | None:
    """The canonical rendering ``(hash, rendering)``: highest-fidelity route, then newest, or None.

    Ranks by converter preference (litcache-manifest.md §5), breaking ties toward the most recent
    ``created_at`` (a re-render of a newer revision is newer) and finally the hash — a total,
    deterministic order, since protobuf-map iteration order is unspecified.
    """
    if not manifest.renderings:
        return None

    def _key(item: tuple[str, litcache_pb2.Rendering]) -> tuple[int, int, int, str]:
        rendering_hash, rendering = item
        rank = _CONVERTER_RANK.get(rendering.converter, _CONVERTER_RANK_MISS)
        created = rendering.created_at
        return (rank, -created.seconds, -created.nanos, rendering_hash)

    return min(manifest.renderings.items(), key=_key)


def _source_by_format(manifest: litcache_pb2.Manifest, fmt: litcache_pb2.SourceFormat) -> litcache_pb2.Source | None:
    # The newest lineage of this media type by its current revision's captured_at, never array order
    # (litcache-manifest.md); a revision-less lineage is skipped (nothing to name, and _current_revision
    # would raise on it).
    candidates = [s for s in manifest.sources if s.media_type == fmt and s.revisions]
    return max(candidates, key=lambda s: _current_revision(s).captured_at.ToDatetime(), default=None)


def _renders_from_xml(manifest: litcache_pb2.Manifest, rendering: litcache_pb2.Rendering) -> bool:
    source = next((s for s in manifest.sources if s.handle == rendering.from_source), None)
    return source is not None and source.media_type == litcache_pb2.SourceFormat.SOURCE_FORMAT_XML


def _current_revision(source: litcache_pb2.Source) -> litcache_pb2.Revision:
    """The latest revision of a lineage — recency is ``captured_at``, never array order (litcache-manifest.md)."""
    return max(source.revisions, key=lambda r: r.captured_at.ToDatetime())


def _media_type_for(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or 'application/octet-stream'


def _title(metadata: bytes | None, manifest: litcache_pb2.Manifest, doc_id: str) -> str:
    """The bibliographic title, falling back to an external id then the doc_id if unavailable."""
    if metadata is not None:
        article = _parse(pubmed_pb2.PubmedArticle(), metadata)
        title = article.medline_citation.article.article_title.value
        if title:
            return title
    external = manifest.external_ids
    return external.doi or external.pmid or doc_id
