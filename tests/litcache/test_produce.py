"""Tests for the on-demand full-text producer (`themis.litcache.produce`).

Backed by a fake-gcs-server bucket (Docker-gated via the shared `gcs_bucket` fixture). The litfetch
fetch and the Anthropic PDF converter are injected so the path runs offline; litdown runs for real on
a minimal JATS body, so the OA success path exercises the actual XML→markdown conversion the
production path uses.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib

import litdown
import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from google.protobuf import timestamp_pb2
from litfetch import ids
from pubmed_proto import pubmed_pb2

from themis.litcache import oa, ocr, outcome, produce, writer
from themis.litcache.models import litcache_pb2

_DOC_ID = '9f3a0000-0000-4000-8000-000000000010'
_CAPTURED_AT = datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC)
_PDF_BYTES = b'%PDF-1.7 seed'
_PDF_HASH = hashlib.sha256(_PDF_BYTES).hexdigest()
# A JATS body litdown renders to non-blank markdown (the OA success path's real converter input).
_JATS = (
    b'<article><front><article-meta><title-group><article-title>T</article-title></title-group>'
    b'</article-meta></front><body><sec><title>Intro</title><p>Full text body.</p></sec></body></article>'
)


def _metadata() -> bytes:
    article = pubmed_pb2.PubmedArticle()
    article.medline_citation.pmid.value = '29089047'
    return article.SerializeToString()


def _pdf_source() -> writer.SourceInput:
    return writer.SourceInput(
        handle='pdf',
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
        kind=litcache_pb2.SourceKind.SOURCE_KIND_SEED,
        data=_PDF_BYTES,
        licence='',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT,
        access=litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess()),
        captured_at=_CAPTURED_AT,
    )


def _docling_rendering() -> writer.RenderingInput:
    created = timestamp_pb2.Timestamp()
    created.FromDatetime(_CAPTURED_AT)
    rendering = litcache_pb2.Rendering(
        from_source='pdf',
        from_revision=_PDF_HASH,
        converter=litcache_pb2.Converter.CONVERTER_DOCLING,
        converter_version='2.0.0',
        created_at=created,
    )
    return writer.RenderingInput(rendering=rendering, markdown='# From the seed pdf\n')


def _write_paper(
    bucket: gcs.Bucket,
    *,
    external_ids: litcache_pb2.ExternalIds,
    renderings: list[writer.RenderingInput] | None = None,
    sources: list[writer.SourceInput] | None = None,
) -> None:
    writer.write_paper(
        bucket,
        writer.PaperInput(
            doc_id=_DOC_ID,
            external_ids=external_ids,
            claim_key='doi:10.1/abc',
            equivalence=litcache_pb2.Equivalence(edges=[], canonical_doc_id=_DOC_ID),
            retraction=litcache_pb2.Retraction(),
            sources=[_pdf_source()] if sources is None else sources,
            renderings=renderings or [],
            metadata=_metadata(),
        ),
    )


def _oa_source(content: bytes = _JATS) -> oa.OaSource:
    return oa.OaSource(
        content=content,
        kind=litcache_pb2.SourceKind.SOURCE_KIND_EUROPE_PMC,
        access=oa.AccessTerms(
            licence='https://creativecommons.org/licenses/by/4.0/',
            licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT,
            access=litcache_pb2.Access(free_to_read=litcache_pb2.FreeToRead()),
        ),
        origin_url='https://europepmc.org/full-text.xml',
    )


def _returning(result: oa.OaSource | None, captured: dict[str, ids.ArticleIds] | None = None) -> produce.Fetcher:
    async def fetch(article_ids: ids.ArticleIds, **_kwargs: object) -> oa.OaSource | None:
        if captured is not None:
            captured['ids'] = article_ids
        return result

    return fetch


async def _boom(*_args: object, **_kwargs: object) -> oa.OaSource | None:
    raise AssertionError('fetch must not be called')


def _ocr_returning(
    markdown: str, *, model: str = 'claude-sonnet-5', captured: dict[str, bytes] | None = None
) -> produce.PdfConverter:
    async def convert(pdf_bytes: bytes) -> ocr.OcrRendering:
        if captured is not None:
            captured['pdf'] = pdf_bytes
        return ocr.OcrRendering(markdown=markdown, model=model)

    return convert


async def _ocr_boom(_pdf_bytes: bytes) -> ocr.OcrRendering:
    raise AssertionError('convert_pdf must not be called')


def _load_manifest(bucket: gcs.Bucket) -> litcache_pb2.Manifest:
    return litcache_pb2.Manifest.FromString(bucket.blob(writer.manifest_path(_DOC_ID)).download_as_bytes())


def _pdf_revision(data: bytes, captured_at: datetime.datetime) -> litcache_pb2.Revision:
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(captured_at)
    return litcache_pb2.Revision(
        hash=hashlib.sha256(data).hexdigest(), kind=litcache_pb2.SourceKind.SOURCE_KIND_SEED, captured_at=ts
    )


def _write_multi_revision_pdf_paper(bucket: gcs.Bucket, revisions: list[tuple[bytes, datetime.datetime]]) -> None:
    """Commit a manifest with one PDF source carrying several revisions, and their content blobs."""
    for data, _captured in revisions:
        bucket.blob(
            writer.source_revision_path(
                _DOC_ID, 'pdf', hashlib.sha256(data).hexdigest(), litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF
            )
        ).upload_from_string(data)
    source = litcache_pb2.Source(
        handle='pdf',
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
        licence='',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT,
        access=litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess()),
        revisions=[_pdf_revision(data, captured) for data, captured in revisions],
    )
    manifest = litcache_pb2.Manifest(
        doc_id=_DOC_ID,
        external_ids=litcache_pb2.ExternalIds(),
        claim_key='doi:10.1/abc',
        equivalence=litcache_pb2.Equivalence(edges=[], canonical_doc_id=_DOC_ID),
        retraction=litcache_pb2.Retraction(),
        sources=[source],
    )
    bucket.blob(writer.manifest_path(_DOC_ID)).upload_from_string(manifest.SerializeToString())


def test_produce_fetches_and_commits_oa_full_text(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc', pmid='29089047'))
    captured: dict[str, ids.ArticleIds] = {}
    readiness = asyncio.run(
        produce.produce_full_text(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(_oa_source(), captured),
            convert_pdf=_ocr_boom,  # the OA path wins; the PDF is never OCR'd
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.READY
    # The manifest ids reach the fetcher as a litfetch bundle.
    assert (captured['ids'].doi, captured['ids'].pmid) == ('10.1/abc', '29089047')
    manifest = _load_manifest(gcs_bucket)
    xml = next(s for s in manifest.sources if s.handle == 'xml')
    assert xml.media_type == litcache_pb2.SourceFormat.SOURCE_FORMAT_XML
    assert [r.hash for r in xml.revisions] == [hashlib.sha256(_JATS).hexdigest()]
    assert len(manifest.renderings) == 1
    rendering = next(iter(manifest.renderings.values()))
    assert rendering.converter == litcache_pb2.Converter.CONVERTER_LITDOWN
    assert outcome.read_readiness(gcs_bucket, _DOC_ID) is outcome.Readiness.READY


def test_produce_is_a_noop_when_a_rendering_exists(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), renderings=[_docling_rendering()])
    readiness = asyncio.run(produce.produce_full_text(gcs_bucket, _DOC_ID, fetch=_boom, convert_pdf=_ocr_boom))
    assert readiness is outcome.Readiness.READY
    # No fetch, no new lineage: the seed pdf is still the only source.
    assert {s.handle for s in _load_manifest(gcs_bucket).sources} == {'pdf'}


def test_produce_ocrs_the_pdf_when_the_ladder_serves_no_xml(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    captured: dict[str, bytes] = {}
    readiness = asyncio.run(
        produce.produce_full_text(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(None),
            convert_pdf=_ocr_returning('# OCR of the pdf\n', captured=captured),
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.READY
    # The seed pdf's bytes are what the converter received.
    assert captured['pdf'] == _PDF_BYTES
    manifest = _load_manifest(gcs_bucket)
    # No new source lineage — the OCR renders the pdf already in the manifest.
    assert {s.handle for s in manifest.sources} == {'pdf'}
    rendering = next(iter(manifest.renderings.values()))
    assert rendering.converter == litcache_pb2.Converter.CONVERTER_LLM_OCR
    assert rendering.model == 'claude-sonnet-5'
    assert (rendering.from_source, rendering.from_revision) == ('pdf', _PDF_HASH)


def test_produce_ocrs_the_pdf_when_no_fetchable_id(gcs_bucket: gcs.Bucket) -> None:
    # No fetchable id ⇒ the OA path never fetches, but a PDF source is still OCR'd.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds())
    readiness = asyncio.run(
        produce.produce_full_text(
            gcs_bucket, _DOC_ID, fetch=_boom, convert_pdf=_ocr_returning('# OCR\n'), now=lambda: _CAPTURED_AT
        )
    )
    assert readiness is outcome.Readiness.READY
    assert outcome.read_readiness(gcs_bucket, _DOC_ID) is outcome.Readiness.READY


def test_produce_ocrs_the_pdf_when_the_oa_conversion_is_empty(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A served-but-empty XML conversion falls through to the pdf, not straight to NO_FULL_TEXT.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    monkeypatch.setattr(litdown, 'convert', lambda _content: '   \n')
    readiness = asyncio.run(
        produce.produce_full_text(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(_oa_source()),
            convert_pdf=_ocr_returning('# OCR\n'),
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.READY
    rendering = next(iter(_load_manifest(gcs_bucket).renderings.values()))
    assert rendering.converter == litcache_pb2.Converter.CONVERTER_LLM_OCR


def test_produce_records_no_full_text_when_no_xml_and_no_pdf(gcs_bucket: gcs.Bucket) -> None:
    # Nothing fetchable and no pdf to OCR — the terminal marker, and neither path is called.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(), sources=[])
    readiness = asyncio.run(
        produce.produce_full_text(gcs_bucket, _DOC_ID, fetch=_boom, convert_pdf=_ocr_boom, now=lambda: _CAPTURED_AT)
    )
    assert readiness is outcome.Readiness.NO_FULL_TEXT
    marker = outcome.read_outcome(gcs_bucket, _DOC_ID)
    assert marker is not None
    assert marker.kind is outcome.OutcomeKind.NO_FULL_TEXT


def test_produce_records_no_full_text_on_an_empty_ocr(gcs_bucket: gcs.Bucket) -> None:
    # A blank transcription is not committed as a rendering; the paper is terminal NO_FULL_TEXT.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    readiness = asyncio.run(
        produce.produce_full_text(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(None),
            convert_pdf=_ocr_returning('   \n'),
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.NO_FULL_TEXT
    manifest = _load_manifest(gcs_bucket)
    assert not manifest.renderings
    assert {s.handle for s in manifest.sources} == {'pdf'}


def test_produce_ocrs_the_latest_pdf_revision_by_captured_at(gcs_bucket: gcs.Bucket) -> None:
    # The newest-captured revision sits in the array middle, so neither first- nor last-in-array is
    # the winner — only max-by-captured_at is. That revision's bytes are OCR'd and recorded.
    older = b'%PDF older'
    newest = b'%PDF newest'
    middling = b'%PDF middling'
    _write_multi_revision_pdf_paper(
        gcs_bucket,
        [
            (older, _CAPTURED_AT),
            (newest, _CAPTURED_AT + datetime.timedelta(days=2)),
            (middling, _CAPTURED_AT + datetime.timedelta(days=1)),
        ],
    )
    captured: dict[str, bytes] = {}
    readiness = asyncio.run(
        produce.produce_full_text(
            gcs_bucket,
            _DOC_ID,
            fetch=_boom,
            convert_pdf=_ocr_returning('# OCR\n', captured=captured),
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.READY
    assert captured['pdf'] == newest
    rendering = next(iter(_load_manifest(gcs_bucket).renderings.values()))
    assert rendering.from_revision == hashlib.sha256(newest).hexdigest()


def test_produce_records_failed_on_a_permanent_ocr_error(gcs_bucket: gcs.Bucket) -> None:
    # A permanent OCR failure settles FAILED (with the reason), not a raise: the worker returns 2xx and
    # the paper goes terminal, instead of retrying and re-billing the model to the same dead end.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    async def convert(_pdf_bytes: bytes) -> ocr.OcrRendering:
        raise ocr.OcrError('model refused to transcribe the PDF')

    readiness = asyncio.run(
        produce.produce_full_text(
            gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=convert, now=lambda: _CAPTURED_AT
        )
    )
    assert readiness is outcome.Readiness.FAILED
    marker = outcome.read_outcome(gcs_bucket, _DOC_ID)
    assert marker is not None
    assert marker.kind is outcome.OutcomeKind.FAILED
    assert 'refused' in marker.error  # the reason is preserved for diagnosis
    assert not _load_manifest(gcs_bucket).renderings
    assert outcome.read_readiness(gcs_bucket, _DOC_ID) is outcome.Readiness.FAILED


def test_produce_propagates_a_transient_convert_error_for_retry(gcs_bucket: gcs.Bucket) -> None:
    # Only a permanent OcrError settles FAILED; any other convert error propagates so the worker 5xxs
    # and Cloud Tasks retries, and no terminal marker is written (the paper stays PENDING for the retry).
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    async def convert(_pdf_bytes: bytes) -> ocr.OcrRendering:
        raise RuntimeError('transient upstream blip')

    with pytest.raises(RuntimeError, match='transient'):
        asyncio.run(
            produce.produce_full_text(
                gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=convert, now=lambda: _CAPTURED_AT
            )
        )
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None  # no terminal marker on a retryable error


def test_produce_falls_through_to_pdf_on_a_permanent_oa_error(gcs_bucket: gcs.Bucket) -> None:
    # A broken OA body (a ValueError from fetch/litdown) must not escape and strand a paper that has a
    # usable PDF; it falls through to the OCR branch and the paper is READY.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))  # carries a seed PDF

    async def fetch(_article_ids: object, **_kwargs: object) -> oa.OaSource:
        raise oa.OaSourceError('litfetch read no access terms from an XML body it served')

    readiness = asyncio.run(
        produce.produce_full_text(
            gcs_bucket, _DOC_ID, fetch=fetch, convert_pdf=_ocr_returning('# OCR\n'), now=lambda: _CAPTURED_AT
        )
    )
    assert readiness is outcome.Readiness.READY
    rendering = next(iter(_load_manifest(gcs_bucket).renderings.values()))
    assert rendering.converter == litcache_pb2.Converter.CONVERTER_LLM_OCR


def test_produce_records_failed_on_a_permanent_oa_error_without_a_pdf(gcs_bucket: gcs.Bucket) -> None:
    # A broken OA body and no PDF to fall back on → terminal FAILED (with the reason), not PENDING forever.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), sources=[])

    async def fetch(_article_ids: object, **_kwargs: object) -> oa.OaSource:
        raise oa.OaSourceError('served body of an unknown source kind')

    readiness = asyncio.run(
        produce.produce_full_text(gcs_bucket, _DOC_ID, fetch=fetch, convert_pdf=_ocr_boom, now=lambda: _CAPTURED_AT)
    )
    assert readiness is outcome.Readiness.FAILED
    marker = outcome.read_outcome(gcs_bucket, _DOC_ID)
    assert marker is not None
    assert marker.kind is outcome.OutcomeKind.FAILED
    assert 'unknown source kind' in marker.error


def test_produce_records_failed_on_an_unparseable_oa_body(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # litdown raising ValueError on a body it cannot parse is permanent for that body; with no PDF, FAILED.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), sources=[])

    def _unparseable(_content: bytes) -> str:
        raise ValueError('unrecognized root element')

    monkeypatch.setattr(litdown, 'convert', _unparseable)
    readiness = asyncio.run(
        produce.produce_full_text(
            gcs_bucket, _DOC_ID, fetch=_returning(_oa_source()), convert_pdf=_ocr_boom, now=lambda: _CAPTURED_AT
        )
    )
    assert readiness is outcome.Readiness.FAILED


def test_produce_propagates_a_transient_fetch_error_for_retry(gcs_bucket: gcs.Bucket) -> None:
    # A non-OaSourceError fetch error is transient, not a permanent anomaly: it propagates so Cloud Tasks
    # retries, and no terminal marker is written.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    async def fetch(_article_ids: object, **_kwargs: object) -> oa.OaSource:
        raise RuntimeError('europe pmc unreachable')

    with pytest.raises(RuntimeError, match='unreachable'):
        asyncio.run(
            produce.produce_full_text(gcs_bucket, _DOC_ID, fetch=fetch, convert_pdf=_ocr_boom, now=lambda: _CAPTURED_AT)
        )
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None


def test_produce_propagates_a_bare_valueerror_from_fetch_for_retry(gcs_bucket: gcs.Bucket) -> None:
    # The crux of the narrowed catch: a plain ValueError from fetch (a JSONDecodeError from litfetch on a
    # transient truncated/HTML body is one) is NOT the typed OaSourceError, so it propagates and retries
    # rather than being condemned to a terminal FAILED — no marker is written.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), sources=[])

    async def fetch(_article_ids: object, **_kwargs: object) -> oa.OaSource:
        raise ValueError('Expecting value: line 1 column 1 (char 0)')  # shape of a JSONDecodeError

    with pytest.raises(ValueError, match='Expecting value'):
        asyncio.run(
            produce.produce_full_text(gcs_bucket, _DOC_ID, fetch=fetch, convert_pdf=_ocr_boom, now=lambda: _CAPTURED_AT)
        )
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None


def test_produce_short_circuits_on_a_terminal_marker(gcs_bucket: gcs.Bucket) -> None:
    # Cloud Tasks delivers at-least-once; a redelivery for a paper already settled terminal must return
    # that state without re-running the ladder — neither fetch nor OCR is invoked (both would raise).
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    outcome.write_outcome(
        gcs_bucket, _DOC_ID, outcome.FetchOutcome(kind=outcome.OutcomeKind.FAILED, at=_CAPTURED_AT, error='refused')
    )
    readiness = asyncio.run(
        produce.produce_full_text(gcs_bucket, _DOC_ID, fetch=_boom, convert_pdf=_ocr_boom, now=lambda: _CAPTURED_AT)
    )
    assert readiness is outcome.Readiness.FAILED


def test_pdf_source_picks_the_lineage_with_the_newest_revision() -> None:
    # Two PDF lineages: the one whose newest revision is most recent wins, regardless of array order
    # (ingestion order), matching _current_revision's captured_at rule. A revision-less lineage is skipped.
    def _pdf_lineage(handle: str, captured: datetime.datetime) -> litcache_pb2.Source:
        ts = timestamp_pb2.Timestamp()
        ts.FromDatetime(captured)
        return litcache_pb2.Source(
            handle=handle,
            media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
            revisions=[litcache_pb2.Revision(hash=handle, captured_at=ts)],
        )

    older = _pdf_lineage('seed', _CAPTURED_AT)
    newer = _pdf_lineage('publisher', _CAPTURED_AT + datetime.timedelta(days=1))
    forward = produce._pdf_source(litcache_pb2.Manifest(sources=[older, newer]))
    reversed_order = produce._pdf_source(litcache_pb2.Manifest(sources=[newer, older]))
    assert forward is not None
    assert reversed_order is not None
    assert forward.handle == 'publisher'
    assert reversed_order.handle == 'publisher'
    empty = litcache_pb2.Source(handle='empty', media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF, revisions=[])
    assert produce._pdf_source(litcache_pb2.Manifest(sources=[empty])) is None


def test_produce_raises_on_a_missing_manifest(gcs_bucket: gcs.Bucket) -> None:
    with pytest.raises(api_exceptions.NotFound):
        asyncio.run(produce.produce_full_text(gcs_bucket, _DOC_ID, fetch=_boom, convert_pdf=_ocr_boom))
