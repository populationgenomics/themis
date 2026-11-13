"""Tests for the full-text worker's loop (`themis.litcache.produce`): its steps, their reach, and the marker.

Backed by a fake-gcs-server bucket (Docker-gated via the shared `gcs_bucket` fixture). The litfetch
fetch and the Anthropic PDF converter are injected so the path runs offline; litdown runs for real on
a minimal JATS body, so the OA success path exercises the actual XML→markdown conversion the
production path uses.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import datetime
import hashlib
import pathlib
import uuid
from collections.abc import Callable, Coroutine, Sequence

import litdown
import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from google.protobuf import timestamp_pb2
from grus.models import pedigree_pb2
from litfetch import ids

from themis.litcache import (
    figure_annotate,
    joblog,
    metadata_fields,
    oa,
    ocr,
    outcome,
    pdf,
    pedigree_extract,
    produce,
    variant_extract,
    work,
    writer,
)
from themis.litcache.models import figures_pb2, litcache_pb2, pedigrees_pb2, variants_pb2, work_pb2
from themis.testing import litcache_pedigrees, litcache_variants

_DOC_ID = '9f3a0000-0000-4000-8000-000000000010'
_CAPTURED_AT = datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC)
_PDF_BYTES = b'%PDF-1.7 seed'
# A loadable PDF, for the located path: it probes the PDF's text layer before committing it.
_LOCATED_PDF = (
    pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache' / 'nonoa' / 'source.pdf'
).read_bytes()
_PDF_HASH = hashlib.sha256(_PDF_BYTES).hexdigest()
# A JATS body litdown renders to non-blank markdown (the OA success path's real converter input).
_JATS = (
    b'<article><front><article-meta><title-group><article-title>T</article-title></title-group>'
    b'</article-meta></front><body><sec><title>Intro</title><p>Full text body.</p></sec></body></article>'
)


_ATTEMPT = joblog.Attempt(
    id='00000000-0000-4000-8000-000000000001', queue='themis-work-interactive', task='9f3a--main_text', retry_count=0
)
_OTHER = joblog.Attempt(
    id='00000000-0000-4000-8000-000000000002', queue='themis-work-bulk', task='9f3a--gen-1', retry_count=0
)


def _metadata() -> bytes:
    source = litcache_pb2.MetadataSource()
    source.pubmed.article.medline_citation.pmid.value = '29089047'
    metadata = litcache_pb2.SourcedMetadata(metadata=metadata_fields.fill(source), source=source)
    return metadata.SerializeToString()


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
    return writer.RenderingInput(
        construction=litcache_pb2.RenderingConstruction(
            converter=litcache_pb2.Converter.CONVERTER_DOCLING, converter_version='2.0.0'
        ),
        from_revision=_PDF_HASH,
        created_at=_CAPTURED_AT,
        markdown='# From the seed pdf\n',
    )


def _served(manifest: litcache_pb2.Manifest) -> litcache_pb2.Record:
    """The rendering record the manifest's markdown ref names: the one a reader is served."""
    return manifest.records[manifest.refs['markdown']]


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


def _located_pdf() -> oa.OaPdf:
    return oa.OaPdf(
        content=_LOCATED_PDF,
        access=oa.AccessTerms(
            licence='cc-by',
            licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED,
            access=litcache_pb2.Access(free_to_read=litcache_pb2.FreeToRead()),
        ),
        origin_url='https://example.org/paper.pdf',
    )


def _pdf_returning(result: oa.OaPdf | None) -> produce.PdfFetcher:
    async def fetch_pdf(_article_ids: ids.ArticleIds) -> oa.OaPdf | None:
        return result

    return fetch_pdf


async def _pdf_boom(_article_ids: ids.ArticleIds) -> oa.OaPdf | None:
    raise AssertionError('fetch_pdf must not be called')


def _ocr_returning(
    markdown: str,
    *,
    model: str = 'claude-sonnet-5',
    converter_version: str = '1.1',
    captured: dict[str, bytes] | None = None,
) -> ocr.PdfConverter:
    async def convert(pdf_bytes: bytes, *, timeout: float | None) -> ocr.OcrRendering:
        del timeout
        if captured is not None:
            captured['pdf'] = pdf_bytes
        return ocr.OcrRendering(markdown=markdown, model=model, converter_version=converter_version)

    return convert


async def _ocr_boom(_pdf_bytes: bytes, *, timeout: float | None) -> ocr.OcrRendering:
    del timeout
    raise AssertionError('convert_pdf must not be called')


def _supplements_returning(files: list[oa.SupplementaryFile]) -> produce.SupplementFetcher:
    async def fetch_supplementary(_article_ids: ids.ArticleIds, **_kwargs: object) -> list[oa.SupplementaryFile]:
        return files

    return fetch_supplementary


async def _no_supplements(_article_ids: ids.ArticleIds, **_kwargs: object) -> list[oa.SupplementaryFile]:
    return []


async def _supplements_boom(*_args: object, **_kwargs: object) -> list[oa.SupplementaryFile]:
    raise AssertionError('fetch_supplementary must not be called')


def _produce(
    bucket: gcs.Bucket,
    doc_id: str,
    *,
    targets: tuple[str, ...] = (work.MAIN_TEXT_TARGET,),
    attempt: joblog.Attempt = _ATTEMPT,
    fetch: produce.Fetcher = _boom,
    fetch_pdf: produce.PdfFetcher = _pdf_boom,
    fetch_supplementary: produce.SupplementFetcher = _no_supplements,
    convert_pdf: ocr.PdfConverter = _ocr_boom,
    variants: produce.Variants = litcache_variants.UNUSED,
    figures: produce.Figures = litcache_pedigrees.UNUSED_FIGURES,
    pedigrees: produce.Pedigrees = litcache_pedigrees.UNUSED,
    now: Callable[[], datetime.datetime] = lambda: _CAPTURED_AT,
) -> Coroutine[object, object, outcome.Readiness]:
    """`produce.run` for this attempt, asking for the main text, with every collaborator refusing to be called."""
    return produce.run(
        bucket,
        doc_id,
        targets=targets,
        attempt=attempt,
        fetch=fetch,
        fetch_pdf=fetch_pdf,
        fetch_supplementary=fetch_supplementary,
        convert_pdf=convert_pdf,
        variants=variants,
        figures=figures,
        pedigrees=pedigrees,
        now=now,
    )


def _log(bucket: gcs.Bucket) -> list[tuple[str, int, int]]:
    """The paper's log as (step, event, outcome) triples."""
    return [(e.step, e.event, e.outcome) for e in joblog.read(bucket, _DOC_ID).entries]


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
        _produce(
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
    assert len(manifest.records) == 1
    rendering = _served(manifest)
    assert rendering.rendering.converter == litcache_pb2.Converter.CONVERTER_LITDOWN
    assert outcome.read_readiness(gcs_bucket, _DOC_ID) is outcome.Readiness.READY


def test_produce_hands_the_fetcher_a_manifests_bookshelf_accession(gcs_bucket: gcs.Bucket) -> None:
    # A chapter's manifest carries its accession; the fetch bundle carries it too, which is what
    # lets the Bookshelf rung fire for a paper with no PMCID.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(pmid='30000010', bookid='NBK900001'))
    captured: dict[str, ids.ArticleIds] = {}
    readiness = asyncio.run(
        _produce(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(_oa_source(), captured),
            convert_pdf=_ocr_boom,
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.READY
    assert captured['ids'] == ids.ArticleIds(pmid='30000010', bookid='NBK900001')


def test_a_paper_with_text_runs_its_free_steps_and_transcribes_nothing(gcs_bucket: gcs.Bucket) -> None:
    # The Docling text meets the main text, so the transcription is not reached; the XML step is free, and runs.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), renderings=[_docling_rendering()])
    readiness = asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None)))
    assert readiness is outcome.Readiness.READY
    assert {s.handle for s in _load_manifest(gcs_bucket).sources} == {'pdf'}
    assert _log(gcs_bucket) == [('xml', work_pb2.JOB_EVENT_SETTLED, work_pb2.SETTLED_OUTCOME_NOTHING)]


