"""Produce a paper's full-text rendering on demand: OA fetch, else PDF LLM-OCR.

The write-back side of the readiness plane (`docs/design/evidence-fulltext.md`). Given a known
`doc_id`, this turns a PENDING paper READY by producing a markdown rendering, preferring the cheap
path:

- **OA XML** off the litfetch ladder, converted with litdown (`add_source_and_rendering` — the fetched
  XML is a source the paper lacked). Seconds.
- **PDF LLM-OCR** when the ladder serves no usable XML but the manifest already carries a PDF source:
  the PDF is transcribed to markdown by the injected converter and committed
  with `add_rendering` (rendering-only — the PDF source is already in the manifest). Minutes; this is
  why the async worker exists.

When neither path yields text (no fetchable id, the ladder serves nothing, no PDF, or a conversion is
empty), it records the terminal `.fetch_outcome` NO_FULL_TEXT marker instead. When a conversion fails
*permanently* — an OCR refusal / truncation, or an OA body that is unparseable or of an unknown kind
(which first falls through to the PDF, often the better source) — it records a terminal FAILED marker
rather than raising, so the worker settles the paper (2xx) instead of retrying to the same dead end.
Transient fetch/convert errors still propagate, to be retried.

Standalone by construction: a module-level function, not a request handler. The Cloud Tasks worker
calls this; the read service only reports readiness. Keeping fetch/convert out of the request handler
is what keeps the read service's image lean (architecture B) and leaves the door open to inline the
fast OA path later without a wire change.

The fetcher, resolver, and PDF converter are injected so the path runs offline in tests — a fake
`fetch` returns a canned `OaSource` (or `None`) and a fake `convert_pdf` returns canned markdown, so
no network is touched.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.metadata
from collections.abc import Awaitable, Callable

import litdown
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from google.protobuf import timestamp_pb2
from litfetch import resolvers

from themis.litcache import identity, oa, ocr, outcome, writer
from themis.litcache.models import litcache_pb2

# The fetched XML lands as its own lineage; the litdown rendering is produced from it.
_XML_HANDLE = 'xml'
_LITDOWN_VERSION = importlib.metadata.version('litdown')

# ExternalIds' fields, each an id whose name is its `identity.ExternalId` scheme; read off the
# descriptor so an id field added to the proto reaches the fetch bundle without a change here.
# `oa.article_ids` keeps only the fetchable subset.
_ID_FIELDS = tuple(field.name for field in litcache_pb2.ExternalIds.DESCRIPTOR.fields)

Fetcher = Callable[..., Awaitable[oa.OaSource | None]]


class PaperNotInCorpusError(Exception):
    """The corpus holds no manifest for the doc_id.

    Distinguishes the one `NotFound` a retry cannot clear from every other missing object — a seed
    revision blob, a manifest that vanished mid-write — which are operational faults rather than a
    paper the corpus does not hold, and must stay retryable.
    """


class _PermanentSourceError(Exception):
    """An OA body could not be turned into text and a retry would not help.

    Raised for a served body of an unknown source kind / no access terms, or XML litdown cannot parse.
    Internal: the producer falls through to the PDF, then settles FAILED — it never escapes as a
    retryable error.
    """


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _external_ids(external_ids: litcache_pb2.ExternalIds) -> list[identity.ExternalId]:
    return [
        identity.ExternalId(scheme=field, value=getattr(external_ids, field))
        for field in _ID_FIELDS
        if external_ids.HasField(field)
    ]


def _record_no_full_text(bucket: gcs.Bucket, doc_id: str, at: datetime.datetime) -> outcome.Readiness:
    outcome.write_outcome(bucket, doc_id, outcome.FetchOutcome(kind=outcome.OutcomeKind.NO_FULL_TEXT, at=at))
    return outcome.Readiness.NO_FULL_TEXT


def _record_failed(bucket: gcs.Bucket, doc_id: str, at: datetime.datetime, *, error: str) -> outcome.Readiness:
    outcome.write_outcome(bucket, doc_id, outcome.FetchOutcome(kind=outcome.OutcomeKind.FAILED, at=at, error=error))
    return outcome.Readiness.FAILED


def _source_input(oa_source: oa.OaSource, captured_at: datetime.datetime) -> writer.SourceInput:
    return writer.SourceInput(
        handle=_XML_HANDLE,
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_XML,
        kind=oa_source.kind,
        data=oa_source.content,
        licence=oa_source.access.licence,
        licence_basis=oa_source.access.licence_basis,
        access=oa_source.access.access,
        captured_at=captured_at,
        origin_url=oa_source.origin_url,
    )


def _xml_rendering(oa_source: oa.OaSource, created_at: datetime.datetime) -> litcache_pb2.Rendering:
    created = timestamp_pb2.Timestamp()
    created.FromDatetime(created_at)
    return litcache_pb2.Rendering(
        from_source=_XML_HANDLE,
        from_revision=hashlib.sha256(oa_source.content).hexdigest(),
        converter=litcache_pb2.Converter.CONVERTER_LITDOWN,
        converter_version=_LITDOWN_VERSION,
        created_at=created,
    )


def _current_revision(source: litcache_pb2.Source) -> litcache_pb2.Revision:
    """The latest revision of a lineage — recency is `captured_at`, never array order."""
    if not source.revisions:
        raise ValueError(f'source lineage {source.handle!r} has no revisions')
    return max(source.revisions, key=lambda r: r.captured_at.ToDatetime())


def _pdf_source(manifest: litcache_pb2.Manifest) -> litcache_pb2.Source | None:
    """The PDF lineage to OCR when several exist (e.g. a seed PDF and a later publisher PDF).

    Picks the one whose newest revision is most recent, by `captured_at` — the same recency rule as
    `_current_revision`, never `manifest.sources` array order (which is ingestion order). Revision-less
    PDF lineages are ignored: there is nothing to download for them.
    """
    candidates = [
        s for s in manifest.sources if s.media_type == litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF and s.revisions
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: max(r.captured_at.ToDatetime() for r in s.revisions))


def _ocr_rendering(
    source: litcache_pb2.Source,
    revision: litcache_pb2.Revision,
    result: ocr.OcrRendering,
    created_at: datetime.datetime,
) -> litcache_pb2.Rendering:
    created = timestamp_pb2.Timestamp()
    created.FromDatetime(created_at)
    return litcache_pb2.Rendering(
        from_source=source.handle,
        from_revision=revision.hash,
        converter=litcache_pb2.Converter.CONVERTER_LLM_OCR,
        converter_version=result.converter_version,
        model=result.model,
        created_at=created,
    )


async def _produce_from_oa(
    bucket: gcs.Bucket,
    doc_id: str,
    manifest: litcache_pb2.Manifest,
    resolver: resolvers.Resolver | None,
    fetch: Fetcher,
    now: Callable[[], datetime.datetime],
) -> bool:
    """Fetch OA XML and commit an xml-faithful rendering; return whether one was committed."""
    article_ids = oa.article_ids(_external_ids(manifest.external_ids))
    if article_ids is None:
        return False
    try:
        oa_source = await fetch(article_ids, resolver=resolver if resolver is not None else oa.default_resolver())
    except oa.OaSourceError as e:
        # A real anomaly in the served body (unknown source kind / no access terms). Narrow to this
        # typed error, not a bare ValueError: litfetch's own dependencies raise ValueError (e.g. a
        # JSONDecodeError) on a transient upstream blip, which must propagate and retry, not be
        # condemned permanent.
        raise _PermanentSourceError(str(e)) from e
    if oa_source is None:
        return False
    try:
        markdown = litdown.convert(oa_source.content)
    except ValueError as e:
        # litdown raises ValueError on XML it cannot parse — permanent for this body.
        raise _PermanentSourceError(str(e)) from e
    if not markdown.strip():
        return False
    captured = now()
    writer.add_source_and_rendering(
        bucket,
        doc_id,
        _source_input(oa_source, captured),
        writer.RenderingInput(rendering=_xml_rendering(oa_source, captured), markdown=markdown),
    )
    return True


async def _produce_from_pdf(
    bucket: gcs.Bucket,
    doc_id: str,
    manifest: litcache_pb2.Manifest,
    convert_pdf: ocr.PdfConverter,
    now: Callable[[], datetime.datetime],
) -> bool:
    """OCR the manifest's PDF source to markdown and commit it; return whether one was committed."""
    source = _pdf_source(manifest)
    if source is None:
        return False
    revision = _current_revision(source)
    pdf_bytes = bucket.blob(
        writer.source_revision_path(doc_id, source.handle, revision.hash, source.media_type)
    ).download_as_bytes()
    result = await convert_pdf(pdf_bytes)
    if not result.markdown.strip():
        return False
    writer.add_rendering(
        bucket,
        doc_id,
        writer.RenderingInput(rendering=_ocr_rendering(source, revision, result, now()), markdown=result.markdown),
    )
    return True


