"""Tests for the directory + manifest writer (`themis.litcache.writer`).

Backed by a fake-gcs-server bucket (Docker-gated via the shared `gcs_bucket` fixture): the
writer works against a real `google.cloud.storage.Bucket`, so the tests exercise the same
content-addressed writes and create-if-absent (`if_generation_match=0`) manifest commit the
production path uses.
"""

from __future__ import annotations

import datetime
import hashlib
import posixpath

import pytest
from google.cloud import storage as gcs
from google.protobuf import timestamp_pb2
from pubmed_proto import pubmed_pb2

from themis.litcache import writer
from themis.litcache.models import litcache_pb2

_CREATED_AT = datetime.datetime(2026, 6, 25, tzinfo=datetime.UTC)
_CAPTURED_AT = datetime.datetime(2026, 6, 24, tzinfo=datetime.UTC)
_DOC_ID = '9f3a0000-0000-4000-8000-000000000001'

_PDF_BYTES = b'%PDF-1.7 fake pdf bytes'
_PDF_HASH = hashlib.sha256(_PDF_BYTES).hexdigest()
_MARKDOWN = '# Title\n\nBody.\n'
_MARKDOWN_HASH = hashlib.sha256(_MARKDOWN.encode('utf-8')).hexdigest()


def _metadata(pmid: str = '29089047') -> bytes:
    article = pubmed_pb2.PubmedArticle()
    article.medline_citation.pmid.value = pmid
    return article.SerializeToString()


def _access() -> litcache_pb2.Access:
    return litcache_pb2.Access(free_to_read=litcache_pb2.FreeToRead())


def _source(data: bytes = _PDF_BYTES, *, handle: str = 'pdf', has_text_layer: bool | None = None) -> writer.SourceInput:
    return writer.SourceInput(
        handle=handle,
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
        kind=litcache_pb2.SourceKind.SOURCE_KIND_SEED,
        data=data,
        licence='https://creativecommons.org/licenses/by/4.0/',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT,
        access=_access(),
        captured_at=_CAPTURED_AT,
        has_text_layer=has_text_layer,
    )


def _rendering(
    *,
    from_source: str = 'pdf',
    from_revision: str = _PDF_HASH,
    converter: litcache_pb2.Converter = litcache_pb2.Converter.CONVERTER_DOCLING,
    model: str | None = None,
) -> litcache_pb2.Rendering:
    created_at = timestamp_pb2.Timestamp()
    created_at.FromDatetime(_CREATED_AT)
    rendering = litcache_pb2.Rendering(
        from_source=from_source,
        from_revision=from_revision,
        converter=converter,
        converter_version='2.0.0',
        created_at=created_at,
    )
    if model is not None:
        rendering.model = model
    return rendering


def _paper(**overrides: object) -> writer.PaperInput:
    defaults: dict[str, object] = {
        'doc_id': _DOC_ID,
        'external_ids': litcache_pb2.ExternalIds(doi='10.1/abc'),
        'claim_key': 'doi:10.1/abc',
        'equivalence': litcache_pb2.Equivalence(edges=[], canonical_doc_id=_DOC_ID),
        'retraction': litcache_pb2.Retraction(),
        'sources': [_source()],
        'renderings': [writer.RenderingInput(rendering=_rendering(), markdown=_MARKDOWN, docling_json=b'{"x": 1}')],
        'metadata': _metadata(),
        'files': (),
    }
    defaults.update(overrides)
    return writer.PaperInput(**defaults)  # type: ignore[arg-type]


def _read(bucket: gcs.Bucket, name: str) -> bytes:
    return bucket.blob(name).download_as_bytes()


def _pdf_path() -> str:
    return f'sources/pdf/{_PDF_HASH}.pdf'


def _markdown_path() -> str:
    return f'renderings/{_MARKDOWN_HASH}.md'


def test_write_paper_writes_expected_layout(gcs_bucket: gcs.Bucket) -> None:
    result = writer.write_paper(gcs_bucket, _paper())

    assert result.written is True
    paper_dir = posixpath.join('papers', _DOC_ID)
    assert gcs_bucket.blob(posixpath.join(paper_dir, 'manifest.pb')).exists()
    assert _read(gcs_bucket, posixpath.join(paper_dir, 'metadata.pb')) == _metadata()
    assert _read(gcs_bucket, posixpath.join(paper_dir, _pdf_path())) == _PDF_BYTES
    assert _read(gcs_bucket, posixpath.join(paper_dir, _markdown_path())) == _MARKDOWN.encode('utf-8')
    docling_path = _markdown_path().removesuffix('.md') + '.docling.json'
    assert _read(gcs_bucket, posixpath.join(paper_dir, docling_path)) == b'{"x": 1}'


