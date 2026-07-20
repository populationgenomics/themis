"""Write a paper directory and commit it with the manifest.

litcache owns placement: given a resolved identity, the paper's source lineages
and renderings (each carrying its bytes), the bibliographic metadata, and the
known associated files, this module writes the per-paper GCS layout and computes
the manifest's paths and hashes. The caller (the per-paper pipeline) supplies
byte-bearing inputs and the already-built `Rendering` records from the converter
branch; the writer content-addresses every blob, hashes source bytes, and
assembles the `Manifest` (see `docs/design/litcache-manifest.md`).

Layout written under `papers/{doc_id}/`:

    manifest.pb                         # the commit, written last
    metadata.pb                         # bibliographic (pubmed_proto PubmedArticle)
    sources/{handle}/{hex}.{ext}        # raw source bytes, content-addressed
    renderings/{hex}.md                 # rendering markdown, keyed by its hash
    renderings/{hex}.docling.json       # structured docling output (converter=docling)
    figures/{hash}.{ext}                # content-addressed blobs
    supplementary/{hash}.{ext}

The manifest write is the commit point: everything else is written first, then
the manifest. A crash before the manifest leaves no manifest, so a re-run sees
the paper as uncached and re-completes it, reusing the claimed `doc_id`.
Content-addressed writes are idempotent (identical bytes map to one name, and a
GCS upload is atomic), so a re-put is a no-op. `write_paper` skips a paper whose
manifest already exists; that manifest is the resumability checkpoint, not the
crosswalk row. The commit itself is create-only (`if_generation_match=0`), so if two
workers race past the skip check the first to commit wins and the loser adopts it.
"""

from __future__ import annotations

import dataclasses
import datetime
import posixpath
from collections.abc import Sequence

from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from google.protobuf import message, timestamp_pb2
from pubmed_proto import pubmed_pb2

from themis.common import storage
from themis.litcache.models import litcache_pb2

_PAPERS_PREFIX = 'papers'
_MANIFEST_NAME = 'manifest.pb'
_METADATA_NAME = 'metadata.pb'
_SOURCES_DIR = 'sources'
_RENDERINGS_DIR = 'renderings'

# media_type -> the on-disk extension the source bytes are content-addressed under.
_SOURCE_EXTENSIONS: dict[litcache_pb2.SourceFormat, str] = {
    litcache_pb2.SourceFormat.SOURCE_FORMAT_XML: 'xml',
    litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF: 'pdf',
}

# A blob's content-addressed bytes live under a role-derived subdirectory; the
# manifest stores the role + relative path, so a blob with an unknown role cannot
# be placed and fails loud.
_BLOB_DIRS: dict[litcache_pb2.AssociatedFileRole, str] = {
    litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_FIGURE: 'figures',
    litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_SUPPLEMENTARY: 'supplementary',
}


def manifest_path(doc_id: str) -> str:
    """The manifest key for `doc_id` — the commit point and resumability checkpoint.

    A caller probes this to skip an already-committed paper before doing the
    expensive conversion work `write_paper` would redo.
    """
    return posixpath.join(_PAPERS_PREFIX, doc_id, _MANIFEST_NAME)


@dataclasses.dataclass(frozen=True)
class SourceInput:
    """A primary-artifact lineage plus its single captured revision's bytes.

    A fresh write captures one revision per lineage; re-ingestion (appending a
    revision to an existing lineage) is a separate path that rewrites the manifest
    rather than calling `write_paper`. Licence/access describe this lineage (they
    vary between a CC-BY xml and a restricted pdf, stable across the lineage's
    revisions).

    Attributes:
        handle: Lineage identity, stable across updates (`pdf`, `xml`, …) — the
            manifest's `Source.handle` and a rendering's `from_source`.
        media_type: `SOURCE_FORMAT_XML` or `SOURCE_FORMAT_PDF`; also selects the
            on-disk file extension.
        kind: Provenance of the revision's bytes (`SourceKind`).
        data: The raw source bytes.
        licence: Raw licence string as litfetch returned it (not an SPDX id).
        licence_basis: `LICENCE_BASIS_ARTIFACT` or `LICENCE_BASIS_ASSERTED`.
        access: The `Access` oneof.
        captured_at: When the revision's bytes were captured.
        origin_url: External provenance (an OA fetch URL); omitted for seed/upload.
        has_text_layer: pdf only — whether positioned characters are recoverable;
            omitted when the xml is the source of truth.
    """

    handle: str
    media_type: litcache_pb2.SourceFormat
    kind: litcache_pb2.SourceKind
    data: bytes
    licence: str
    licence_basis: litcache_pb2.LicenceBasis
    access: litcache_pb2.Access
    captured_at: datetime.datetime
    origin_url: str | None = None
    has_text_layer: bool | None = None