def test_produce_ocrs_the_pdf_when_the_ladder_serves_no_xml(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    captured: dict[str, bytes] = {}
    readiness = asyncio.run(
        _produce(
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
    rendering = _served(manifest)
    assert rendering.rendering.converter == litcache_pb2.Converter.CONVERTER_LLM_OCR
    assert rendering.rendering.model == 'claude-sonnet-5'
    assert list(rendering.deps) == [_PDF_HASH]


def test_produce_ocrs_the_pdf_when_no_fetchable_id(gcs_bucket: gcs.Bucket) -> None:
    # No fetchable id ⇒ the OA path never fetches, but a PDF source is still OCR'd.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds())
    readiness = asyncio.run(
        _produce(gcs_bucket, _DOC_ID, fetch=_boom, convert_pdf=_ocr_returning('# OCR\n'), now=lambda: _CAPTURED_AT)
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
        _produce(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(_oa_source()),
            convert_pdf=_ocr_returning('# OCR\n'),
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.READY
    rendering = _served(_load_manifest(gcs_bucket))
    assert rendering.rendering.converter == litcache_pb2.Converter.CONVERTER_LLM_OCR


def test_produce_records_no_full_text_when_no_xml_and_no_pdf(gcs_bucket: gcs.Bucket) -> None:
    # Nothing fetchable and no pdf to OCR — the terminal marker, and neither path is called.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(), sources=[])
    readiness = asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_boom, convert_pdf=_ocr_boom, now=lambda: _CAPTURED_AT))
    assert readiness is outcome.Readiness.NO_FULL_TEXT
    marker = outcome.read_outcome(gcs_bucket, _DOC_ID)
    assert marker is not None
    assert marker.kind is outcome.OutcomeKind.NO_FULL_TEXT


def test_produce_commits_a_located_pdf_and_its_ocr_when_the_paper_holds_no_pdf(gcs_bucket: gcs.Bucket) -> None:
    # An id-only paper: no XML on the ladder and no PDF of its own, so an open-access PDF is located,
    # transcribed, and committed as the paper's pdf lineage together with its rendering.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), sources=[])
    captured: dict[str, bytes] = {}
    readiness = asyncio.run(
        _produce(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(None),
            fetch_pdf=_pdf_returning(_located_pdf()),
            convert_pdf=_ocr_returning('# OCR of the located pdf\n', captured=captured),
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.READY
    assert captured['pdf'] == _LOCATED_PDF
    manifest = _load_manifest(gcs_bucket)
    (source,) = manifest.sources
    assert source.media_type == litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF
    assert source.licence_basis == litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED
    (revision,) = source.revisions
    located_hash = hashlib.sha256(_LOCATED_PDF).hexdigest()
    assert revision.hash == located_hash
    assert revision.kind == litcache_pb2.SourceKind.SOURCE_KIND_UNPAYWALL
    assert revision.origin_url == 'https://example.org/paper.pdf'
    assert revision.HasField('has_text_layer')
    assert (
        gcs_bucket.blob(
            writer.source_revision_path(_DOC_ID, source.handle, located_hash, source.media_type)
        ).download_as_bytes()
        == _LOCATED_PDF
    )
    (rendering,) = manifest.records.values()
    assert rendering.rendering.converter == litcache_pb2.Converter.CONVERTER_LLM_OCR
    assert list(rendering.deps) == [located_hash]
    assert manifest.refs['markdown'] in manifest.records


def test_produce_prefers_the_held_pdf_to_locating_one(gcs_bucket: gcs.Bucket) -> None:
    # A paper that holds a PDF transcribes that one; nothing is located.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    readiness = asyncio.run(
        _produce(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(None),
            fetch_pdf=_pdf_boom,
            convert_pdf=_ocr_returning('# OCR\n'),
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.READY


def test_produce_records_no_full_text_when_no_pdf_is_located(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), sources=[])
    readiness = asyncio.run(
        _produce(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(None),
            fetch_pdf=_pdf_returning(None),
            convert_pdf=_ocr_boom,
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.NO_FULL_TEXT
    assert not _load_manifest(gcs_bucket).sources


def test_produce_records_failed_when_the_located_pdf_will_not_transcribe(gcs_bucket: gcs.Bucket) -> None:
    # Locating commits the PDF; its transcription is a step of its own, which settles FAILED.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), sources=[])

    async def refuse(_pdf_bytes: bytes, *, timeout: float | None) -> ocr.OcrRendering:
        del timeout
        raise ocr.OcrError('refused')

    readiness = asyncio.run(
        _produce(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(None),
            fetch_pdf=_pdf_returning(_located_pdf()),
            convert_pdf=refuse,
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.FAILED
    assert [s.handle for s in _load_manifest(gcs_bucket).sources] == ['pdf']


def test_produce_records_failed_on_an_oversized_located_pdf(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), sources=[])

    async def fetch_pdf(_article_ids: ids.ArticleIds) -> oa.OaPdf | None:
        raise oa.OaSourceError('serves more than 104857600 bytes')

    readiness = asyncio.run(
        _produce(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(None),
            fetch_pdf=fetch_pdf,
            convert_pdf=_ocr_boom,
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.FAILED


def test_produce_propagates_a_transient_locate_failure_for_retry(gcs_bucket: gcs.Bucket) -> None:
    # Unpaywall or the PDF host still failing after retries is not "no full text": no marker is written.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), sources=[])

    async def fetch_pdf(_article_ids: ids.ArticleIds) -> oa.OaPdf | None:
        raise oa.UpstreamUnavailableError('Unpaywall answered 503')

    with pytest.raises(oa.UpstreamUnavailableError):
        asyncio.run(
            _produce(
                gcs_bucket,
                _DOC_ID,
                fetch=_returning(None),
                fetch_pdf=fetch_pdf,
                convert_pdf=_ocr_boom,
                now=lambda: _CAPTURED_AT,
            )
        )
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None


def test_produce_records_no_full_text_on_an_empty_ocr(gcs_bucket: gcs.Bucket) -> None:
    # A blank transcription is not committed as a rendering; the paper is terminal NO_FULL_TEXT.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    readiness = asyncio.run(
        _produce(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(None),
            convert_pdf=_ocr_returning('   \n'),
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.NO_FULL_TEXT
    manifest = _load_manifest(gcs_bucket)
    assert not manifest.records
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
        _produce(
            gcs_bucket,
            _DOC_ID,
            fetch=_boom,
            convert_pdf=_ocr_returning('# OCR\n', captured=captured),
            now=lambda: _CAPTURED_AT,
        )
    )
    assert readiness is outcome.Readiness.READY
    assert captured['pdf'] == newest
    rendering = _served(_load_manifest(gcs_bucket))
    assert list(rendering.deps) == [hashlib.sha256(newest).hexdigest()]


def test_produce_records_failed_on_a_permanent_ocr_error(gcs_bucket: gcs.Bucket) -> None:
    # A permanent OCR failure settles FAILED (with the reason), not a raise: the worker returns 2xx and
    # the paper goes terminal, instead of retrying and re-billing the model to the same dead end.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    async def convert(_pdf_bytes: bytes, *, timeout: float | None) -> ocr.OcrRendering:
        del timeout
        raise ocr.OcrError('model refused to transcribe the PDF')

    readiness = asyncio.run(
        _produce(gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=convert, now=lambda: _CAPTURED_AT)
    )
    assert readiness is outcome.Readiness.FAILED
    marker = outcome.read_outcome(gcs_bucket, _DOC_ID)
    assert marker is not None
    assert marker.kind is outcome.OutcomeKind.FAILED
    assert 'refused' in marker.error  # the reason is preserved for diagnosis
    assert not _load_manifest(gcs_bucket).records
    assert outcome.read_readiness(gcs_bucket, _DOC_ID) is outcome.Readiness.FAILED


def test_produce_propagates_a_transient_convert_error_for_retry(gcs_bucket: gcs.Bucket) -> None:
    # Only a permanent OcrError settles FAILED; any other convert error propagates so the worker 5xxs
    # and Cloud Tasks retries, and no terminal marker is written (the paper stays PENDING for the retry).
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    async def convert(_pdf_bytes: bytes, *, timeout: float | None) -> ocr.OcrRendering:
        del timeout
        raise RuntimeError('transient upstream blip')

    with pytest.raises(RuntimeError, match='transient'):
        asyncio.run(
            _produce(gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=convert, now=lambda: _CAPTURED_AT)
        )
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None  # no terminal marker on a retryable error


def test_produce_falls_through_to_pdf_on_a_permanent_oa_error(gcs_bucket: gcs.Bucket) -> None:
    # A broken OA body (a ValueError from fetch/litdown) must not escape and strand a paper that has a
    # usable PDF; it falls through to the OCR branch and the paper is READY.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))  # carries a seed PDF

    async def fetch(_article_ids: object, **_kwargs: object) -> oa.OaSource:
        raise oa.OaSourceError('litfetch read no access terms from an XML body it served')

    readiness = asyncio.run(
        _produce(gcs_bucket, _DOC_ID, fetch=fetch, convert_pdf=_ocr_returning('# OCR\n'), now=lambda: _CAPTURED_AT)
    )
    assert readiness is outcome.Readiness.READY
    rendering = _served(_load_manifest(gcs_bucket))
    assert rendering.rendering.converter == litcache_pb2.Converter.CONVERTER_LLM_OCR


def test_produce_records_failed_on_a_permanent_oa_error_without_a_pdf(gcs_bucket: gcs.Bucket) -> None:
    # A broken OA body and no PDF to fall back on → terminal FAILED (with the reason), not PENDING forever.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), sources=[])

    async def fetch(_article_ids: object, **_kwargs: object) -> oa.OaSource:
        raise oa.OaSourceError('served body of an unknown source kind')

    readiness = asyncio.run(
        _produce(
            gcs_bucket,
            _DOC_ID,
            fetch=fetch,
            fetch_pdf=_pdf_returning(None),
            convert_pdf=_ocr_boom,
            now=lambda: _CAPTURED_AT,
        )
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
        _produce(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(_oa_source()),
            fetch_pdf=_pdf_returning(None),
            convert_pdf=_ocr_boom,
            now=lambda: _CAPTURED_AT,
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
        asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=fetch, convert_pdf=_ocr_boom, now=lambda: _CAPTURED_AT))
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None


def test_produce_propagates_a_bare_valueerror_from_fetch_for_retry(gcs_bucket: gcs.Bucket) -> None:
    # The crux of the narrowed catch: a plain ValueError from fetch (a JSONDecodeError from litfetch on a
    # transient truncated/HTML body is one) is NOT the typed OaSourceError, so it propagates and retries
    # rather than being condemned to a terminal FAILED — no marker is written.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), sources=[])

    async def fetch(_article_ids: object, **_kwargs: object) -> oa.OaSource:
        raise ValueError('Expecting value: line 1 column 1 (char 0)')  # shape of a JSONDecodeError

    with pytest.raises(ValueError, match='Expecting value'):
        asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=fetch, convert_pdf=_ocr_boom, now=lambda: _CAPTURED_AT))
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None


def test_a_redelivery_runs_no_settled_step_again(gcs_bucket: gcs.Bucket) -> None:
    # Cloud Tasks delivers at-least-once; the log, not the marker, is what keeps a redelivery from re-running the
    # ladder or paying for the transcription again.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    async def refuse(_pdf_bytes: bytes, *, timeout: float | None) -> ocr.OcrRendering:
        del timeout
        raise ocr.OcrError('refused')

    first = asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=refuse))
    again = asyncio.run(_produce(gcs_bucket, _DOC_ID, attempt=_OTHER))

    assert first is again is outcome.Readiness.FAILED


def test_a_marker_does_not_stop_a_step_with_work(gcs_bucket: gcs.Bucket) -> None:
    # A marker is what readers see; a PDF no route has tried still has work under it.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    outcome.write_outcome(
        gcs_bucket, _DOC_ID, outcome.FetchOutcome(kind=outcome.OutcomeKind.NO_FULL_TEXT, at=_CAPTURED_AT)
    )

    readiness = asyncio.run(
        _produce(gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=_ocr_returning('# OCR\n'))
    )

    assert readiness is outcome.Readiness.READY
    assert outcome.read_readiness(gcs_bucket, _DOC_ID) is outcome.Readiness.READY


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


def test_produce_raises_when_the_paper_is_not_in_the_corpus(gcs_bucket: gcs.Bucket) -> None:
    with pytest.raises(produce.PaperNotInCorpusError):
        asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_boom, convert_pdf=_ocr_boom))


def test_produce_raises_not_found_when_the_manifest_names_an_absent_seed(gcs_bucket: gcs.Bucket) -> None:
    """A seed blob the manifest names but the bucket lacks is operational — retryable, not settled."""
    data = b'%PDF-1.7 seed'
    _write_multi_revision_pdf_paper(gcs_bucket, [(data, _CAPTURED_AT)])
    gcs_bucket.blob(
        writer.source_revision_path(
            _DOC_ID, 'pdf', hashlib.sha256(data).hexdigest(), litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF
        )
    ).delete()
    with pytest.raises(api_exceptions.NotFound):
        asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_boom, convert_pdf=_ocr_boom, now=lambda: _CAPTURED_AT))


def test_produce_settles_failed_when_the_manifest_refuses_the_write(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A write the writer refuses is refused on every attempt, so the paid conversion must not be retried.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    def refuse(*_args: object, **_kwargs: object) -> writer.AddRenderingResult:
        raise writer.ManifestRefusedError('manifest breaks its constraints: manifest.deps_resolve')

    monkeypatch.setattr(writer, 'add_rendering', refuse)
    readiness = asyncio.run(
        _produce(
            gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=_ocr_returning('# OCR\n'), now=lambda: _CAPTURED_AT
        )
    )
    assert readiness is outcome.Readiness.FAILED
    marker = outcome.read_outcome(gcs_bucket, _DOC_ID)
    assert marker is not None
    assert 'refused the write' in marker.error


def test_produce_settles_a_stored_manifest_that_breaks_its_constraints_before_any_conversion(
    gcs_bucket: gcs.Bucket,
) -> None:
    # Every write would be refused, so neither the OA fetch nor the OCR may run first.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    manifest = _load_manifest(gcs_bucket)
    manifest.sources.add(handle='orphan')  # a lineage with no access and no revisions
    gcs_bucket.blob(writer.manifest_path(_DOC_ID)).upload_from_string(manifest.SerializeToString())

    readiness = asyncio.run(
        _produce(gcs_bucket, _DOC_ID, fetch=_boom, fetch_pdf=_pdf_boom, convert_pdf=_ocr_boom, now=lambda: _CAPTURED_AT)
    )

    assert readiness is outcome.Readiness.FAILED
    marker = outcome.read_outcome(gcs_bucket, _DOC_ID)
    assert marker is not None
    assert 'breaks its constraints' in marker.error


def test_produce_lets_a_transient_write_fault_propagate_for_retry(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A garbled storage response surfaces as a plain ValueError (a JSON decode error); it is not the writer's
    # refusal, so the paper is left to be retried rather than settled.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    def garbled(*_args: object, **_kwargs: object) -> writer.AddRenderingResult:
        raise ValueError('Expecting value: line 1 column 1 (char 0)')

    monkeypatch.setattr(writer, 'add_rendering', garbled)
    with pytest.raises(ValueError, match='Expecting value'):
        asyncio.run(
            _produce(
                gcs_bucket,
                _DOC_ID,
                fetch=_returning(None),
                convert_pdf=_ocr_returning('# OCR\n'),
                now=lambda: _CAPTURED_AT,
            )
        )
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None


def test_a_request_naming_no_target_runs_the_free_steps_and_spends_nothing(gcs_bucket: gcs.Bucket) -> None:
    # A PDF waits for a request that reaches its transcription; until one does, its route has not settled.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    readiness = asyncio.run(_produce(gcs_bucket, _DOC_ID, targets=(), fetch=_returning(None)))

    assert readiness is outcome.Readiness.PENDING
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None
    assert _log(gcs_bucket) == [('xml', work_pb2.JOB_EVENT_SETTLED, work_pb2.SETTLED_OUTCOME_NOTHING)]


def test_a_request_naming_the_transcription_upgrades_a_seed_papers_text(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), renderings=[_docling_rendering()])

    readiness = asyncio.run(
        _produce(
            gcs_bucket,
            _DOC_ID,
            targets=(produce.TRANSCRIPTION_STEP,),
            fetch=_returning(None),
            convert_pdf=_ocr_returning('# OCR\n'),
        )
    )

    assert readiness is outcome.Readiness.READY
    assert _served(_load_manifest(gcs_bucket)).rendering.converter == litcache_pb2.Converter.CONVERTER_LLM_OCR


def test_a_paper_whose_text_came_from_xml_still_gets_its_pdf(gcs_bucket: gcs.Bucket) -> None:
    # Locating is free and reads nothing the XML step writes, so it runs off the main text's path.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), sources=[])

    readiness = asyncio.run(
        _produce(gcs_bucket, _DOC_ID, fetch=_returning(_oa_source()), fetch_pdf=_pdf_returning(_located_pdf()))
    )

    assert readiness is outcome.Readiness.READY
    manifest = _load_manifest(gcs_bucket)
    assert sorted(s.handle for s in manifest.sources) == ['pdf', 'xml']
    assert _served(manifest).rendering.converter == litcache_pb2.Converter.CONVERTER_LITDOWN


def test_a_transcription_running_elsewhere_is_left_to_it(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    begun = _OTHER.entry(
        at=_CAPTURED_AT, step=produce.TRANSCRIPTION_STEP, inputs=(_PDF_HASH,), event=work_pb2.JOB_EVENT_BEGUN
    )
    joblog.append(gcs_bucket, _DOC_ID, [begun])

    readiness = asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None)))

    assert readiness is outcome.Readiness.PENDING
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None


