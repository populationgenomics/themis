"""Tests for the directory + manifest writer (`themis.litcache.writer`).

Backed by a fake-gcs-server bucket (Docker-gated via the shared `gcs_bucket` fixture): the
writer works against a real `google.cloud.storage.Bucket`, so the tests exercise the same
content-addressed writes and create-if-absent (`if_generation_match=0`) manifest commit the
production path uses.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import posixpath

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs

from themis.litcache import metadata_fields, records, writer
from themis.litcache.models import litcache_pb2

_CREATED_AT = datetime.datetime(2026, 6, 25, tzinfo=datetime.UTC)
_CAPTURED_AT = datetime.datetime(2026, 6, 24, tzinfo=datetime.UTC)
_DOC_ID = '9f3a0000-0000-4000-8000-000000000001'

_PDF_BYTES = b'%PDF-1.7 fake pdf bytes'
_PDF_HASH = hashlib.sha256(_PDF_BYTES).hexdigest()
_MARKDOWN = '# Title\n\nBody.\n'
_MARKDOWN_HASH = hashlib.sha256(_MARKDOWN.encode('utf-8')).hexdigest()
_XML_BYTES = b'<article><body>Full text.</body></article>'
_XML_HASH = hashlib.sha256(_XML_BYTES).hexdigest()


def _metadata(pmid: str = '29089047') -> bytes:
    source = litcache_pb2.MetadataSource()
    source.pubmed.article.medline_citation.pmid.value = pmid
    metadata = litcache_pb2.SourcedMetadata(metadata=metadata_fields.fill(source), source=source)
    return metadata.SerializeToString()


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


def _xml_source(data: bytes = _XML_BYTES, *, handle: str = 'xml') -> writer.SourceInput:
    return writer.SourceInput(
        handle=handle,
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_XML,
        kind=litcache_pb2.SourceKind.SOURCE_KIND_EUROPE_PMC,
        data=data,
        licence='https://creativecommons.org/licenses/by/4.0/',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT,
        access=_access(),
        captured_at=_CAPTURED_AT,
        origin_url='https://europepmc.org/full-text.xml',
    )


def _rendering(
    *,
    from_revision: str = _PDF_HASH,
    converter: litcache_pb2.Converter = litcache_pb2.Converter.CONVERTER_DOCLING,
    model: str | None = None,
    markdown: str = _MARKDOWN,
    docling_json: bytes | None = None,
    created_at: datetime.datetime = _CREATED_AT,
) -> writer.RenderingInput:
    construction = litcache_pb2.RenderingConstruction(converter=converter, converter_version='2.0.0')
    if model is not None:
        construction.model = model
    return writer.RenderingInput(
        construction=construction,
        from_revision=from_revision,
        created_at=created_at,
        markdown=markdown,
        docling_json=docling_json,
    )


def _xml_rendering(from_revision: str = _XML_HASH, *, markdown: str = _MARKDOWN) -> writer.RenderingInput:
    return _rendering(
        from_revision=from_revision, converter=litcache_pb2.Converter.CONVERTER_LITDOWN, markdown=markdown
    )


def _paper(**overrides: object) -> writer.PaperInput:
    defaults: dict[str, object] = {
        'doc_id': _DOC_ID,
        'external_ids': litcache_pb2.ExternalIds(doi='10.1/abc'),
        'claim_key': 'doi:10.1/abc',
        'equivalence': litcache_pb2.Equivalence(edges=[], canonical_doc_id=_DOC_ID),
        'retraction': litcache_pb2.Retraction(),
        'sources': [_source()],
        'renderings': [_rendering(markdown=_MARKDOWN, docling_json=b'{"x": 1}')],
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
    # The rendering is a record keyed by the markdown's content hash, whose dep is the
    # source revision it rendered, and the markdown ref names it.
    assert list(manifest.records[_MARKDOWN_HASH].deps) == [_PDF_HASH]
    assert manifest.refs['markdown'] == _MARKDOWN_HASH


def test_a_crash_on_the_commit_leaves_no_manifest_and_no_record(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A crash on the manifest write leaves the content-addressed bodies but neither the
    # manifest nor the record, so a re-run treats the paper as uncached.
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
    assert not real_blob(posixpath.join(paper_dir, 'metadata.pb')).exists()
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


def _committed(bucket: gcs.Bucket) -> litcache_pb2.Manifest:
    return litcache_pb2.Manifest.FromString(_read(bucket, writer.manifest_path(_DOC_ID)))


def _generation(bucket: gcs.Bucket) -> int:
    blob = bucket.get_blob(writer.manifest_path(_DOC_ID))
    assert blob is not None
    assert blob.generation is not None
    return blob.generation


def test_a_writer_finding_its_paper_committed_adds_its_work(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())

    changed_bytes = b'DIFFERENT'
    changed_hash = hashlib.sha256(changed_bytes).hexdigest()
    second = writer.write_paper(
        gcs_bucket,
        _paper(
            sources=[_source(data=changed_bytes)],
            renderings=[_rendering(from_revision=changed_hash, markdown='# Another rendering\n')],
        ),
    )

    assert second.written is False
    manifest = _committed(gcs_bucket)
    assert {r.hash for r in manifest.sources[0].revisions} == {_PDF_HASH, changed_hash}
    assert len(manifest.records) == 2
    assert second.manifest == manifest


def _miss_the_skip_check(gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the manifest skip-check to miss, as a writer racing inside the exists()-then-write window does."""
    real_blob = gcs_bucket.blob

    def skip_check_misses(name: str) -> gcs.Blob:
        blob = real_blob(name)
        if name.endswith('manifest.pb'):
            monkeypatch.setattr(blob, 'exists', lambda: False)
        return blob

    monkeypatch.setattr(gcs_bucket, 'blob', skip_check_misses)