@dataclasses.dataclass(frozen=True)
class RenderingInput:
    """A rendering record plus the markdown (and optional docling json) bytes.

    Attributes:
        rendering: The `Rendering` from the converter branch, carrying
            `from_source` (a lineage handle), `from_revision` (the source byte
            hash it rendered), `converter`, and `converter_version`. The writer
            verifies `from_source`/`from_revision` against the sources it wrote
            and keys the rendering by the markdown's content hash.
        markdown: The rendered markdown text.
        docling_json: The structured DoclingDocument json, written alongside the
            markdown when the converter is docling; None otherwise.
    """

    rendering: litcache_pb2.Rendering
    markdown: str
    docling_json: bytes | None = None


@dataclasses.dataclass(frozen=True)
class FileInput:
    """A known associated file, fetched (bytes present) or not.

    Attributes:
        role: `figure` or `supplementary`; selects the blob subdirectory when
            `data` is present.
        name: The file's original name; its extension keys the content-addressed
            blob path.
        source_url: Where the file can be fetched from.
        data: The blob bytes when fetched; None for a known-but-un-fetched file
            (lazy fetch), whose manifest entry has `path` unset.
    """

    role: litcache_pb2.AssociatedFileRole
    name: str
    source_url: str | None = None
    data: bytes | None = None


@dataclasses.dataclass(frozen=True)
class PaperInput:
    """Everything needed to write and commit one paper directory.

    `external_ids`, `claim_key`, `equivalence`, and `retraction` come from
    identity and the crosswalk mapped into the manifest shape; `metadata` is the
    bibliographic `metadata.pb` bytes (a serialized pubmed_proto `PubmedArticle`).
    The writer adds only placement: content-addressed paths and hashes.
    """

    doc_id: str
    external_ids: litcache_pb2.ExternalIds
    claim_key: str
    equivalence: litcache_pb2.Equivalence
    retraction: litcache_pb2.Retraction
    sources: Sequence[SourceInput]
    renderings: Sequence[RenderingInput]
    metadata: bytes
    files: Sequence[FileInput] = ()


@dataclasses.dataclass(frozen=True)
class WriteResult:
    """The outcome of `write_paper`.

    Attributes:
        manifest: The committed manifest — assembled when newly written, loaded
            from GCS when the paper was already cached.
        written: True when this call wrote the paper; False when it skipped an
            already-committed paper.
    """

    manifest: litcache_pb2.Manifest
    written: bool


def write_paper(bucket: gcs.Bucket, paper: PaperInput) -> WriteResult:
    """Write `paper`'s directory and commit it with the manifest.

    Skips (and returns the existing manifest) when the paper's manifest already
    exists. Otherwise writes the sources, renderings, metadata, and blobs, then
    writes the manifest last as the commit.

    Args:
        bucket: The cache bucket to write into.
        paper: The fully-resolved paper inputs (identity, source lineages with
            bytes, renderings, metadata, associated files).

    Returns:
        A `WriteResult`: the manifest and whether this call wrote it.

    Raises:
        ValueError: On an inconsistency the writer refuses to commit — a rendering
            whose `from_source`/`from_revision` names no source or revision
            present, a rendering carrying (or missing) `model` against its
            converter, two renderings with the same markdown hash, a source with
            an unknown media type, a blob with an unknown role or a name without an
            extension, or `metadata` that is not a valid `PubmedArticle` proto.
    """
    paper_dir = posixpath.join(_PAPERS_PREFIX, paper.doc_id)
    manifest_key = manifest_path(paper.doc_id)
    manifest_blob = bucket.blob(manifest_key)
    if manifest_blob.exists():
        existing = litcache_pb2.Manifest.FromString(manifest_blob.download_as_bytes())
        return WriteResult(manifest=existing, written=False)

    _validate_metadata(paper.metadata)

    sources = [_write_source(bucket, paper_dir, s) for s in paper.sources]
    revision_hashes = {src.handle: {rev.hash for rev in src.revisions} for src in sources}
    renderings = _write_renderings(bucket, paper_dir, revision_hashes, paper.renderings)
    files = _write_files(bucket, paper_dir, paper.files)

    bucket.blob(posixpath.join(paper_dir, _METADATA_NAME)).upload_from_string(paper.metadata)

    manifest = litcache_pb2.Manifest(
        doc_id=paper.doc_id,
        external_ids=paper.external_ids,
        claim_key=paper.claim_key,
        equivalence=paper.equivalence,
        retraction=paper.retraction,
        sources=sources,
        renderings=renderings,
        files=files,
    )
    try:
        # if_generation_match=0 (create-only): the first writer to commit this doc_id
        # wins, closing the exists()-then-write race between concurrent workers.
        manifest_blob.upload_from_string(manifest.SerializeToString(), if_generation_match=0)
    except api_exceptions.PreconditionFailed:
        existing = litcache_pb2.Manifest.FromString(bucket.blob(manifest_key).download_as_bytes())
        return WriteResult(manifest=existing, written=False)
    return WriteResult(manifest=manifest, written=True)


