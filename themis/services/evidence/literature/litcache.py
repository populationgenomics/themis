"""The litcache-reading full-text store (docs/design/document-pane.md §Backend seam).

Resolves a canonical ``doc_id`` against the real litcache GCS layout: reads ``papers/{doc_id}/
manifest.pb``, follows its ``markdown`` ref to the rendering readers are served, and names the
GCS object for a rendering, the current PDF revision, or an associated file — or serves that
rendering's text itself, up to a per-read budget. ``locate``/``validate`` run
anchorite's quote-to-offset location over the rendering bytes — manifest +
rendering only, never ``metadata.pb`` (that is read solely for ``describe_paper``'s title, decoded as
the ``PaperMetadata`` envelope and titled by whichever index's record states one; its absence falls
back rather than failing). ``validate`` is markdown-only and forgiving: a PDF-only
paper is reported unknown-not-checked, never a false "not located".

Readiness and provenance are derived from the layout, never stored: readiness by
``litcache.outcome``, provenance from the lineages holding the revision that rendering was made
from.

``ingest_papers`` is the one method that writes the bucket, committing a manifest for a paper the
corpus does not hold; otherwise the cache warms via the ingestion pipeline and the convert worker.
``request_conversions`` writes only the queue. Neither owns its mechanism — ``litcache.ingest``
resolves the id upstream, mints, and commits, and ``litcache.enqueue`` creates the conversion task —
so what this adapter adds is the crosswalk connection the ingest runs on and, to both, the decision
of which failures the servicer should offer a caller a retry for. An
associated file the manifest lists without a ``path`` is not yet fetched; resolving it raises
``MissingContentError`` rather than fetching-and-writing (deferred — the seeded papers have every
file fetched). PDF quote location (anchorite page regions) is not implemented; ``locate`` for a PDF raises
``PdfLocationUnavailableError`` rather than reporting a false ``not_located``.

Blocking GCS I/O is offloaded with ``asyncio.to_thread`` so it never stalls the ``grpc.aio`` loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import mimetypes
from collections.abc import Callable, Iterable, Mapping, Sequence

import anchorite
import httpx2
from google.api_core import exceptions as api_exceptions
from google.cloud import storage
from google.protobuf import message as _message

from themis.common import sql
from themis.litcache import (
    crosswalk,
    efetch,
    enqueue,
    identity,
    ingest,
    mirror,
    outcome,
    paper_metadata,
    records,
    resolve,
    writer,
)
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

# litcache associated-file role -> evidence FileRole (values align; mapped by name to stay explicit).
_FILE_ROLE = {
    litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_FIGURE: literature_pb2.FILE_ROLE_FIGURE,
    litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_SUPPLEMENTARY: literature_pb2.FILE_ROLE_SUPPLEMENTARY,
}


# Postgres's `insufficient_privilege`, which pg8000 reports in the mapping it carries as `args[0]`.
_INSUFFICIENT_PRIVILEGE = '42501'


def _denied_for_want_of_a_grant(error: Exception) -> bool:
    """Whether `error` is a store refusing the write because this deployment holds no grant for it.

    A fact about the deployment rather than an outage: the crosswalk `INSERT` comes from a migration
    and the bucket's write from the stack, and no retry provisions either. Matched on the
    SQLSTATE rather than the exception type, since pg8000 raises one `DatabaseError` for everything.
    """
    if isinstance(error, api_exceptions.Forbidden):
        return True
    detail = error.args[0] if error.args else None
    return isinstance(detail, Mapping) and detail.get('C') == _INSUFFICIENT_PRIVILEGE


# Faults of one upstream record that no retry clears: a record failing the store's id precondition,
# one its mirror does not fit, and one the writer refuses to keep. Each is typed where it is raised,
# so a bare `ValueError` — which a truncated JSON body raises on its way through the ladder — is left
# to the broad catch below and answered as the outage it is.
_UNINGESTABLE = (efetch.RecordPreconditionError, mirror.SchemaDriftError, ingest.UnstorableRecordError)


@dataclasses.dataclass(frozen=True)
class _Paper:
    """A paper's parsed litcache record: its manifest and the rendering its `markdown` ref names (if any)."""

    manifest: litcache_pb2.Manifest
    rendering: tuple[str, litcache_pb2.Record] | None  # (hash, record) — the one readers are served