def test_committed_manifest_parses_and_records_revisions_and_hashes(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())

    raw = _read(gcs_bucket, posixpath.join('papers', _DOC_ID, 'manifest.pb'))
    manifest = litcache_pb2.Manifest.FromString(raw)

    assert manifest.doc_id == _DOC_ID
    source = manifest.sources[0]
    assert source.handle == 'pdf'
    assert source.media_type == litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF
    assert [rev.hash for rev in source.revisions] == [_PDF_HASH]
    # The rendering is keyed by the markdown's content hash and points back at the
    # source revision it rendered.
    assert _MARKDOWN_HASH in manifest.renderings
    assert manifest.renderings[_MARKDOWN_HASH].from_revision == _PDF_HASH


def test_manifest_write_is_the_last_write(gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch) -> None:
    # A crash on the manifest write leaves every other artifact but no manifest,
    # so a re-run treats the paper as uncached.
    real_blob = gcs_bucket.blob

    def failing_blob(name: str) -> gcs.Blob:
        blob = real_blob(name)
        if name.endswith('manifest.pb'):

            def boom(*_args: object, **_kwargs: object) -> None:
                raise OSError('simulated crash before commit')

            monkeypatch.setattr(blob, 'upload_from_string', boom)
        return blob

    monkeypatch.setattr(gcs_bucket, 'blob', failing_blob)

    with pytest.raises(OSError, match='simulated crash'):
        writer.write_paper(gcs_bucket, _paper())

    paper_dir = posixpath.join('papers', _DOC_ID)
    assert not real_blob(posixpath.join(paper_dir, 'manifest.pb')).exists()
    assert real_blob(posixpath.join(paper_dir, 'metadata.pb')).exists()
    assert real_blob(posixpath.join(paper_dir, _pdf_path())).exists()


def test_re_completes_after_a_partial_write_reusing_the_doc_id(gcs_bucket: gcs.Bucket) -> None:
    paper_dir = posixpath.join('papers', _DOC_ID)
    # The partial state a crash-before-commit leaves behind: the content-addressed
    # source bytes are present, but no manifest.
    gcs_bucket.blob(posixpath.join(paper_dir, _pdf_path())).upload_from_string(_PDF_BYTES)

    result = writer.write_paper(gcs_bucket, _paper())

    assert result.written is True
    assert gcs_bucket.blob(posixpath.join(paper_dir, 'manifest.pb')).exists()
    assert _read(gcs_bucket, posixpath.join(paper_dir, _pdf_path())) == _PDF_BYTES