def test_a_transcription_spent_three_times_settles_failed_without_a_fourth(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    spent = [
        joblog.Attempt(id=f'00000000-0000-4000-8000-00000000001{n}', queue='q', task=f't{n}', retry_count=0).entry(
            at=_CAPTURED_AT,
            step=produce.TRANSCRIPTION_STEP,
            inputs=(_PDF_HASH,),
            event=work_pb2.JOB_EVENT_FAILED,
            model_answered=True,
        )
        for n in range(3)
    ]
    joblog.append(gcs_bucket, _DOC_ID, spent)

    readiness = asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None)))

    assert readiness is outcome.Readiness.FAILED
    marker = outcome.read_outcome(gcs_bucket, _DOC_ID)
    assert marker is not None
    assert 'committed nothing' in marker.error


def test_a_transcription_interrupted_before_the_model_answered_is_logged_unspent(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    async def overloaded(_pdf_bytes: bytes, *, timeout: float | None) -> ocr.OcrRendering:
        del timeout
        raise RuntimeError('overloaded')

    with pytest.raises(RuntimeError, match='overloaded'):
        asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=overloaded))

    (failed,) = [e for e in joblog.read(gcs_bucket, _DOC_ID).entries if e.event == work_pb2.JOB_EVENT_FAILED]
    assert not failed.model_answered