async def produce_full_text(
    bucket: gcs.Bucket,
    doc_id: str,
    *,
    resolver: resolvers.Resolver | None = None,
    fetch: Fetcher = oa.fetch_oa_source,
    convert_pdf: ocr.PdfConverter,
    now: Callable[[], datetime.datetime] = _utcnow,
) -> outcome.Readiness:
    """Produce a paper's full text — OA XML if the ladder serves it, else the PDF via LLM-OCR.

    Reads the manifest (which must exist — this runs for a known paper). A rendering already present
    is a no-op READY. Otherwise it tries the OA ladder (build the litfetch id bundle from
    `manifest.external_ids`, fetch the XML body, litdown-convert, commit source + rendering); failing
    that, if the manifest carries a PDF source it OCRs the PDF to markdown and commits a rendering.
    When neither path yields text it records a NO_FULL_TEXT terminal marker.

    Args:
        bucket: The cache bucket holding the paper.
        doc_id: The paper to produce full text for.
        resolver: The litfetch id resolver `fetch` uses to fill missing ids; defaults to
            `oa.default_resolver` (doi/pmid → pmcid).
        fetch: The OA body fetcher; defaults to `oa.fetch_oa_source`. Injected so tests run offline.
        convert_pdf: The PDF → markdown LLM-OCR converter. Injected so
            tests run offline (the real one calls Anthropic).
        now: The clock for captured/created/marker timestamps. Injected for determinism.

    Returns:
        The resulting `Readiness`: READY when a rendering is present or was committed, NO_FULL_TEXT
        when neither the OA ladder nor a PDF source yielded any full text, FAILED when a conversion
        failed permanently and will not be retried — a permanent OA anomaly (unknown source kind / no
        access terms / unparseable XML, which first falls through to the PDF) or an OCR refusal /
        truncation.

    Raises:
        PaperNotInCorpusError: If the corpus holds no manifest for `doc_id` — settled, not retryable.
        google.api_core.exceptions.NotFound: If an object the manifest names is absent (a seed
            revision blob, say). Operational, so it propagates and stays retryable.
        Exception: A transient failure from the converter's provider (rate limit, overload,
            connection) or from the OA fetch propagates, so the worker returns 5xx and Cloud Tasks
            retries; permanent OA and OCR failures are caught and recorded FAILED, not raised.
        writer.ConcurrentWriteError: The manifest write lost its generation race the whole retry budget
            (contention — retryable).
    """
    try:
        manifest_bytes = bucket.blob(writer.manifest_path(doc_id)).download_as_bytes()
    except api_exceptions.NotFound as e:
        raise PaperNotInCorpusError(doc_id) from e
    manifest = litcache_pb2.Manifest.FromString(manifest_bytes)
    if manifest.renderings:
        return outcome.Readiness.READY
    # A terminal marker settles the paper: Cloud Tasks delivers at-least-once, so a redelivery must
    # not re-run the ladder (an OA fetch, or a full LLM-OCR) only to rewrite the same marker.
    marker = outcome.read_outcome(bucket, doc_id)
    if marker is not None:
        return outcome.terminal_readiness(marker)

    oa_failure: str | None = None
    try:
        if await _produce_from_oa(bucket, doc_id, manifest, resolver, fetch, now):
            return outcome.Readiness.READY
    except _PermanentSourceError as e:
        # A broken OA body is permanent, but the paper often has a PDF that OCRs fine — fall through to
        # it rather than escaping; only settle FAILED below if the PDF path yields nothing either.
        oa_failure = str(e)
    try:
        if await _produce_from_pdf(bucket, doc_id, manifest, convert_pdf, now):
            return outcome.Readiness.READY
    except ocr.OcrError as e:
        # A permanent OCR failure (refusal / truncation): settle FAILED so the worker returns 2xx and
        # the paper goes terminal, rather than raising → 500 → Cloud Tasks re-billing the model on every
        # retry before dead-lettering to a stuck PENDING. Transient API errors are not OcrError and
        # propagate here, so they still retry.
        return _record_failed(bucket, doc_id, now(), error=str(e))
    if oa_failure is not None:
        return _record_failed(bucket, doc_id, now(), error=oa_failure)
    return _record_no_full_text(bucket, doc_id, now())