class Store:
    """Serve the store half of the literature rpcs by reading the litcache directory for each ``doc_id``."""

    def __init__(
        self,
        bucket: storage.Bucket,
        http_client: httpx2.AsyncClient,
        *,
        connect: Callable[[], sql.Connection] | None = None,
        enqueuer: enqueue.Enqueuer | None = None,
        resolve_metadata: ingest.MetadataResolver = resolve.resolve_metadata,
    ) -> None:
        self._bucket = bucket
        self._http_client = http_client
        self._connect = connect
        self._enqueuer = enqueuer
        self._resolve_metadata = resolve_metadata

    async def describe_paper(self, doc_id: str) -> literature_pb2.PaperInfo:
        paper, metadata = await asyncio.to_thread(self._read_for_describe, doc_id)
        rendering = paper.rendering[1] if paper.rendering is not None else None
        has_markdown = rendering is not None
        has_pdf = _source_by_format(paper.manifest, litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF) is not None
        markdown_from_xml = rendering is not None and _renders_from_xml(paper.manifest, rendering)
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

    async def get_markdown(self, doc_id: str, max_chars: int) -> literature_pb2.GetMarkdownResponse:
        return await asyncio.to_thread(self._read_markdown, doc_id, max_chars)

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
        if offsets is None:
            return literature_pb2.ValidateResponse(ok=False, reason='quote not located in the markdown rendering')
        return literature_pb2.ValidateResponse(ok=True, located_in=[literature_pb2.REPRESENTATION_MARKDOWN])

    async def resolve_external_ids(self, external_ids: Sequence[str]) -> dict[str, str]:
        if self._connect is None:
            raise literature_backend.CrosswalkNotConfiguredError('no crosswalk is configured')
        if not external_ids:
            return {}  # no dial for an empty batch; `crosswalk.lookup` rejects one outright
        return await asyncio.to_thread(self._lookup, list(external_ids))

    async def ingest_papers(self, external_ids: Sequence[str]) -> dict[str, str]:
        if self._connect is None:
            raise literature_backend.IngestNotConfiguredError('no crosswalk is configured')
        # Serial, not gathered: each id costs one upstream metadata call, and PubMed and Crossref both
        # pace an unkeyed caller, so a fan-out buys latency back as 429s. The servicer's per-call bound
        # on how many papers one request may ingest is what keeps the serial cost answerable.
        # Folded on the crosswalk's own key, not byte-exactly, so two spellings of one DOI cost one
        # upstream call — and every spelling the caller sent is answered with the paper that call
        # found, as `crosswalk.lookup` answers each of them on the read path.
        candidates: dict[str, list[str]] = {}
        for external_id in external_ids:
            scheme, _, _value = external_id.partition(':')
            if scheme in literature_backend.INGESTABLE_SCHEMES:
                candidates.setdefault(crosswalk.normalise_key(external_id), []).append(external_id)
        ingested: dict[str, str] = {}
        for spellings in candidates.values():
            scheme, _, value = spellings[0].partition(':')
            result = await self._ingest_one(identity.ExternalId(scheme=scheme, value=value))
            if result is not None:
                ingested.update(dict.fromkeys(spellings, result.doc_id))
        return ingested

    async def _ingest_one(self, external_id: identity.ExternalId) -> ingest.IngestResult | None:
        try:
            return await ingest.ingest_unknown_paper(
                self._bucket,
                self._lookup,
                self._mint,
                external_id,
                http_client=self._http_client,
                resolve_metadata=self._resolve_metadata,
            )
        except literature_backend.IngestNotConfiguredError:
            raise  # permanent, and already the port's own type — not an outage to retry
        except literature_backend.CrosswalkNotConfiguredError as e:
            # The same permanent condition under the read path's name; retried as an outage it would
            # spend the caller's whole budget on a deployment nothing about retrying provisions.
            raise literature_backend.IngestNotConfiguredError(str(e)) from e
        except _UNINGESTABLE as e:
            # An upstream holds a record this store cannot keep. Terminal for the id, so it is charged
            # to that id alone rather than failing the batch around it — the whole call would otherwise
            # fail on every retry, and the ids beside it would never be resolved either.
            _logger.warning('%s names a record this store cannot hold; not ingested: %s', external_id.key, e)
            return None
        except Exception as e:
            # Deliberately broad, for the reason `_lookup` is: the upstream ladder, the Cloud SQL
            # dial, and the manifest commit raise from unrelated hierarchies (httpx2, pg8000, the
            # connector, api-core), and reading any of them as "no such paper" would write a real
            # paper off permanently. A clean miss is not an exception — it is a `None` return.
            if _denied_for_want_of_a_grant(e):
                # The refusal names the server's source file and routine, or the upload URL and the
                # service account, so it goes to the log; the caller gets the condition alone.
                _logger.exception('the ingest write was refused for want of a grant')
                raise literature_backend.IngestNotConfiguredError(
                    'this deployment holds no grant for the write the ingest makes'
                ) from e
            raise literature_backend.IngestUnavailableError(str(e)) from e

    async def full_text_readiness(self, doc_ids: Sequence[str]) -> dict[str, literature_pb2.FullTextState]:
        # One to two GCS reads per distinct id: READY derives from the manifest alone, but anything
        # else probes the sidecar too. Gathered so a batch runs concurrently, not serially.
        distinct = list(dict.fromkeys(doc_ids))
        states = await asyncio.gather(*(asyncio.to_thread(self._readiness, doc_id) for doc_id in distinct))
        return dict(zip(distinct, states, strict=True))

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

    def _lookup(self, external_ids: Iterable[str]) -> dict[str, str]:
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
            # Admin API, DBAPI), and mistaking any of them for "this paper is not in the store" is
            # the failure this method exists to prevent.
            raise literature_backend.CrosswalkUnavailableError(str(e)) from e

    def _mint(self, external_ids: Iterable[str]) -> crosswalk.MintResult:
        """One crosswalk mint, on a connection opened and closed per call, as `_lookup` dials.

        Blocking; `ingest.ingest_unknown_paper` calls it inside its own `asyncio.to_thread`.

        Raises:
            IngestNotConfiguredError: no crosswalk is wired (permanent).
            Exception: whatever the dial or the transaction raises — several unrelated hierarchies, all
                classified by `_ingest_one`'s broad catch rather than here.
        """
        if self._connect is None:
            raise literature_backend.IngestNotConfiguredError('no crosswalk is configured')
        with contextlib.closing(self._connect()) as conn:
            return crosswalk.mint(conn, external_ids)

    def _readiness(self, doc_id: str) -> literature_pb2.FullTextState:
        return _READINESS_STATE[outcome.read_readiness(self._bucket, doc_id)]

    def _read_manifest(self, doc_id: str) -> _Paper:
        """The paper's manifest + the rendering its ``markdown`` ref names, or ``UnknownPaperError`` if absent."""
        manifest_bytes = self._download(writer.manifest_path(doc_id))
        if manifest_bytes is None:
            raise literature_backend.UnknownPaperError(doc_id)
        manifest = _parse(litcache_pb2.Manifest(), manifest_bytes)
        return _Paper(manifest=manifest, rendering=records.markdown(manifest))

    def _read_for_describe(self, doc_id: str) -> tuple[_Paper, bytes | None]:
        """The manifest and the ``metadata.pb`` bytes (absent ⇒ the title falls back)."""
        return self._read_manifest(doc_id), self._download(writer.metadata_path(doc_id))

    def _read_markdown(self, doc_id: str, max_chars: int) -> literature_pb2.GetMarkdownResponse:
        paper = self._read_manifest(doc_id)
        if paper.rendering is None:
            # The manifest is already in hand, so the readiness derivation takes it rather than
            # downloading it a second time through `outcome.read_readiness`.
            readiness = outcome.readiness_from_manifest(self._bucket, doc_id, paper.manifest)
            return literature_pb2.GetMarkdownResponse(
                unavailable=literature_pb2.FullTextUnavailable(state=_READINESS_STATE[readiness])
            )
        rendering_hash, rendering = paper.rendering
        markdown, total_chars = literature_backend.capped_markdown(
            self._read_rendering(doc_id, rendering_hash), max_chars
        )
        return literature_pb2.GetMarkdownResponse(
            content=literature_pb2.PaperMarkdown(
                doc_id=doc_id,
                markdown=markdown,
                provenance=_provenance(paper.manifest, rendering),
                total_chars=total_chars,
            )
        )

    def _read_rendering(self, doc_id: str, rendering_hash: str) -> str:
        markdown = self._download(f'{writer.paper_dir(doc_id)}/renderings/{rendering_hash}.md')
        if markdown is None:
            raise literature_backend.MissingRenderingBlobError(f'{doc_id} rendering {rendering_hash} is missing')
        return markdown.decode('utf-8')

    def _locate_markdown(self, doc_id: str, quote: str) -> tuple[int, int] | None:
        paper = self._read_manifest(doc_id)
        if paper.rendering is None:
            raise literature_backend.RepresentationUnavailableError(f'{doc_id} has no markdown rendering')
        rendering_hash, _ = paper.rendering
        return anchorite.locate_quote_span(self._read_rendering(doc_id, rendering_hash), quote)

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