def test_a_transcription_paid_for_and_not_committed_is_logged_spent(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    def contended(*_args: object, **_kwargs: object) -> writer.AddRenderingResult:
        raise writer.ConcurrentWriteError('did not converge')

    monkeypatch.setattr(writer, 'add_rendering', contended)
    with pytest.raises(writer.ConcurrentWriteError):
        asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=_ocr_returning('# OCR\n')))

    (failed,) = [e for e in joblog.read(gcs_bucket, _DOC_ID).entries if e.event == work_pb2.JOB_EVENT_FAILED]
    assert failed.model_answered


def test_an_unknown_target_is_refused_before_any_work(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    with pytest.raises(produce.UnknownTargetError):
        asyncio.run(_produce(gcs_bucket, _DOC_ID, targets=('extract_variants',)))
    assert not joblog.read(gcs_bucket, _DOC_ID).entries


def _manifest(*, pdf: bool = False, converters: tuple[litcache_pb2.Converter, ...] = ()) -> litcache_pb2.Manifest:
    manifest = litcache_pb2.Manifest()
    if pdf:
        manifest.sources.add(
            handle='pdf',
            media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
            revisions=[litcache_pb2.Revision(hash=_PDF_HASH)],
        )
    for n, converter in enumerate(converters):
        key = f'{n:064x}'
        manifest.records[key].rendering.converter = converter
        manifest.records[key].deps.append(_PDF_HASH)
        manifest.refs['markdown'] = key
    return manifest


@pytest.mark.parametrize(
    ('targets', 'manifest', 'reached'),
    [
        pytest.param((), _manifest(pdf=True), set(), id='no target reaches nothing'),
        pytest.param(
            (work.MAIN_TEXT_TARGET,), _manifest(pdf=True), {'xml', 'transcription'}, id='main text reaches its routes'
        ),
        pytest.param(
            (work.MAIN_TEXT_TARGET,), _manifest(), {'xml', 'locate', 'transcription'}, id='and the PDF it reads'
        ),
        pytest.param(
            (work.MAIN_TEXT_TARGET,),
            _manifest(pdf=True, converters=(litcache_pb2.Converter.CONVERTER_DOCLING,)),
            set(),
            id='met main text reaches nothing',
        ),
        pytest.param(
            (produce.TRANSCRIPTION_STEP,),
            _manifest(pdf=True, converters=(litcache_pb2.Converter.CONVERTER_DOCLING,)),
            {'xml', 'transcription'},
            id='a named transcription is reached past met text',
        ),
        pytest.param(
            (produce.TRANSCRIPTION_STEP,),
            _manifest(pdf=True, converters=(litcache_pb2.Converter.CONVERTER_LITDOWN,)),
            set(),
            id='text from XML leaves the transcription no work',
        ),
    ],
)
def test_what_a_request_reaches(targets: tuple[str, ...], manifest: litcache_pb2.Manifest, reached: set[str]) -> None:
    assert produce.reach(targets, manifest) == reached


def test_a_paper_with_text_and_a_broken_manifest_stays_ready(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), renderings=[_docling_rendering()])
    manifest = _load_manifest(gcs_bucket)
    manifest.sources.add(handle='orphan')
    gcs_bucket.blob(writer.manifest_path(_DOC_ID)).upload_from_string(manifest.SerializeToString())

    assert asyncio.run(_produce(gcs_bucket, _DOC_ID)) is outcome.Readiness.READY
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None


def test_a_transcription_another_attempt_gives_up_on_mid_request_is_left_to_its_task(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other attempt is running when this one tries to begin, and closes before this one rereads the log.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    joblog.append(
        gcs_bucket,
        _DOC_ID,
        [
            _OTHER.entry(
                at=_CAPTURED_AT, step=produce.TRANSCRIPTION_STEP, inputs=(_PDF_HASH,), event=work_pb2.JOB_EVENT_BEGUN
            )
        ],
    )
    real_begin = joblog.begin

    def begin_then_the_other_gives_up(*args: object, **kwargs: object) -> joblog.Verdict:
        verdict = real_begin(*args, **kwargs)  # type: ignore[arg-type]
        failed = _OTHER.entry(
            at=_CAPTURED_AT, step=produce.TRANSCRIPTION_STEP, inputs=(_PDF_HASH,), event=work_pb2.JOB_EVENT_FAILED
        )
        joblog.append(gcs_bucket, _DOC_ID, [failed])
        return verdict

    monkeypatch.setattr(joblog, 'begin', begin_then_the_other_gives_up)

    assert asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None))) is outcome.Readiness.PENDING


def test_a_failure_the_log_cannot_record_still_surfaces(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    real_append = joblog.append

    def append_all_but_failures(bucket: gcs.Bucket, doc_id: str, entries: list[work_pb2.JobLogEntry]) -> None:
        if any(e.event == work_pb2.JOB_EVENT_FAILED for e in entries):
            raise joblog.LogContentionError('lost every race')
        real_append(bucket, doc_id, entries)

    async def overloaded(_pdf_bytes: bytes, *, timeout: float | None) -> ocr.OcrRendering:
        del timeout
        raise RuntimeError('overloaded')

    monkeypatch.setattr(joblog, 'append', append_all_but_failures)
    with pytest.raises(RuntimeError, match='overloaded'):
        asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=overloaded))


def test_a_transcription_refused_at_the_spend_limit_is_left_undone(gcs_bucket: gcs.Bucket) -> None:
    # The limit resets on the 1st, long after the queue's retries run out, so the request ends with the step undone.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    async def refused(_pdf_bytes: bytes, *, timeout: float | None) -> ocr.OcrRendering:
        del timeout
        raise ocr.SpendRefusedError('You have reached your specified workspace API usage limits')

    readiness = asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=refused))

    assert readiness is outcome.Readiness.PENDING
    assert outcome.read_outcome(gcs_bucket, _DOC_ID) is None
    transcription = [(e, o) for s, e, o in _log(gcs_bucket) if s == produce.TRANSCRIPTION_STEP]
    assert transcription == [
        (work_pb2.JOB_EVENT_BEGUN, work_pb2.SETTLED_OUTCOME_UNSPECIFIED),
        (work_pb2.JOB_EVENT_REFUSED, work_pb2.SETTLED_OUTCOME_UNSPECIFIED),
    ]


def test_refusals_never_use_up_a_transcriptions_attempts(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    async def refused(_pdf_bytes: bytes, *, timeout: float | None) -> ocr.OcrRendering:
        del timeout
        raise ocr.SpendRefusedError('You have reached your specified workspace API usage limits')

    for n in range(4):
        attempt = joblog.Attempt(id=f'00000000-0000-4000-8000-00000000002{n}', queue='q', task='t', retry_count=n)
        asyncio.run(_produce(gcs_bucket, _DOC_ID, attempt=attempt, fetch=_returning(None), convert_pdf=refused))

    readiness = asyncio.run(_produce(gcs_bucket, _DOC_ID, attempt=_OTHER, convert_pdf=_ocr_returning('# OCR\n')))
    assert readiness is outcome.Readiness.READY


def test_a_transcription_the_model_answered_and_that_then_failed_is_logged_spent(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    async def cut_off(_pdf_bytes: bytes, *, timeout: float | None) -> ocr.OcrRendering:
        del timeout
        raise ocr.SpentError('the transcription stream failed after the model answered')

    with pytest.raises(ocr.SpentError):
        asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=cut_off))

    (failed,) = [e for e in joblog.read(gcs_bucket, _DOC_ID).entries if e.event == work_pb2.JOB_EVENT_FAILED]
    assert failed.model_answered


_FIGURE = oa.SupplementaryFile(
    content=b'col\n1\n', filename='table_s1.csv', media_type='text/csv', origin_url='https://pmc.example/s1.csv'
)