def _write_source(bucket: gcs.Bucket, paper_dir: str, src: SourceInput) -> litcache_pb2.Source:
    ext = _SOURCE_EXTENSIONS.get(src.media_type)
    if ext is None:
        raise ValueError(f'source {src.handle!r} has unknown media type {src.media_type!r}')
    name = storage.put_content_addressed(bucket, src.data, posixpath.join(paper_dir, _SOURCES_DIR, src.handle), ext)
    rev_hash = posixpath.splitext(posixpath.basename(name))[0]

    captured_at = timestamp_pb2.Timestamp()
    captured_at.FromDatetime(src.captured_at)
    revision = litcache_pb2.Revision(hash=rev_hash, kind=src.kind, captured_at=captured_at)
    if src.origin_url is not None:
        revision.origin_url = src.origin_url
    if src.has_text_layer is not None:
        revision.has_text_layer = src.has_text_layer
    return litcache_pb2.Source(
        handle=src.handle,
        media_type=src.media_type,
        licence=src.licence,
        licence_basis=src.licence_basis,
        access=src.access,
        revisions=[revision],
    )


def _write_renderings(
    bucket: gcs.Bucket,
    paper_dir: str,
    revision_hashes: dict[str, set[str]],
    rins: Sequence[RenderingInput],
) -> dict[str, litcache_pb2.Rendering]:
    renderings: dict[str, litcache_pb2.Rendering] = {}
    for rin in rins:
        r = rin.rendering
        if r.from_source not in revision_hashes:
            raise ValueError(f'rendering from_source {r.from_source!r} names no source')
        if r.from_revision not in revision_hashes[r.from_source]:
            raise ValueError(
                f'rendering from_revision {r.from_revision!r} names no revision of source {r.from_source!r}'
            )
        # model identifies the LLM iff the converter is model-driven (llm-ocr).
        if (r.converter == litcache_pb2.Converter.CONVERTER_LLM_OCR) != r.HasField('model'):
            raise ValueError(f'rendering converter {r.converter!r} and model presence are inconsistent')

        markdown_bytes = rin.markdown.encode('utf-8')
        name = storage.put_content_addressed(bucket, markdown_bytes, posixpath.join(paper_dir, _RENDERINGS_DIR), 'md')
        key = posixpath.splitext(posixpath.basename(name))[0]
        if key in renderings:
            raise ValueError(f'two renderings share the markdown hash {key}')
        if rin.docling_json is not None:
            docling_name = posixpath.join(paper_dir, _RENDERINGS_DIR, f'{key}.docling.json')
            bucket.blob(docling_name).upload_from_string(rin.docling_json)
        renderings[key] = r
    return renderings


def _write_files(bucket: gcs.Bucket, paper_dir: str, files: Sequence[FileInput]) -> list[litcache_pb2.AssociatedFile]:
    written: list[litcache_pb2.AssociatedFile] = []
    for f in files:
        entry = litcache_pb2.AssociatedFile(role=f.role, name=f.name)
        if f.source_url is not None:
            entry.source_url = f.source_url
        if f.data is not None:
            if f.role not in _BLOB_DIRS:
                raise ValueError(f'cannot place blob with unknown role {f.role!r}')
            ext = posixpath.splitext(f.name)[1].lstrip('.')
            if not ext:
                raise ValueError(f'associated file {f.name!r} has no extension to content-address against')
            blob_name = storage.put_content_addressed(
                bucket, f.data, posixpath.join(paper_dir, _BLOB_DIRS[f.role]), ext
            )
            # Store the paper-relative path (the sha256 is its filename), not the
            # bare hash — consumers read the location directly, as for sources.
            entry.path = posixpath.relpath(blob_name, paper_dir)
        written.append(entry)
    return written


def _validate_metadata(metadata: bytes) -> None:
    try:
        pubmed_pb2.PubmedArticle.FromString(metadata)
    except message.DecodeError as e:
        raise ValueError('metadata is not a valid PubmedArticle proto') from e