def _lineage_provenance(source: litcache_pb2.Source, revision: litcache_pb2.Revision) -> literature_pb2.TextProvenance:
    if source.access.WhichOneof('kind') == 'institution_captured':
        return literature_pb2.TEXT_PROVENANCE_SUPPLIED
    if revision.kind == litcache_pb2.SourceKind.SOURCE_KIND_UPLOAD:
        return literature_pb2.TEXT_PROVENANCE_SUPPLIED
    if source.access.WhichOneof('kind') == 'free_to_read':
        return literature_pb2.TEXT_PROVENANCE_OPEN_ACCESS
    return literature_pb2.TEXT_PROVENANCE_UNSPECIFIED


def _provenance(
    manifest: litcache_pb2.Manifest, rendering: litcache_pb2.Record | None
) -> literature_pb2.TextProvenance:
    """How the rendering's text entered the store, from the revision it was produced from.

    A lineage a human captured under institutional access, or a revision uploaded rather than
    fetched, is SUPPLIED; a free-to-read lineage the OA ladder reached is OPEN_ACCESS. Everything
    else is UNSPECIFIED: no rendering, a rendering whose revision no lineage carries, and the
    ``licensed``/``unknown`` dispositions — text that reached the store neither by a human nor
    demonstrably openly, which neither answer would state honestly.

    The same bytes can sit in more than one lineage, and then the least restrictive terms apply to
    them (litcache-manifest.md): OPEN_ACCESS if any lineage holding them is free to read, else
    SUPPLIED if any reached the store through a human, else UNSPECIFIED.
    """
    if rendering is None:
        return literature_pb2.TEXT_PROVENANCE_UNSPECIFIED
    found = {
        _lineage_provenance(source, revision) for source, revision in records.source_revisions(manifest, rendering)
    }
    for answer in (literature_pb2.TEXT_PROVENANCE_OPEN_ACCESS, literature_pb2.TEXT_PROVENANCE_SUPPLIED):
        if answer in found:
            return answer
    return literature_pb2.TEXT_PROVENANCE_UNSPECIFIED