def test_a_paper_whose_xml_lands_gets_its_supplementary_files(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    readiness = asyncio.run(
        _produce(
            gcs_bucket,
            _DOC_ID,
            fetch=_returning(_oa_source()),
            fetch_supplementary=_supplements_returning([_FIGURE]),
        )
    )

    assert readiness is outcome.Readiness.READY
    (entry,) = _load_manifest(gcs_bucket).files
    assert (entry.name, entry.hash) == ('table_s1.csv', hashlib.sha256(_FIGURE.content).hexdigest())
    assert entry.source_url == 'https://pmc.example/s1.csv'
    assert not [e for e in _log(gcs_bucket) if e[0] == produce.SUPPLEMENTS_STEP]


def test_a_paper_with_no_xml_has_no_supplements_to_fetch(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), renderings=[_docling_rendering()])

    asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None), fetch_supplementary=_supplements_boom))

    assert not _load_manifest(gcs_bucket).files


def test_supplements_none_are_listed_for_settle_on_the_xml_they_were_looked_for_with(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(_oa_source())))

    (settled,) = [e for e in joblog.read(gcs_bucket, _DOC_ID).entries if e.step == produce.SUPPLEMENTS_STEP]
    assert settled.outcome == work_pb2.SETTLED_OUTCOME_NOTHING
    assert list(settled.inputs) == [hashlib.sha256(_JATS).hexdigest()]
    # A redelivery does not list them again.
    asyncio.run(_produce(gcs_bucket, _DOC_ID, attempt=_OTHER, fetch_supplementary=_supplements_boom))


def test_a_supplement_that_cannot_be_stored_settles_the_step_and_leaves_the_text(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    async def unstorable(_article_ids: ids.ArticleIds, **_kwargs: object) -> list[oa.SupplementaryFile]:
        raise oa.OaSourceError('supplementary file has no storable extension: https://pmc.example/s1')

    readiness = asyncio.run(
        _produce(gcs_bucket, _DOC_ID, fetch=_returning(_oa_source()), fetch_supplementary=unstorable)
    )

    assert readiness is outcome.Readiness.READY
    (settled,) = [e for e in joblog.read(gcs_bucket, _DOC_ID).entries if e.step == produce.SUPPLEMENTS_STEP]
    assert settled.outcome == work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE


def test_a_request_for_supplements_reaches_the_xml_they_are_listed_by() -> None:
    assert produce.reach((produce.SUPPLEMENTS_STEP,), _manifest(pdf=True)) == {'xml', 'supplements'}


def test_a_supplement_that_cannot_be_downloaded_is_tried_again_then_settled(gcs_bucket: gcs.Bucket) -> None:
    # A dead link and a passing fault look alike, so the step is left undone for a later request, and settles once
    # the bound is reached rather than failing every request for the paper.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    async def unreachable(_article_ids: ids.ArticleIds, **_kwargs: object) -> list[oa.SupplementaryFile]:
        raise oa.UpstreamUnavailableError('listed supplementary file could not be downloaded')

    supplements: list[tuple[int, int]] = []
    for n in range(produce.SUPPLEMENT_ATTEMPTS_LIMIT):
        attempt = joblog.Attempt(id=f'00000000-0000-4000-8000-00000000003{n}', queue='q', task='t', retry_count=n)
        readiness = asyncio.run(
            _produce(
                gcs_bucket, _DOC_ID, attempt=attempt, fetch=_returning(_oa_source()), fetch_supplementary=unreachable
            )
        )
        assert readiness is outcome.Readiness.READY
        supplements = [(event, kind) for step, event, kind in _log(gcs_bucket) if step == produce.SUPPLEMENTS_STEP]

    assert supplements == [
        (work_pb2.JOB_EVENT_FAILED, work_pb2.SETTLED_OUTCOME_UNSPECIFIED),
        (work_pb2.JOB_EVENT_FAILED, work_pb2.SETTLED_OUTCOME_UNSPECIFIED),
        (work_pb2.JOB_EVENT_SETTLED, work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE),
    ]


# --- variants

_VARIANT_MARKDOWN = '# Results\n\nThe proband carried c.123A>G in GENE1.\n'
_EXTRACTED = variants_pb2.VariantExtractionConstruction(
    model='claude-test', harness_version=variant_extract.HARNESS_VERSION
)


def _mention_set() -> variants_pb2.MentionSet:
    mentions = variants_pb2.MentionSet()
    mention = mentions.mentions.add(
        verbatim='c.123A>G',
        gene='GENE1',
        disposition=variants_pb2.DISPOSITION_HAS_CANDIDATES,
        known_species=variants_pb2.SPECIES_HUMAN,
    )
    mention.candidates.add(hgvs='NM_000001.1:c.123A>G')
    mention.occurrences.add(context=variants_pb2.CONTEXT_OBSERVED, line=3)
    return mentions


def _write_variant_paper(bucket: gcs.Bucket, markdown: str = _VARIANT_MARKDOWN) -> None:
    rendering = dataclasses.replace(_docling_rendering(), markdown=markdown)
    _write_paper(bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), renderings=[rendering])


@dataclasses.dataclass
class _Variants:
    """Variant collaborators that record what they were given, deriving one candidate per mention."""

    validation: variants_pb2.VariantValidationConstruction = dataclasses.field(
        default_factory=lambda: variants_pb2.VariantValidationConstruction(
            weaver_version='2.1.0',
            stores=[variants_pb2.StoreVersion(name='refseq', release='r1')],
            validator_version='9',
        )
    )
    extracted: list[str] = dataclasses.field(default_factory=list)
    validated: list[tuple[variants_pb2.MentionSet, str]] = dataclasses.field(default_factory=list)
    extract_error: Exception | None = None

    async def extract(self, markdown: str) -> variant_extract.Extraction:
        self.extracted.append(markdown)
        if self.extract_error is not None:
            raise self.extract_error
        construction = variants_pb2.VariantExtractionConstruction()
        construction.CopyFrom(_EXTRACTED)
        construction.harness_version = variant_extract.HARNESS_VERSION  # the harness running now, as the agent's is
        return variant_extract.Extraction(
            mention_set=_mention_set(), construction=construction, turns=3, tool_calls=collections.Counter(), usage=[]
        )

    def validate(
        self, mention_set: variants_pb2.MentionSet, text: str
    ) -> tuple[variants_pb2.MentionSet, variants_pb2.Verdicts, variants_pb2.VariantValidationConstruction]:
        self.validated.append((mention_set, text))
        completed = variants_pb2.MentionSet()
        completed.CopyFrom(mention_set)
        verdicts = variants_pb2.Verdicts()
        for mention in completed.mentions:
            mention.candidates.add(
                hgvs='NM_000001.2:c.123A>G',
                rewrites=[variants_pb2.Rewrite(kind=variants_pb2.REWRITE_KIND_VERSION_UPDATE)],
            )
            verdict = verdicts.mentions.add(choice=variants_pb2.CHOICE_CHOSEN, chosen=1)
            for _ in mention.candidates:
                verdict.candidates.add(outcome=variants_pb2.OUTCOME_VALIDATED)
        return completed, verdicts, self.validation

    def seam(self) -> produce.Variants:
        return produce.Variants(extract=self.extract, validate=self.validate, validation=self.validation)


def _named(bucket: gcs.Bucket, ref: str, directory: str) -> bytes:
    key = _load_manifest(bucket).refs[ref]
    return bucket.blob(writer.record_path(_DOC_ID, directory, key, 'pb')).download_as_bytes()


def test_a_request_for_variants_extracts_from_the_served_text_then_validates(gcs_bucket: gcs.Bucket) -> None:
    _write_variant_paper(gcs_bucket)
    variants = _Variants()

    asyncio.run(
        _produce(
            gcs_bucket, _DOC_ID, targets=(produce.VARIANTS_STEP,), fetch=_returning(None), variants=variants.seam()
        )
    )

    assert variants.extracted == [_VARIANT_MARKDOWN]
    manifest = _load_manifest(gcs_bucket)
    extraction = manifest.records[manifest.refs['variants']]
    assert list(extraction.deps) == [manifest.refs['markdown']]
    assert extraction.variant_extraction == _EXTRACTED
    assert variants_pb2.MentionSet.FromString(_named(gcs_bucket, 'variants', 'variants')) == _mention_set()
    # Validation read the agent's mention set and the text it was read from.
    assert variants.validated == [(_mention_set(), _VARIANT_MARKDOWN)]
    validation = manifest.records[manifest.refs['verdicts']]
    assert list(validation.deps) == [manifest.refs['variants']]
    assert validation.variant_validation == variants.validation
    # The candidate validation derived is kept with its verdicts, after the agent's own.
    (verdict,) = variants_pb2.Verdicts.FromString(_named(gcs_bucket, 'verdicts', 'verdicts')).mentions
    assert [c.hgvs for c in verdict.derived] == ['NM_000001.2:c.123A>G']
    assert len(verdict.candidates) == 2


def test_a_request_without_the_variants_target_spends_nothing_on_them(gcs_bucket: gcs.Bucket) -> None:
    _write_variant_paper(gcs_bucket)

    asyncio.run(_produce(gcs_bucket, _DOC_ID, targets=(), fetch=_returning(None)))

    assert 'variants' not in _load_manifest(gcs_bucket).refs