def test_skips_an_already_committed_paper(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    manifest_key = posixpath.join('papers', _DOC_ID, 'manifest.pb')
    committed = _read(gcs_bucket, manifest_key)

    # A second call with different source bytes must not write anything: the
    # existing manifest is the checkpoint, so the paper is skipped untouched.
    changed_bytes = b'DIFFERENT'
    changed_hash = hashlib.sha256(changed_bytes).hexdigest()
    changed = _paper(
        sources=[_source(data=changed_bytes)],
        renderings=[writer.RenderingInput(rendering=_rendering(from_revision=changed_hash), markdown=_MARKDOWN)],
    )
    second = writer.write_paper(gcs_bucket, changed)

    assert second.written is False
    assert _read(gcs_bucket, manifest_key) == committed
    changed_path = posixpath.join('papers', _DOC_ID, f'sources/pdf/{changed_hash}.pdf')
    assert not gcs_bucket.blob(changed_path).exists()


def test_a_lost_commit_race_adopts_the_winner(gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch) -> None:
    # First writer commits. A second writer whose skip-check is forced to miss (the
    # exists()-then-write window) reaches the create-only commit; if_generation_match=0
    # loses to the existing manifest, so it adopts the winner rather than clobbering it.
    writer.write_paper(gcs_bucket, _paper())
    manifest_key = posixpath.join('papers', _DOC_ID, 'manifest.pb')
    committed = _read(gcs_bucket, manifest_key)

    real_blob = gcs_bucket.blob

    def skip_check_misses(name: str) -> gcs.Blob:
        blob = real_blob(name)
        if name.endswith('manifest.pb'):
            monkeypatch.setattr(blob, 'exists', lambda: False)
        return blob

    monkeypatch.setattr(gcs_bucket, 'blob', skip_check_misses)

    changed_bytes = b'DIFFERENT'
    changed = _paper(
        sources=[_source(data=changed_bytes)],
        renderings=[
            writer.RenderingInput(
                rendering=_rendering(from_revision=hashlib.sha256(changed_bytes).hexdigest()), markdown=_MARKDOWN
            )
        ],
    )
    result = writer.write_paper(gcs_bucket, changed)

    assert result.written is False
    assert _read(gcs_bucket, manifest_key) == committed  # winner's manifest untouched


def test_content_addresses_a_fetched_blob_and_records_unfetched_files(gcs_bucket: gcs.Bucket) -> None:
    fig_bytes = b'\xff\xd8\xff fake jpeg'
    paper = _paper(
        files=[
            writer.FileInput(
                role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_FIGURE,
                name='fig1.jpg',
                source_url='https://x/fig1.jpg',
                data=fig_bytes,
            ),
            writer.FileInput(
                role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_FIGURE,
                name='fig2.jpg',
                source_url='https://x/fig2.jpg',
            ),
        ]
    )
    result = writer.write_paper(gcs_bucket, paper)

    figures = list(gcs_bucket.list_blobs(prefix=posixpath.join('papers', _DOC_ID, 'figures/')))
    assert len(figures) == 1  # only the fetched blob

    files = {f.name: f for f in result.manifest.files}
    digest = hashlib.sha256(fig_bytes).hexdigest()
    assert files['fig1.jpg'].path == f'figures/{digest}.jpg'
    assert _read(gcs_bucket, posixpath.join('papers', _DOC_ID, files['fig1.jpg'].path)) == fig_bytes
    assert not files['fig2.jpg'].HasField('path')  # un-fetched (lazy fetch)


def test_rendering_from_source_naming_no_source_fails_loud(gcs_bucket: gcs.Bucket) -> None:
    paper = _paper(renderings=[writer.RenderingInput(rendering=_rendering(from_source='xml'), markdown=_MARKDOWN)])
    with pytest.raises(ValueError, match='from_source'):
        writer.write_paper(gcs_bucket, paper)


def test_rendering_from_revision_naming_no_revision_fails_loud(gcs_bucket: gcs.Bucket) -> None:
    stale_rev = '0' * 64
    paper = _paper(
        renderings=[writer.RenderingInput(rendering=_rendering(from_revision=stale_rev), markdown=_MARKDOWN)]
    )
    with pytest.raises(ValueError, match='from_revision'):
        writer.write_paper(gcs_bucket, paper)


def test_model_inconsistent_with_converter_fails_loud(gcs_bucket: gcs.Bucket) -> None:
    # docling carries no model; supplying one is inconsistent.
    paper = _paper(
        renderings=[writer.RenderingInput(rendering=_rendering(model='claude-opus-4-8'), markdown=_MARKDOWN)]
    )
    with pytest.raises(ValueError, match='model'):
        writer.write_paper(gcs_bucket, paper)


def test_two_renderings_with_the_same_markdown_hash_fail_loud(gcs_bucket: gcs.Bucket) -> None:
    paper = _paper(
        renderings=[
            writer.RenderingInput(rendering=_rendering(), markdown=_MARKDOWN),
            writer.RenderingInput(rendering=_rendering(), markdown=_MARKDOWN),
        ]
    )
    with pytest.raises(ValueError, match='share the markdown hash'):
        writer.write_paper(gcs_bucket, paper)


def test_blob_with_unknown_role_fails_loud(gcs_bucket: gcs.Bucket) -> None:
    paper = _paper(
        files=[
            writer.FileInput(
                role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_UNSPECIFIED, name='d.csv', data=b'a,b\n1,2\n'
            )
        ]
    )
    with pytest.raises(ValueError, match='unknown role'):
        writer.write_paper(gcs_bucket, paper)


def test_blob_without_extension_fails_loud(gcs_bucket: gcs.Bucket) -> None:
    paper = _paper(
        files=[
            writer.FileInput(
                role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_FIGURE, name='noext', data=b'bytes'
            )
        ]
    )
    with pytest.raises(ValueError, match='extension'):
        writer.write_paper(gcs_bucket, paper)


def test_invalid_metadata_proto_fails_loud(gcs_bucket: gcs.Bucket) -> None:
    with pytest.raises(ValueError, match='not a valid PubmedArticle'):
        writer.write_paper(gcs_bucket, _paper(metadata=b'\x08'))


def test_metadata_is_written_verbatim(gcs_bucket: gcs.Bucket) -> None:
    metadata = _metadata(pmid='12345678')
    writer.write_paper(gcs_bucket, _paper(metadata=metadata))
    assert _read(gcs_bucket, posixpath.join('papers', _DOC_ID, 'metadata.pb')) == metadata