def _source_by_format(manifest: litcache_pb2.Manifest, fmt: litcache_pb2.SourceFormat) -> litcache_pb2.Source | None:
    # The newest lineage of this media type by its current revision's captured_at, never array order
    # (litcache-manifest.md); a revision-less lineage is skipped (nothing to name, and _current_revision
    # would raise on it).
    candidates = [s for s in manifest.sources if s.media_type == fmt and s.revisions]
    return max(candidates, key=lambda s: _current_revision(s).captured_at.ToDatetime(), default=None)


def _renders_from_xml(manifest: litcache_pb2.Manifest, rendering: litcache_pb2.Record) -> bool:
    return any(
        source.media_type == litcache_pb2.SourceFormat.SOURCE_FORMAT_XML
        for source, _ in records.source_revisions(manifest, rendering)
    )


def _current_revision(source: litcache_pb2.Source) -> litcache_pb2.Revision:
    """The latest revision of a lineage — recency is ``captured_at``, never array order (litcache-manifest.md)."""
    return max(source.revisions, key=lambda r: r.captured_at.ToDatetime())


def _media_type_for(name: str) -> str:
    guessed, _ = mimetypes.guess_type(name)
    return guessed or 'application/octet-stream'


def _title(metadata: bytes | None, manifest: litcache_pb2.Manifest, doc_id: str) -> str:
    """The bibliographic title, falling back to an external id then the doc_id if unavailable.

    Raises:
        literature_backend.CorruptMetadataError: If ``metadata`` does not read as an envelope
            meeting its constraints.
    """
    if metadata is not None:
        try:
            title = paper_metadata.title(paper_metadata.parse(metadata))
        except ValueError as e:
            raise literature_backend.CorruptMetadataError(f'{doc_id}: {e}') from e
        if title:
            return title
    external = manifest.external_ids
    return external.doi or external.pmid or doc_id