def test_a_text_the_scan_finds_no_variant_in_settles_without_the_agent(gcs_bucket: gcs.Bucket) -> None:
    _write_variant_paper(gcs_bucket, markdown='# Methods\n\nNo variants here.\n')

    asyncio.run(_produce(gcs_bucket, _DOC_ID, targets=(produce.VARIANTS_STEP,), fetch=_returning(None)))

    assert (produce.VARIANTS_STEP, work_pb2.JOB_EVENT_SETTLED, work_pb2.SETTLED_OUTCOME_NOTHING) in _log(gcs_bucket)


def test_an_extraction_the_model_will_not_finish_settles_as_failed(gcs_bucket: gcs.Bucket) -> None:
    _write_variant_paper(gcs_bucket)
    variants = _Variants(
        extract_error=variant_extract.ExtractionError('turn 2 stopped with refusal', partial=_mention_set(), turns=2)
    )

    asyncio.run(
        _produce(
            gcs_bucket, _DOC_ID, targets=(produce.VARIANTS_STEP,), fetch=_returning(None), variants=variants.seam()
        )
    )

    entries = [(e.event, e.outcome) for e in joblog.read(gcs_bucket, _DOC_ID).entries if e.step == 'variants']
    assert entries == [
        (work_pb2.JOB_EVENT_BEGUN, work_pb2.SETTLED_OUTCOME_UNSPECIFIED),
        (work_pb2.JOB_EVENT_SETTLED, work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE),
    ]
    assert 'variants' not in _load_manifest(gcs_bucket).refs


def test_an_extraction_refused_at_the_spend_limit_is_left_undone(gcs_bucket: gcs.Bucket) -> None:
    _write_variant_paper(gcs_bucket)
    variants = _Variants(extract_error=ocr.SpendRefusedError('workspace limit'))

    asyncio.run(
        _produce(
            gcs_bucket, _DOC_ID, targets=(produce.VARIANTS_STEP,), fetch=_returning(None), variants=variants.seam()
        )
    )

    events = [e.event for e in joblog.read(gcs_bucket, _DOC_ID).entries if e.step == 'variants']
    assert events == [work_pb2.JOB_EVENT_BEGUN, work_pb2.JOB_EVENT_REFUSED]


def _late(by: datetime.timedelta) -> Callable[[], datetime.datetime]:
    """A clock for a request that starts at `_CAPTURED_AT` and has run for `by` at every later reading."""
    readings = iter([_CAPTURED_AT])
    return lambda: next(readings, _CAPTURED_AT + by)


def test_a_transcription_begun_late_is_given_only_the_time_its_request_has_left(gcs_bucket: gcs.Bucket) -> None:
    # Begun 20 minutes into a 30-minute request: it may run until a minute before the end, to commit.
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    timeouts: list[float | None] = []

    async def convert(_pdf_bytes: bytes, *, timeout: float | None) -> ocr.OcrRendering:
        timeouts.append(timeout)
        return ocr.OcrRendering(markdown='# OCR\n', model='claude-sonnet-5', converter_version='1.1')

    asyncio.run(
        _produce(
            gcs_bucket, _DOC_ID, fetch=_returning(None), convert_pdf=convert, now=_late(datetime.timedelta(minutes=20))
        )
    )

    assert timeouts == [datetime.timedelta(minutes=9).total_seconds()]


def test_a_request_with_less_than_a_transcriptions_expected_time_left_defers_it(gcs_bucket: gcs.Bucket) -> None:
    _write_paper(gcs_bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))

    with pytest.raises(produce.DeferredError):
        asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None), now=_late(datetime.timedelta(minutes=28))))

    assert not [e for e in joblog.read(gcs_bucket, _DOC_ID).entries if e.step == produce.TRANSCRIPTION_STEP]


def test_a_request_without_the_time_to_finish_an_extraction_does_not_begin_it(gcs_bucket: gcs.Bucket) -> None:
    _write_variant_paper(gcs_bucket)
    variants = _Variants()

    with pytest.raises(produce.DeferredError):
        asyncio.run(
            _produce(
                gcs_bucket,
                _DOC_ID,
                targets=(produce.VARIANTS_STEP,),
                fetch=_returning(None),
                variants=variants.seam(),
                now=_late(datetime.timedelta(minutes=10)),
            )
        )

    assert variants.extracted == []
    assert not [e for e in joblog.read(gcs_bucket, _DOC_ID).entries if e.step == produce.VARIANTS_STEP]


def test_a_text_with_nothing_variant_like_settles_however_late_in_its_request(gcs_bucket: gcs.Bucket) -> None:
    # The scan spends nothing, so it runs before the time check: such a paper is never deferred.
    _write_variant_paper(gcs_bucket, markdown='# Methods\n\nNo variants here.\n')

    asyncio.run(
        _produce(
            gcs_bucket,
            _DOC_ID,
            targets=(produce.VARIANTS_STEP,),
            fetch=_returning(None),
            now=_late(datetime.timedelta(minutes=29)),
        )
    )

    assert (produce.VARIANTS_STEP, work_pb2.JOB_EVENT_SETTLED, work_pb2.SETTLED_OUTCOME_NOTHING) in _log(gcs_bucket)