def test_a_writer_losing_the_commit_to_an_id_only_manifest_names_its_work(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An on-demand ingest commits an id-only manifest while a bulk writer converts the same paper; the bulk writer's
    # PDF and rendering would otherwise sit in the bucket with no manifest naming them.
    writer.write_paper(gcs_bucket, _paper(sources=[], renderings=[]))
    _miss_the_skip_check(gcs_bucket, monkeypatch)

    result = writer.write_paper(gcs_bucket, _paper())

    assert result.written is False
    manifest = _committed(gcs_bucket)
    assert [s.handle for s in manifest.sources] == ['pdf']
    assert manifest.refs[records.MARKDOWN_REF] == _MARKDOWN_HASH
    assert _read(gcs_bucket, writer.metadata_path(_DOC_ID)) == _metadata()  # the winner's record, kept


def test_a_writer_claiming_fewer_ids_than_the_manifest_adds_its_work(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer.write_paper(
        gcs_bucket,
        _paper(external_ids=litcache_pb2.ExternalIds(doi='10.1/abc', pmid='30000001'), sources=[], renderings=[]),
    )
    _miss_the_skip_check(gcs_bucket, monkeypatch)

    writer.write_paper(gcs_bucket, _paper())

    manifest = _committed(gcs_bucket)
    assert [s.handle for s in manifest.sources] == ['pdf']
    assert manifest.refs[records.MARKDOWN_REF] == _MARKDOWN_HASH
    assert _read(gcs_bucket, writer.metadata_path(_DOC_ID)) == _metadata()


def test_a_join_keeps_the_record_of_the_run_that_finished_last(gcs_bucket: gcs.Bucket) -> None:
    later = _CREATED_AT + datetime.timedelta(hours=1)
    writer.write_paper(gcs_bucket, _paper(renderings=[_rendering(created_at=later)]))

    writer.write_paper(gcs_bucket, _paper())  # an earlier run of the same conversion
    assert _committed(gcs_bucket).records[_MARKDOWN_HASH].created_at.ToDatetime(tzinfo=datetime.UTC) == later

    latest = later + datetime.timedelta(hours=1)
    writer.write_paper(gcs_bucket, _paper(renderings=[_rendering(created_at=latest)]))
    assert _committed(gcs_bucket).records[_MARKDOWN_HASH].created_at.ToDatetime(tzinfo=datetime.UTC) == latest


def test_a_joining_source_on_other_terms_starts_a_lineage_of_its_own(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    other_bytes = b'OTHER PDF'
    other_terms = dataclasses.replace(_source(data=other_bytes), licence='https://example.org/other-licence')

    writer.write_paper(
        gcs_bucket,
        _paper(
            sources=[other_terms],
            renderings=[_rendering(from_revision=hashlib.sha256(other_bytes).hexdigest(), markdown='# Other\n')],
        ),
    )

    assert [s.handle for s in _committed(gcs_bucket).sources] == ['pdf', 'pdf-2']
    other_path = f'sources/pdf-2/{hashlib.sha256(other_bytes).hexdigest()}.pdf'
    assert _read(gcs_bucket, posixpath.join(writer.paper_dir(_DOC_ID), other_path)) == other_bytes


def test_a_join_that_loses_its_generation_race_keeps_both_writers_work(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A conversion lands between the join's read and its write; the retry re-reads and keeps that rendering too.
    writer.write_paper(gcs_bucket, _paper(renderings=[]))
    real_merge = writer._merge_work
    raced: list[bool] = []

    def merge_after_a_concurrent_rendering(*args: object, **kwargs: object) -> None:
        if not raced:
            raced.append(True)
            writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(markdown='# Converted meanwhile\n'))
        real_merge(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(writer, '_merge_work', merge_after_a_concurrent_rendering)
    writer.write_paper(gcs_bucket, _paper())

    assert len(_committed(gcs_bucket).records) == 2


def test_a_writer_claiming_an_id_the_manifest_lacks_adds_nothing(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It may be another paper's seed that names this paper's DOI by mistake: its work is not this paper's.
    writer.write_paper(gcs_bucket, _paper(sources=[], renderings=[]))
    committed = _read(gcs_bucket, writer.manifest_path(_DOC_ID))
    _miss_the_skip_check(gcs_bucket, monkeypatch)

    other = _paper(external_ids=litcache_pb2.ExternalIds(doi='10.1/abc', pmid='30000001'))
    result = writer.write_paper(gcs_bucket, other)

    assert result.written is False
    assert _read(gcs_bucket, writer.manifest_path(_DOC_ID)) == committed


def test_a_lost_commit_race_writes_no_record(gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two seeds sharing an identifier claim one doc_id; the loser resolved another paper's
    # record, and it must not land beside the winner's manifest.
    writer.write_paper(gcs_bucket, _paper())
    gcs_bucket.blob(writer.metadata_path(_DOC_ID)).delete()  # the winner, between its commit and its record
    _miss_the_skip_check(gcs_bucket, monkeypatch)

    loser = _paper(
        external_ids=litcache_pb2.ExternalIds(doi='10.1/abc', pmid='30000001'), metadata=_metadata('30000001')
    )
    result = writer.write_paper(gcs_bucket, loser)

    assert result.written is False
    assert not gcs_bucket.blob(writer.metadata_path(_DOC_ID)).exists()


def test_a_commit_whose_response_was_lost_still_writes_the_record(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The client retries a commit whose response was lost; the retry fails the precondition
    # against the writer's own manifest, so the writer sees a lost race.
    writer.write_paper(gcs_bucket, _paper())
    gcs_bucket.blob(writer.metadata_path(_DOC_ID)).delete()
    _miss_the_skip_check(gcs_bucket, monkeypatch)

    generation = _generation(gcs_bucket)
    result = writer.write_paper(gcs_bucket, _paper())

    assert result.written is False
    assert _read(gcs_bucket, writer.metadata_path(_DOC_ID)) == _metadata()
    assert _generation(gcs_bucket) == generation  # its own work: nothing to add


def test_a_record_lost_after_the_commit_is_filled_by_the_same_paper(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    gcs_bucket.blob(writer.metadata_path(_DOC_ID)).delete()  # a crash between the commit and the record

    result = writer.write_paper(gcs_bucket, _paper())

    assert result.written is False
    assert _read(gcs_bucket, writer.metadata_path(_DOC_ID)) == _metadata()


def test_a_record_lost_after_the_commit_is_not_filled_by_another_paper(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    gcs_bucket.blob(writer.metadata_path(_DOC_ID)).delete()

    other = _paper(
        external_ids=litcache_pb2.ExternalIds(doi='10.1/abc', pmid='30000001'), metadata=_metadata('30000001')
    )
    writer.write_paper(gcs_bucket, other)

    assert not gcs_bucket.blob(writer.metadata_path(_DOC_ID)).exists()


def test_the_fill_keeps_a_present_record(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())

    writer.write_paper(gcs_bucket, _paper(metadata=_metadata('30000001')))

    assert _read(gcs_bucket, writer.metadata_path(_DOC_ID)) == _metadata()


def test_the_fill_rejects_a_manifest_naming_another_doc_id(gcs_bucket: gcs.Bucket) -> None:
    manifest = litcache_pb2.Manifest(doc_id='another-doc-id', external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'))
    with pytest.raises(ValueError, match='another-doc-id'):
        writer.fill_absent_metadata(gcs_bucket, _DOC_ID, manifest, manifest.external_ids, _metadata())


def test_content_addresses_a_fetched_blob_and_records_unfetched_files(gcs_bucket: gcs.Bucket) -> None:
    fig_bytes = b'\xff\xd8\xff fake jpeg'
    paper = _paper(
        files=[
            writer.FileInput(
                role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_FIGURE,
                name='fig1.jpg',
                source_url='https://x/fig1.jpg',
                data=fig_bytes,
                captured_at=_CAPTURED_AT,
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
    # A fetched file records the hash a record's dep can name, and when it was fetched.
    assert files['fig1.jpg'].hash == digest
    assert files['fig1.jpg'].captured_at.ToDatetime(datetime.UTC) == _CAPTURED_AT
    assert not files['fig2.jpg'].HasField('path')  # un-fetched (lazy fetch)
    assert not files['fig2.jpg'].hash


def test_a_rendering_of_a_revision_no_lineage_holds_fails_loud(gcs_bucket: gcs.Bucket) -> None:
    paper = _paper(renderings=[_rendering(from_revision='0' * 64, markdown=_MARKDOWN)])
    with pytest.raises(ValueError, match='deps_resolve'):
        writer.write_paper(gcs_bucket, paper)
    assert not gcs_bucket.blob(posixpath.join('papers', _DOC_ID, 'manifest.pb')).exists()


def test_model_inconsistent_with_converter_fails_loud(gcs_bucket: gcs.Bucket) -> None:
    # docling carries no model; supplying one is inconsistent.
    paper = _paper(renderings=[_rendering(model='claude-opus-4-8', markdown=_MARKDOWN)])
    with pytest.raises(ValueError, match='model'):
        writer.write_paper(gcs_bucket, paper)


def test_two_renderings_with_the_same_markdown_hash_fail_loud(gcs_bucket: gcs.Bucket) -> None:
    paper = _paper(
        renderings=[
            _rendering(markdown=_MARKDOWN),
            _rendering(markdown=_MARKDOWN),
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
    with pytest.raises(ValueError, match='not a valid SourcedMetadata'):
        writer.write_paper(gcs_bucket, _paper(metadata=b'\x08'))


def test_metadata_with_no_record_fails_loud(gcs_bucket: gcs.Bucket) -> None:
    # A source record with no index's record is a corrupt blob, never a paper without metadata.
    sourced = litcache_pb2.SourcedMetadata(metadata=litcache_pb2.Metadata(), source=litcache_pb2.MetadataSource())
    with pytest.raises(ValueError, match='no index record set'):
        writer.write_paper(gcs_bucket, _paper(metadata=sourced.SerializeToString()))


def test_metadata_is_written_verbatim(gcs_bucket: gcs.Bucket) -> None:
    metadata = _metadata(pmid='12345678')
    writer.write_paper(gcs_bucket, _paper(metadata=metadata))
    assert _read(gcs_bucket, posixpath.join('papers', _DOC_ID, 'metadata.pb')) == metadata


def test_invalid_metadata_leaves_no_artifact_behind(gcs_bucket: gcs.Bucket) -> None:
    # The metadata check runs before anything is written, so a paper it rejects has no directory.
    with pytest.raises(ValueError, match='not a valid SourcedMetadata'):
        writer.write_paper(gcs_bucket, _paper(metadata=b'\x08'))
    assert not list(gcs_bucket.list_blobs(prefix=writer.paper_dir(_DOC_ID)))


def test_write_metadata_replaces_a_committed_papers_record(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper(metadata=_metadata(pmid='1')))
    manifest_before = _read(gcs_bucket, writer.manifest_path(_DOC_ID))

    writer.write_metadata(gcs_bucket, _DOC_ID, _metadata(pmid='2'))

    assert _read(gcs_bucket, writer.metadata_path(_DOC_ID)) == _metadata(pmid='2')
    # The manifest neither names nor hashes the record, so the commit is untouched.
    assert _read(gcs_bucket, writer.manifest_path(_DOC_ID)) == manifest_before


def test_write_metadata_recreates_a_deleted_record(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    gcs_bucket.blob(writer.metadata_path(_DOC_ID)).delete()

    writer.write_metadata(gcs_bucket, _DOC_ID, _metadata(pmid='2'))

    assert _read(gcs_bucket, writer.metadata_path(_DOC_ID)) == _metadata(pmid='2')


def test_write_metadata_rejects_an_invalid_record(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    with pytest.raises(ValueError, match='not a valid SourcedMetadata'):
        writer.write_metadata(gcs_bucket, _DOC_ID, b'\x08')
    assert _read(gcs_bucket, writer.metadata_path(_DOC_ID)) == _metadata()


_MD2 = '# Title\n\nA second rendering.\n'
_MD2_HASH = hashlib.sha256(_MD2.encode('utf-8')).hexdigest()


def _load_manifest(bucket: gcs.Bucket, doc_id: str = _DOC_ID) -> litcache_pb2.Manifest:
    return litcache_pb2.Manifest.FromString(_read(bucket, writer.manifest_path(doc_id)))


def test_add_rendering_appends_to_an_existing_manifest(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    result = writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(markdown=_MD2))
    assert result == writer.AddRenderingResult(hash=_MD2_HASH, added=True, generation=_generation(gcs_bucket))
    manifest = _load_manifest(gcs_bucket)
    # The original rendering survives; the new one is added, keyed by its markdown hash.
    assert set(manifest.records) == {_MARKDOWN_HASH, _MD2_HASH}
    assert _read(gcs_bucket, posixpath.join('papers', _DOC_ID, f'renderings/{_MD2_HASH}.md')) == _MD2.encode('utf-8')


def test_add_rendering_is_idempotent_on_a_repeated_hash(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    # Re-adding the rendering the paper already carries (same markdown) is a no-op.
    result = writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(markdown=_MARKDOWN))
    assert result == writer.AddRenderingResult(hash=_MARKDOWN_HASH, added=False, generation=_generation(gcs_bucket))
    assert set(_load_manifest(gcs_bucket).records) == {_MARKDOWN_HASH}


def test_add_rendering_to_a_missing_paper_raises_not_found(gcs_bucket: gcs.Bucket) -> None:
    with pytest.raises(api_exceptions.NotFound):
        writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(markdown=_MD2))


def test_add_rendering_of_a_revision_no_lineage_holds_fails_loud(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    with pytest.raises(ValueError, match='deps_resolve'):
        writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(from_revision='0' * 64, markdown=_MD2))


def test_add_source_and_rendering_adds_a_new_lineage_and_rendering(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    result = writer.add_source_and_rendering(gcs_bucket, _DOC_ID, _xml_source(), _xml_rendering(markdown=_MD2))
    assert result == writer.AddRenderingResult(hash=_MD2_HASH, added=True, generation=_generation(gcs_bucket))
    manifest = _load_manifest(gcs_bucket)
    # The seed pdf lineage survives; the fetched xml lineage is added alongside it.
    assert {s.handle for s in manifest.sources} == {'pdf', 'xml'}
    xml = next(s for s in manifest.sources if s.handle == 'xml')
    assert [r.hash for r in xml.revisions] == [_XML_HASH]
    assert set(manifest.records) == {_MARKDOWN_HASH, _MD2_HASH}
    assert _read(gcs_bucket, posixpath.join('papers', _DOC_ID, f'sources/xml/{_XML_HASH}.xml')) == _XML_BYTES
    assert _read(gcs_bucket, posixpath.join('papers', _DOC_ID, f'renderings/{_MD2_HASH}.md')) == _MD2.encode('utf-8')


def test_add_source_and_rendering_is_idempotent_on_re_add(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    rendering = _xml_rendering(markdown=_MD2)
    first = writer.add_source_and_rendering(gcs_bucket, _DOC_ID, _xml_source(), rendering)
    second = writer.add_source_and_rendering(gcs_bucket, _DOC_ID, _xml_source(), rendering)
    assert first.added is True
    assert second == writer.AddRenderingResult(hash=_MD2_HASH, added=False, generation=first.generation)
    manifest = _load_manifest(gcs_bucket)
    xml = next(s for s in manifest.sources if s.handle == 'xml')
    # A re-delivery duplicates neither the source revision nor the rendering.
    assert [r.hash for r in xml.revisions] == [_XML_HASH]
    assert set(manifest.records) == {_MARKDOWN_HASH, _MD2_HASH}


def test_add_source_and_rendering_appends_a_revision_to_an_existing_lineage(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    writer.add_source_and_rendering(gcs_bucket, _DOC_ID, _xml_source(), _xml_rendering(markdown=_MD2))
    xml2 = b'<article><body>Revised full text.</body></article>'
    xml2_hash = hashlib.sha256(xml2).hexdigest()
    md3 = '# Title\n\nRevised body.\n'
    md3_hash = hashlib.sha256(md3.encode('utf-8')).hexdigest()
    result = writer.add_source_and_rendering(
        gcs_bucket,
        _DOC_ID,
        _xml_source(data=xml2),
        _xml_rendering(from_revision=xml2_hash, markdown=md3),
    )
    assert result == writer.AddRenderingResult(hash=md3_hash, added=True, generation=_generation(gcs_bucket))
    manifest = _load_manifest(gcs_bucket)
    xmls = [s for s in manifest.sources if s.handle == 'xml']
    # A second capture of the same lineage appends a revision — it does not fork a new source.
    assert len(xmls) == 1
    assert [r.hash for r in xmls[0].revisions] == [_XML_HASH, xml2_hash]
    assert set(manifest.records) == {_MARKDOWN_HASH, _MD2_HASH, md3_hash}


def test_add_source_and_rendering_to_a_missing_paper_raises_not_found(gcs_bucket: gcs.Bucket) -> None:
    with pytest.raises(api_exceptions.NotFound):
        writer.add_source_and_rendering(gcs_bucket, _DOC_ID, _xml_source(), _xml_rendering(markdown=_MD2))


def test_add_source_and_rendering_with_a_mismatched_revision_fails_loud(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    with pytest.raises(ValueError, match='deps_resolve'):
        writer.add_source_and_rendering(
            gcs_bucket,
            _DOC_ID,
            _xml_source(),
            _xml_rendering(from_revision='deadbeef', markdown=_MD2),
        )


def test_add_rendering_retries_and_keeps_a_concurrent_rendering(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The load-bearing CAS claim: a rendering a concurrent writer commits between this RMW's read and
    # write is not clobbered. Fire one competing add_rendering during the first reload so the ensuing
    # generation-matched read then conflicts; the loop must re-read and both renderings must survive.
    writer.write_paper(gcs_bucket, _paper(renderings=[]))
    original_reload = gcs.Blob.reload
    fired: list[int] = []

    def racing_reload(self: gcs.Blob) -> None:
        original_reload(self)
        if self.name and self.name.endswith('manifest.pb') and not fired:
            fired.append(1)
            writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(markdown=_MD2))

    monkeypatch.setattr(gcs.Blob, 'reload', racing_reload)
    result = writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(markdown=_MARKDOWN))

    assert result.added is True
    assert {_MARKDOWN_HASH, _MD2_HASH} <= set(_load_manifest(gcs_bucket).records)  # neither is lost


def test_add_rendering_keeps_a_rendering_committed_between_its_read_and_its_write(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The write loses the generation race, so the retry has to read the manifest's current generation, not the one
    # its first attempt read.
    writer.write_paper(gcs_bucket, _paper(renderings=[]))
    real_put = writer._put_rendering_record
    fired: list[int] = []

    def put_after_a_concurrent_rendering(
        manifest: litcache_pb2.Manifest, key: str, rendering: writer.RenderingInput
    ) -> bool:
        if not fired:
            fired.append(1)
            writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(markdown=_MD2))
        return real_put(manifest, key, rendering)

    monkeypatch.setattr(writer, '_put_rendering_record', put_after_a_concurrent_rendering)
    writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(markdown=_MARKDOWN))

    assert {_MARKDOWN_HASH, _MD2_HASH} <= set(_load_manifest(gcs_bucket).records)


def test_add_rendering_raises_when_the_rmw_never_converges(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every generation-matched read conflicts → the RMW exhausts its budget and fails loud rather than
    # silently dropping the rendering (exercises the retry-guarded read for the reload/read race).
    writer.write_paper(gcs_bucket, _paper(renderings=[]))
    original_download = gcs.Blob.download_as_bytes

    def always_conflict(self: gcs.Blob, **kwargs: object) -> bytes:
        if 'if_generation_match' in kwargs:  # the RMW's generation-matched read — the only one here
            raise api_exceptions.PreconditionFailed('a concurrent writer keeps winning')
        return original_download(self)

    monkeypatch.setattr(gcs.Blob, 'download_as_bytes', always_conflict)
    with pytest.raises(writer.ConcurrentWriteError, match='did not converge'):
        writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(markdown=_MD2))


def test_merge_source_rejects_a_media_type_mismatch() -> None:
    # A handle whose media_type disagrees with the manifest's would dangle the revision blob (its path
    # extension is media_type-derived), so the merge fails loud rather than committing a bad reference.
    manifest = litcache_pb2.Manifest(
        sources=[litcache_pb2.Source(handle='doc', media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF)]
    )
    incoming = litcache_pb2.Source(handle='doc', media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_XML)
    with pytest.raises(ValueError, match='in the manifest'):
        writer._merge_source(manifest, incoming)


def test_a_fetched_file_without_a_capture_time_fails_loud(gcs_bucket: gcs.Bucket) -> None:
    paper = _paper(
        files=[
            writer.FileInput(
                role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_SUPPLEMENTARY, name='mmc1.xlsx', data=b'PK'
            )
        ]
    )
    with pytest.raises(ValueError, match='capture time'):
        writer.write_paper(gcs_bucket, paper)


def test_write_paper_points_the_markdown_ref_at_the_best_rendering(gcs_bucket: gcs.Bucket) -> None:
    # A litdown rendering of the XML outranks a docling rendering of the PDF, whichever is listed first.
    paper = _paper(
        sources=[_source(), _xml_source()],
        renderings=[_rendering(markdown=_MARKDOWN), _xml_rendering(markdown=_MD2)],
    )
    manifest = writer.write_paper(gcs_bucket, paper).manifest
    assert set(manifest.records) == {_MARKDOWN_HASH, _MD2_HASH}
    assert manifest.refs['markdown'] == _MD2_HASH


def test_add_rendering_moves_the_markdown_ref_only_to_a_better_rendering(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())  # one docling rendering
    llm = _rendering(converter=litcache_pb2.Converter.CONVERTER_LLM_OCR, model='claude-sonnet-5', markdown=_MD2)
    writer.add_rendering(gcs_bucket, _DOC_ID, llm)
    assert _load_manifest(gcs_bucket).refs['markdown'] == _MD2_HASH  # llm-ocr outranks docling

    worse = _rendering(markdown='# A later docling rendering\n', created_at=_CREATED_AT + datetime.timedelta(days=1))
    writer.add_rendering(gcs_bucket, _DOC_ID, worse)
    manifest = _load_manifest(gcs_bucket)
    assert len(manifest.records) == 3
    assert manifest.refs['markdown'] == _MD2_HASH  # a newer docling rendering still ranks below llm-ocr


def test_a_revision_on_other_terms_lands_in_a_numbered_lineage(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    second = b'%PDF-1.7 a second pdf'
    other_terms = dataclasses.replace(
        _source(data=second), access=litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess())
    )
    rendering = _rendering(from_revision=hashlib.sha256(second).hexdigest(), markdown=_MD2)
    writer.add_source_and_rendering(gcs_bucket, _DOC_ID, other_terms, rendering)
    sources = {s.handle: s for s in _load_manifest(gcs_bucket).sources}
    assert [r.hash for r in sources['pdf'].revisions] == [_PDF_HASH]  # the held lineage keeps only its own terms
    assert sources['pdf-2'].access.WhichOneof('kind') == 'unknown'
    assert [r.hash for r in sources['pdf-2'].revisions] == [hashlib.sha256(second).hexdigest()]
    stored = posixpath.join('papers', _DOC_ID, f'sources/pdf-2/{hashlib.sha256(second).hexdigest()}.pdf')
    assert _read(gcs_bucket, stored) == second


def test_a_redelivered_fetch_adds_nothing(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    first = writer.add_source_and_rendering(gcs_bucket, _DOC_ID, _source(), _rendering(markdown=_MD2))
    again = writer.add_source_and_rendering(gcs_bucket, _DOC_ID, _source(), _rendering(markdown=_MD2))
    assert (first.added, again.added) == (True, False)
    assert [s.handle for s in _load_manifest(gcs_bucket).sources] == ['pdf']


def test_a_rendering_produced_again_takes_the_latest_runs_record(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    later = _CREATED_AT + datetime.timedelta(days=3)
    result = writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(markdown=_MARKDOWN, created_at=later))
    assert not result.added  # the key was already held
    record = _load_manifest(gcs_bucket).records[_MARKDOWN_HASH]
    assert record.created_at.ToDatetime(datetime.UTC) == later


def test_each_commit_reports_the_generation_it_leaves_the_manifest_at(gcs_bucket: gcs.Bucket) -> None:
    created = writer.write_paper(gcs_bucket, _paper(renderings=[]))
    assert created.generation == _generation(gcs_bucket)

    rendered = writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(markdown=_MD2))
    assert rendered.generation == _generation(gcs_bucket)
    assert rendered.generation != created.generation

    joined = writer.write_paper(gcs_bucket, _paper(renderings=[]))
    assert joined == writer.WriteResult(manifest=_committed(gcs_bucket), written=False, generation=rendered.generation)


def test_a_commit_whose_response_was_lost_reports_the_generation_it_wrote(
    gcs_bucket: gcs.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The write lands but its response is lost, so the client's retry meets its own generation and answers 412; the
    # re-read finds the change already made.
    writer.write_paper(gcs_bucket, _paper(renderings=[]))
    real_upload = gcs.Blob.upload_from_string
    lost: list[int] = []

    def upload_losing_the_response(self: gcs.Blob, data: bytes, *, if_generation_match: int | None = None) -> None:
        if not self.name or not self.name.endswith('manifest.pb') or lost:
            real_upload(self, data, if_generation_match=if_generation_match)
            return
        # Through another handle: a real 412 leaves this one holding the generation it read.
        real_upload(gcs_bucket.blob(self.name), data, if_generation_match=if_generation_match)
        lost.append(1)
        raise api_exceptions.PreconditionFailed('the retry of a write that landed')

    monkeypatch.setattr(gcs.Blob, 'upload_from_string', upload_losing_the_response)
    result = writer.add_rendering(gcs_bucket, _DOC_ID, _rendering(markdown=_MD2))

    assert result.generation == _generation(gcs_bucket)
    assert _MD2_HASH in _load_manifest(gcs_bucket).records


def test_a_change_that_alters_nothing_writes_nothing(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    before = _generation(gcs_bucket)

    result = writer.commit(gcs_bucket, _DOC_ID, lambda _manifest: False)

    assert result.changed is False
    assert result.generation == before == _generation(gcs_bucket)
    assert result.manifest == result.prior == _committed(gcs_bucket)


def test_a_change_the_manifest_refuses_leaves_it_as_it_was(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    before = _committed(gcs_bucket)

    def break_a_ref(manifest: litcache_pb2.Manifest) -> bool:
        manifest.refs[records.MARKDOWN_REF] = '0' * 64
        return True

    with pytest.raises(writer.ManifestRefusedError):
        writer.commit(gcs_bucket, _DOC_ID, break_a_ref)
    assert _committed(gcs_bucket) == before


def test_a_commit_refuses_a_manifest_naming_another_doc_id(gcs_bucket: gcs.Bucket) -> None:
    other = litcache_pb2.Manifest(doc_id='9f3a0000-0000-4000-8000-000000000002')
    gcs_bucket.blob(writer.manifest_path(_DOC_ID)).upload_from_string(other.SerializeToString())

    with pytest.raises(writer.ManifestRefusedError, match='names doc_id'):
        writer.commit(gcs_bucket, _DOC_ID, lambda _manifest: True)


def test_add_source_adds_a_lineage_with_no_rendering(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper(sources=[], renderings=[]))

    result = writer.add_source(gcs_bucket, _DOC_ID, _source())

    assert result == writer.AddSourceResult(
        handle='pdf', hash=_PDF_HASH, added=True, generation=_generation(gcs_bucket)
    )
    manifest = _committed(gcs_bucket)
    assert [(s.handle, [r.hash for r in s.revisions]) for s in manifest.sources] == [('pdf', [_PDF_HASH])]
    assert not manifest.records
    assert _read(gcs_bucket, posixpath.join(writer.paper_dir(_DOC_ID), _pdf_path())) == _PDF_BYTES


def test_add_source_of_bytes_the_lineage_holds_adds_nothing(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    before = _generation(gcs_bucket)

    result = writer.add_source(gcs_bucket, _DOC_ID, _source())

    assert result == writer.AddSourceResult(handle='pdf', hash=_PDF_HASH, added=False, generation=before)


def test_add_source_on_other_terms_names_the_lineage_it_joined(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    other_bytes = b'%PDF-1.7 a deposited copy'
    other_terms = dataclasses.replace(_source(data=other_bytes), licence='all rights reserved')

    result = writer.add_source(gcs_bucket, _DOC_ID, other_terms)

    assert (result.handle, result.hash, result.added) == ('pdf-2', hashlib.sha256(other_bytes).hexdigest(), True)
    assert [s.handle for s in _committed(gcs_bucket).sources] == ['pdf', 'pdf-2']


def test_add_source_of_held_bytes_on_other_terms_adds_nothing(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())
    before = _generation(gcs_bucket)
    held_on_other_terms = dataclasses.replace(_source(), licence='all rights reserved')

    result = writer.add_source(gcs_bucket, _DOC_ID, held_on_other_terms)

    assert result == writer.AddSourceResult(handle='pdf', hash=_PDF_HASH, added=False, generation=before)
    assert [s.handle for s in _committed(gcs_bucket).sources] == ['pdf']


def test_add_source_to_a_missing_paper_raises_not_found(gcs_bucket: gcs.Bucket) -> None:
    with pytest.raises(api_exceptions.NotFound):
        writer.add_source(gcs_bucket, _DOC_ID, _source())


def _supplement(data: bytes = b'col\n1\n', name: str = 'table_s1.csv') -> writer.FileInput:
    return writer.FileInput(
        role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_SUPPLEMENTARY,
        name=name,
        source_url='https://pmc.example/s1.csv',
        data=data,
        captured_at=_CAPTURED_AT,
    )


def test_add_files_lists_each_file_once(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())

    first = writer.add_files(gcs_bucket, _DOC_ID, [_supplement(), _supplement(b'x', 'figure.png')])
    again = writer.add_files(gcs_bucket, _DOC_ID, [_supplement()])

    assert (first.added, again.added) == (2, 0)
    assert again.generation == first.generation == _generation(gcs_bucket)
    files = _committed(gcs_bucket).files
    assert sorted(f.name for f in files) == ['figure.png', 'table_s1.csv']
    entry = next(f for f in files if f.name == 'table_s1.csv')
    assert _read(gcs_bucket, posixpath.join(writer.paper_dir(_DOC_ID), entry.path)) == b'col\n1\n'


def test_add_files_refuses_a_file_it_cannot_place(gcs_bucket: gcs.Bucket) -> None:
    writer.write_paper(gcs_bucket, _paper())

    with pytest.raises(writer.ManifestRefusedError, match='extension'):
        writer.add_files(gcs_bucket, _DOC_ID, [_supplement(name='table_s1')])