def _new_harness(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr(variant_extract, 'HARNESS_VERSION', version)
    monkeypatch.setattr(produce, '_HARNESS_INPUT', hashlib.sha256(version.encode()).hexdigest())


def _extract_variants(bucket: gcs.Bucket, variants: _Variants, *, days: int) -> None:
    # Each a delivery of its own, days apart: long past the dispatch deadline of any attempt before it.
    at = _CAPTURED_AT + datetime.timedelta(days=days)
    attempt = joblog.Attempt(id=str(uuid.UUID(int=days)), queue='bulk', task=f'task-{days}', retry_count=0)
    asyncio.run(
        _produce(
            bucket,
            _DOC_ID,
            targets=(produce.VARIANTS_STEP,),
            fetch=_returning(None),
            variants=variants.seam(),
            attempt=attempt,
            now=lambda: at,
        )
    )


def test_each_new_harness_extracts_the_same_rendering_once(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Extractions committed under earlier harnesses are not attempts that paid and committed nothing.
    _write_variant_paper(gcs_bucket)
    variants = _Variants()
    for day in range(1, 5):
        _new_harness(monkeypatch, f'h{day}')
        _extract_variants(gcs_bucket, variants, days=day)
    assert len(variants.extracted) == 4


def test_a_new_harness_tries_again_an_extraction_that_failed_under_the_old(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_variant_paper(gcs_bucket)
    variants = _Variants(extract_error=variant_extract.ExtractionError('no end', partial=_mention_set(), turns=80))
    _extract_variants(gcs_bucket, variants, days=1)

    variants.extract_error = None
    _new_harness(monkeypatch, 'next')
    _extract_variants(gcs_bucket, variants, days=2)

    assert len(variants.extracted) == 2
    assert 'variants' in _load_manifest(gcs_bucket).refs


def test_verdicts_made_another_way_are_made_again_by_any_request(gcs_bucket: gcs.Bucket) -> None:
    # A weaver, store or validator change re-validates for free; the agent is not run again.
    _write_variant_paper(gcs_bucket)
    first = _Variants()
    asyncio.run(
        _produce(gcs_bucket, _DOC_ID, targets=(produce.VARIANTS_STEP,), fetch=_returning(None), variants=first.seam())
    )
    newer = _Variants()
    newer.validation.validator_version = '10'

    later = _CAPTURED_AT + datetime.timedelta(hours=1)

    asyncio.run(_produce(gcs_bucket, _DOC_ID, attempt=_OTHER, targets=(), variants=newer.seam(), now=lambda: later))

    assert newer.extracted == []
    assert len(newer.validated) == 1
    manifest = _load_manifest(gcs_bucket)
    assert manifest.records[manifest.refs['verdicts']].variant_validation.validator_version == '10'


def test_a_caller_that_cannot_say_how_verdicts_are_made_counts_any_as_current(gcs_bucket: gcs.Bucket) -> None:
    # The sweep holds no reference stores; it must not see validation work on every pass.
    _write_variant_paper(gcs_bucket)
    asyncio.run(
        _produce(
            gcs_bucket, _DOC_ID, targets=(produce.VARIANTS_STEP,), fetch=_returning(None), variants=_Variants().seam()
        )
    )

    work_left = produce.would_run(
        (),
        _load_manifest(gcs_bucket),
        joblog.read(gcs_bucket, _DOC_ID),
        now=_CAPTURED_AT,
        deadline=work.DISPATCH_DEADLINE,
    )

    assert produce.VERDICTS_STEP not in work_left


def test_a_newer_rendering_gives_the_variants_step_work_again() -> None:
    manifest = _manifest(pdf=True, converters=(litcache_pb2.Converter.CONVERTER_DOCLING,))
    held = manifest.refs['markdown']
    manifest.records['v' * 64].CopyFrom(litcache_pb2.Record(deps=[held], variant_extraction=_EXTRACTED))
    manifest.refs['variants'] = 'v' * 64
    assert produce.reach((produce.VARIANTS_STEP,), manifest) == frozenset()

    manifest.records['n' * 64].CopyFrom(manifest.records[held])
    manifest.refs['markdown'] = 'n' * 64

    assert produce.reach((produce.VARIANTS_STEP,), manifest) == {produce.VARIANTS_STEP}


# --- figures and pedigrees

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_FIGURE_PDF = (_FIXTURES / 'oa' / 'source.pdf').read_bytes()


def _write_figure_paper(bucket: gcs.Bucket, pdf_bytes: bytes = _FIGURE_PDF, markdown: str = '# Results\n') -> str:
    source = dataclasses.replace(_pdf_source(), data=pdf_bytes)
    rendering = dataclasses.replace(
        _docling_rendering(), from_revision=hashlib.sha256(pdf_bytes).hexdigest(), markdown=markdown
    )
    _write_paper(
        bucket, external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'), sources=[source], renderings=[rendering]
    )
    return hashlib.sha256(pdf_bytes).hexdigest()


def _panel(kind: figures_pb2.FigureKind, page: int) -> figures_pb2.Panel:
    box = figures_pb2.Box(left=0.1, top=0.1, right=0.9, bottom=0.5)
    return figures_pb2.Panel(kinds=[kind], place=figures_pb2.Place(pdf_page=page, box=box))


def _legend(page: int) -> figures_pb2.Place:
    return figures_pb2.Place(pdf_page=page, box=figures_pb2.Box(left=0.1, top=0.6, right=0.9, bottom=0.7))


# Figure 1 draws a pedigree on page 2 with its legend on page 3; Figure 2 is a chart.
_A_PEDIGREE_FIGURE = figures_pb2.FigureSet(
    figures=[
        figures_pb2.Figure(label='Figure 1', legend=_legend(3), panels=[_panel(figures_pb2.FIGURE_KIND_PEDIGREE, 2)]),
        figures_pb2.Figure(label='Figure 2', legend=_legend(5), panels=[_panel(figures_pb2.FIGURE_KIND_CHART, 5)]),
    ]
)


@dataclasses.dataclass
class _Figures:
    """The figure collaborator, recording the pdfs it was asked to annotate and answering with a fixed set."""

    figure_set: figures_pb2.FigureSet = dataclasses.field(default_factory=lambda: _A_PEDIGREE_FIGURE)
    error: Exception | None = None
    annotated: list[int] = dataclasses.field(default_factory=list)

    async def annotate(self, _pdf: bytes, layouts: Sequence[pdf.PageLayout]) -> figure_annotate.Annotation:
        self.annotated.append(len(layouts))
        if self.error is not None:
            raise self.error
        construction = figures_pb2.FigureAnnotationConstruction(
            model='claude-haiku-5-5', harness_version=figure_annotate.HARNESS_VERSION
        )
        return figure_annotate.Annotation(figure_set=self.figure_set, construction=construction, usage=[])

    def seam(self) -> produce.Figures:
        return produce.Figures(annotate=self.annotate)


@dataclasses.dataclass
class _Pedigrees:
    """The pedigree collaborator, recording what it was asked to read and answering with one pedigree."""

    read_papers: list[pedigree_extract.PaperInput] = dataclasses.field(default_factory=list)
    error: Exception | None = None

    async def read(self, paper: pedigree_extract.PaperInput) -> pedigree_extract.Reading:
        self.read_papers.append(paper)
        if self.error is not None:
            raise self.error
        pedigree_set = pedigree_pb2.PedigreeSet()
        pedigree_set.pedigrees.add().individuals.add(generation=1, index=1, gender=pedigree_pb2.GENDER_MAN)
        construction = pedigrees_pb2.PedigreeExtractionConstruction(
            model='claude-sonnet-5-5',
            harness_version=pedigree_extract.HARNESS_VERSION,
            figures_read=[pedigrees_pb2.PedigreeFigure(figure=f) for f in sorted({u.figure for u in paper.figures})],
        )
        return pedigree_extract.Reading(pedigree_set=pedigree_set, construction=construction, usage=[])

    def seam(self) -> produce.Pedigrees:
        return produce.Pedigrees(read=self.read)


def _request_pedigrees(
    bucket: gcs.Bucket,
    figures: _Figures,
    pedigrees: _Pedigrees | None = None,
    *,
    at: datetime.datetime = _CAPTURED_AT,
    variants: _Variants | None = None,
) -> None:
    asyncio.run(
        _produce(
            bucket,
            _DOC_ID,
            fetch=_returning(None),
            targets=(produce.PEDIGREES_STEP,),
            figures=figures.seam(),
            pedigrees=(pedigrees or _Pedigrees()).seam(),
            variants=(variants or _Variants()).seam(),
            now=lambda: at,
        )
    )


def test_a_request_for_pedigrees_reads_the_paper_variants_first_and_names_them_to_the_reader(
    gcs_bucket: gcs.Bucket,
) -> None:
    pdf_hash = _write_figure_paper(gcs_bucket, markdown=_VARIANT_MARKDOWN)
    variants, pedigrees = _Variants(), _Pedigrees()

    _request_pedigrees(gcs_bucket, _Figures(), pedigrees, variants=variants)

    assert variants.extracted == [_VARIANT_MARKDOWN]
    (paper,) = pedigrees.read_papers
    assert list(paper.variants) == [
        pedigree_extract.KnownVariant(
            printed='c.123A>G',
            checked='NM_000001.2:c.123A>G',
            gene='GENE1',
            lines=('The proband carried c.123A>G in GENE1.',),
        )
    ]
    manifest = _load_manifest(gcs_bucket)
    record = manifest.records[manifest.refs['pedigrees']]
    assert list(record.deps) == [
        manifest.refs['markdown'],
        manifest.refs['figures'],
        pdf_hash,
        manifest.refs['verdicts'],
    ]


def test_a_paper_with_no_variant_is_read_for_pedigrees_without_any(gcs_bucket: gcs.Bucket) -> None:
    _write_figure_paper(gcs_bucket)
    pedigrees = _Pedigrees()

    _request_pedigrees(gcs_bucket, _Figures(), pedigrees)

    (paper,) = pedigrees.read_papers
    assert not paper.variants
    manifest = _load_manifest(gcs_bucket)
    assert 'verdicts' not in manifest.refs
    record = manifest.records[manifest.refs['pedigrees']]
    assert len(record.deps) == 3  # the rendering, the figure set and the pdf: no verdicts to depend on


def test_a_request_for_pedigrees_annotates_the_figures_then_reads_the_pedigree_figure(gcs_bucket: gcs.Bucket) -> None:
    pdf_hash = _write_figure_paper(gcs_bucket)
    figures, pedigrees = _Figures(), _Pedigrees()

    _request_pedigrees(gcs_bucket, figures, pedigrees)

    (paper,) = pedigrees.read_papers
    (unit,) = paper.figures
    assert (unit.figure, unit.label, unit.page.number) == (0, 'Figure 1', 2)
    assert unit.legend is not None
    assert unit.legend.number == 3
    assert paper.pdf_hash == pdf_hash
    manifest = _load_manifest(gcs_bucket)
    assert list(manifest.records[manifest.refs['figures']].deps) == [pdf_hash]
    record = manifest.records[manifest.refs['pedigrees']]
    assert list(record.deps) == [manifest.refs['markdown'], manifest.refs['figures'], pdf_hash]
    assert [r.figure for r in record.pedigree_extraction.figures_read] == [0]
    stored = figures_pb2.FigureSet.FromString(_named(gcs_bucket, 'figures', 'figure-sets'))
    assert stored == _A_PEDIGREE_FIGURE


def test_a_paper_whose_figures_hold_no_pedigree_settles_without_a_reading(gcs_bucket: gcs.Bucket) -> None:
    _write_figure_paper(gcs_bucket)
    no_pedigree = figures_pb2.FigureSet(figures=[_A_PEDIGREE_FIGURE.figures[1]])

    _request_pedigrees(gcs_bucket, _Figures(figure_set=no_pedigree), _Pedigrees())

    assert ('pedigrees', work_pb2.JOB_EVENT_SETTLED, work_pb2.SETTLED_OUTCOME_NOTHING) in _log(gcs_bucket)
    refs = _load_manifest(gcs_bucket).refs
    assert 'figures' in refs
    assert 'pedigrees' not in refs


def test_a_request_without_the_targets_spends_nothing_on_figures_or_pedigrees(gcs_bucket: gcs.Bucket) -> None:
    _write_figure_paper(gcs_bucket)

    asyncio.run(_produce(gcs_bucket, _DOC_ID, fetch=_returning(None), targets=()))

    assert not {'figures', 'pedigrees'} & {step for step, _, _ in _log(gcs_bucket)}


def test_a_figure_drawn_across_two_pages_is_read_on_each_and_counted_once(gcs_bucket: gcs.Bucket) -> None:
    _write_figure_paper(gcs_bucket)
    spread = figures_pb2.Figure(
        label='Figure 1',
        legend=_legend(4),
        panels=[_panel(figures_pb2.FIGURE_KIND_PEDIGREE, 2), _panel(figures_pb2.FIGURE_KIND_PEDIGREE, 3)],
    )
    pedigrees = _Pedigrees()

    _request_pedigrees(gcs_bucket, _Figures(figure_set=figures_pb2.FigureSet(figures=[spread])), pedigrees)

    (paper,) = pedigrees.read_papers
    assert [(u.figure, u.page.number) for u in paper.figures] == [(0, 2), (0, 3)]


def test_a_paper_is_read_on_at_most_the_capped_number_of_pedigree_figures(gcs_bucket: gcs.Bucket) -> None:
    _write_figure_paper(gcs_bucket)
    many = figures_pb2.FigureSet(
        figures=[
            figures_pb2.Figure(label=f'Figure {n}', panels=[_panel(figures_pb2.FIGURE_KIND_PEDIGREE, 2)])
            for n in range(produce.MAX_PEDIGREE_READS + 3)
        ]
    )
    pedigrees = _Pedigrees()

    _request_pedigrees(gcs_bucket, _Figures(figure_set=many), pedigrees)

    (paper,) = pedigrees.read_papers
    assert [u.figure for u in paper.figures] == list(range(produce.MAX_PEDIGREE_READS))


def test_a_figure_that_does_not_fit_under_the_cap_is_left_out_whole(gcs_bucket: gcs.Bucket) -> None:
    _write_figure_paper(gcs_bucket)
    wide = [_panel(figures_pb2.FIGURE_KIND_PEDIGREE, page) for page in range(2, 2 + produce.MAX_PEDIGREE_READS)]
    one_page = [_panel(figures_pb2.FIGURE_KIND_PEDIGREE, 2)]
    figure_set = figures_pb2.FigureSet(
        figures=[
            figures_pb2.Figure(label='Figure 1', panels=one_page),
            figures_pb2.Figure(label='Figure 2', panels=wide),
            figures_pb2.Figure(label='Figure 3', panels=one_page),
        ]
    )
    pedigrees = _Pedigrees()

    _request_pedigrees(gcs_bucket, _Figures(figure_set=figure_set), pedigrees)

    (paper,) = pedigrees.read_papers
    assert [u.figure for u in paper.figures] == [0, 2]


def test_a_paper_whose_only_pedigree_figure_is_over_the_cap_settles_without_a_reading(gcs_bucket: gcs.Bucket) -> None:
    _write_figure_paper(gcs_bucket)
    wide = [_panel(figures_pb2.FIGURE_KIND_PEDIGREE, page) for page in range(1, 2 + produce.MAX_PEDIGREE_READS)]
    pedigrees = _Pedigrees()

    _request_pedigrees(
        gcs_bucket, _Figures(figure_set=figures_pb2.FigureSet(figures=[figures_pb2.Figure(panels=wide)])), pedigrees
    )

    assert not pedigrees.read_papers
    assert 'pedigrees' not in _load_manifest(gcs_bucket).refs
    (entry,) = [e for e in joblog.read(gcs_bucket, _DOC_ID).entries if e.step == produce.PEDIGREES_STEP]
    assert entry.outcome == work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE


def test_a_reading_that_ends_without_an_answer_settles_as_a_permanent_failure(gcs_bucket: gcs.Bucket) -> None:
    _write_figure_paper(gcs_bucket)
    failing = _Pedigrees(error=pedigree_extract.ExtractionError('Figure 1 on page 2 stopped with refusal'))

    _request_pedigrees(gcs_bucket, _Figures(), failing)

    assert ('pedigrees', work_pb2.JOB_EVENT_SETTLED, work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE) in _log(gcs_bucket)


def test_an_annotation_that_ends_without_an_answer_settles_and_reads_no_pedigrees(gcs_bucket: gcs.Bucket) -> None:
    _write_figure_paper(gcs_bucket)
    pedigrees = _Pedigrees()

    _request_pedigrees(gcs_bucket, _Figures(error=figure_annotate.AnnotationError('stopped with refusal')), pedigrees)

    assert ('figures', work_pb2.JOB_EVENT_SETTLED, work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE) in _log(gcs_bucket)
    assert not pedigrees.read_papers


def test_a_pdf_that_cannot_be_read_settles_the_annotation_as_a_permanent_failure(gcs_bucket: gcs.Bucket) -> None:
    _write_figure_paper(gcs_bucket, b'%PDF-1.7 not a pdf')
    figures = _Figures()

    _request_pedigrees(gcs_bucket, figures)

    assert ('figures', work_pb2.JOB_EVENT_SETTLED, work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE) in _log(gcs_bucket)
    assert not figures.annotated


def test_a_pdf_past_the_page_limit_settles_before_anything_is_spent(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_figure_paper(gcs_bucket)
    monkeypatch.setattr(figure_annotate, 'MAX_PAGES', 1)
    figures = _Figures()

    _request_pedigrees(gcs_bucket, figures)

    assert ('figures', work_pb2.JOB_EVENT_SETTLED, work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE) in _log(gcs_bucket)
    assert not figures.annotated


def test_a_pdfium_crash_while_rendering_the_pages_settles_the_annotation(gcs_bucket: gcs.Bucket) -> None:
    _write_figure_paper(gcs_bucket)

    _request_pedigrees(gcs_bucket, _Figures(error=pdf.PdfiumCrashedError('pdfium crashed on page 3')))

    assert ('figures', work_pb2.JOB_EVENT_SETTLED, work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE) in _log(gcs_bucket)


def test_a_new_pdf_revision_is_annotated_again_before_its_pedigrees_are_read(gcs_bucket: gcs.Bucket) -> None:
    _write_figure_paper(gcs_bucket)
    # The newer revision has one page.
    on_page_one = figures_pb2.Figure(label='Figure 1', panels=[_panel(figures_pb2.FIGURE_KIND_PEDIGREE, 1)])
    figures, pedigrees = _Figures(figure_set=figures_pb2.FigureSet(figures=[on_page_one])), _Pedigrees()
    _request_pedigrees(gcs_bucket, figures, pedigrees)
    newer = (_FIXTURES / 'nonoa' / 'source.pdf').read_bytes()
    writer.add_source(
        gcs_bucket,
        _DOC_ID,
        dataclasses.replace(_pdf_source(), data=newer, captured_at=_CAPTURED_AT + datetime.timedelta(days=1)),
    )

    # A later run: the same figure set's bytes, made again from the newer revision, replace the held record.
    _request_pedigrees(gcs_bucket, figures, pedigrees, at=_CAPTURED_AT + datetime.timedelta(days=2))

    newer_hash = hashlib.sha256(newer).hexdigest()
    assert len(figures.annotated) == 2
    assert [paper.pdf_hash for paper in pedigrees.read_papers][-1] == newer_hash
    manifest = _load_manifest(gcs_bucket)
    assert newer_hash in manifest.records[manifest.refs['pedigrees']].deps


def test_pedigrees_are_not_read_from_a_figure_set_a_newer_annotation_running_elsewhere_replaces(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_hash = _write_figure_paper(gcs_bucket)
    figures = _Figures()
    asyncio.run(
        _produce(gcs_bucket, _DOC_ID, targets=(produce.FIGURES_STEP,), fetch=_returning(None), figures=figures.seam())
    )
    newer = hashlib.sha256(b'figure annotation harness newer').hexdigest()
    monkeypatch.setattr(figure_annotate, 'HARNESS_VERSION', 'newer')
    monkeypatch.setattr(produce, '_FIGURE_HARNESS_INPUT', newer)
    begun = _OTHER.entry(
        at=_CAPTURED_AT, step=produce.FIGURES_STEP, inputs=(pdf_hash, newer), event=work_pb2.JOB_EVENT_BEGUN
    )
    joblog.append(gcs_bucket, _DOC_ID, [begun])
    pedigrees = _Pedigrees()

    _request_pedigrees(gcs_bucket, figures, pedigrees)

    assert len(figures.annotated) == 1
    assert not pedigrees.read_papers


def test_a_request_for_pedigrees_reaches_a_variant_extraction_made_by_an_older_harness(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_figure_paper(gcs_bucket, markdown=_VARIANT_MARKDOWN)
    asyncio.run(
        _produce(
            gcs_bucket, _DOC_ID, targets=(produce.VERDICTS_STEP,), fetch=_returning(None), variants=_Variants().seam()
        )
    )
    monkeypatch.setattr(variant_extract, 'HARNESS_VERSION', 'a newer harness')

    reached = produce.reach((produce.PEDIGREES_STEP,), _load_manifest(gcs_bucket))

    # The pedigree step waits on the variant extraction, so a request for it must schedule that extraction too.
    assert produce.VARIANTS_STEP in reached
