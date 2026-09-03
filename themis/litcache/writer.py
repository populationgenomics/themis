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
    metadata.pb                         # bibliographic (a PaperMetadata envelope)
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

`metadata.pb` is the one artifact re-creatable after the commit (`write_metadata`, an
idempotent overwrite; `themis.litcache.refresh` drives it) — the manifest holds no hash
of it and its bytes derive from the paper's identifiers, not from the directory. See
`docs/design/litcache-manifest.md` § Path layout.

`add_rendering` and `add_source_and_rendering` are the paths that mutate a
committed paper. Both use a generation-matched read-modify-write, so a concurrent
writer is detected and retried rather than clobbered: `add_rendering` adds a
`Rendering` against a source already in the manifest (the PDF-convert case);
`add_source_and_rendering` adds a new `Source` and its `Rendering` together (the
OA-fetch case, where the fetched XML is a source the paper lacked).
"""

from __future__ import annotations

import dataclasses
import datetime
import posixpath
from collections.abc import Sequence

from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from google.protobuf import timestamp_pb2

from themis.common import storage
from themis.litcache import paper_metadata
from themis.litcache.models import litcache_pb2

# The layout's fixed names, for readers that list `papers/` rather than address one paper.
PAPERS_DIR = 'papers'
MANIFEST_NAME = 'manifest.pb'
METADATA_NAME = 'metadata.pb'
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


def paper_dir(doc_id: str) -> str:
    """The layout root for `doc_id` — the prefix every one of its objects hangs off."""
    return posixpath.join(PAPERS_DIR, doc_id)


def manifest_path(doc_id: str) -> str:
    """The manifest key for `doc_id` — the commit point and resumability checkpoint.

    A caller probes this to skip an already-committed paper before doing the
    expensive conversion work `write_paper` would redo.
    """
    return posixpath.join(paper_dir(doc_id), MANIFEST_NAME)


def metadata_path(doc_id: str) -> str:
    """The `metadata.pb` key for `doc_id` — the bibliographic record beside the manifest."""
    return posixpath.join(paper_dir(doc_id), METADATA_NAME)


def source_revision_path(doc_id: str, handle: str, revision_hash: str, media_type: litcache_pb2.SourceFormat) -> str:
    """The GCS key for a source revision's content-addressed bytes.

    The read-side counterpart to `_write_source`'s placement: a caller resolves a
    manifest's `Source.handle` + `Revision.hash` to the object to download.

    Raises:
        ValueError: If `media_type` has no known on-disk extension.
    """
    ext = _SOURCE_EXTENSIONS.get(media_type)
    if ext is None:
        raise ValueError(f'source {handle!r} has unknown media type {media_type!r}')
    return posixpath.join(paper_dir(doc_id), _SOURCES_DIR, handle, f'{revision_hash}.{ext}')


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
    bibliographic `metadata.pb` bytes (a serialized `PaperMetadata` envelope
    carrying the resolving index's record). The writer adds only placement:
    content-addressed paths and hashes.
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
class AddRenderingResult:
    """The outcome of `add_rendering`.

    Attributes:
        hash: The rendering's markdown content hash — its `Manifest.renderings` key
            and the `renderings/{hash}.md` blob name.
        added: True when this call added the rendering; False when a rendering with
            that hash was already present (an idempotent no-op).
    """

    hash: str
    added: bool


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
            extension, or `metadata` that is not a `PaperMetadata` envelope meeting its
            constraints (`paper_metadata.parse`).
    """
    root = paper_dir(paper.doc_id)
    manifest_key = manifest_path(paper.doc_id)
    manifest_blob = bucket.blob(manifest_key)
    if manifest_blob.exists():
        existing = litcache_pb2.Manifest.FromString(manifest_blob.download_as_bytes())
        return WriteResult(manifest=existing, written=False)

    write_metadata(bucket, paper.doc_id, paper.metadata)
    sources = [_write_source(bucket, root, s) for s in paper.sources]
    revision_hashes = {src.handle: {rev.hash for rev in src.revisions} for src in sources}
    renderings = _write_renderings(bucket, root, revision_hashes, paper.renderings)
    files = _write_files(bucket, root, paper.files)

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


def write_metadata(bucket: gcs.Bucket, doc_id: str, metadata: bytes) -> None:
    """Write `doc_id`'s `metadata.pb`, replacing whatever is there.

    Called by `write_paper` before the commit, and by a metadata refresh after it. An
    overwrite, not a create: the manifest neither names nor hashes this object, so
    there is no generation for a concurrent writer to invalidate and nothing a re-run
    can leave inconsistent.

    Args:
        bucket: The cache bucket.
        doc_id: The paper whose record is written.
        metadata: The `metadata.pb` bytes — a serialized `PaperMetadata` envelope.

    Raises:
        ValueError: If `metadata` is not a `PaperMetadata` envelope meeting its constraints
            (`paper_metadata.parse`).
    """
    _validate_metadata(metadata)
    bucket.blob(metadata_path(doc_id)).upload_from_string(metadata)


_MANIFEST_RMW_ATTEMPTS = 5


class ConcurrentWriteError(Exception):
    """A manifest RMW lost its generation race `_MANIFEST_RMW_ATTEMPTS` times — **retryable**.

    Distinct from the `ValueError` these functions raise for a malformed rendering, which is a
    permanent programming error: a caller (the convert worker) retries this but not that.
    """


def add_rendering(bucket: gcs.Bucket, doc_id: str, rendering: RenderingInput) -> AddRenderingResult:
    """Add a rendering to an existing paper via a generation-matched manifest RMW.

    Writes the markdown blob (content-addressed, so idempotent and safe before the
    manifest), then reads the manifest at its current generation, adds the
    `Rendering` keyed by the markdown hash, and rewrites the manifest with
    `if_generation_match`. A concurrent writer (another conversion of the same
    paper, or an ingestion re-commit) invalidates the generation; the read-modify-
    write retries against the new manifest. Idempotent: a rendering whose hash is
    already present is a no-op, so a re-delivered conversion re-adds nothing.

    Args:
        bucket: The cache bucket holding the paper.
        doc_id: The paper whose manifest gains the rendering.
        rendering: The `Rendering` record plus its markdown (and optional docling
            json). `from_source`/`from_revision` are validated against the
            manifest's own sources.

    Returns:
        An `AddRenderingResult`: the rendering hash and whether it was newly added.

    Raises:
        google.api_core.exceptions.NotFound: If the paper's manifest is absent.
        ValueError: If the rendering names no source/revision present in the
            manifest, or its `model`/converter are inconsistent (a permanent
            programming error — do not retry).
        ConcurrentWriteError: If the RMW lost its generation race the whole retry
            budget (contention — retryable).
    """
    root = paper_dir(doc_id)
    markdown_bytes = rendering.markdown.encode('utf-8')
    name = storage.put_content_addressed(bucket, markdown_bytes, posixpath.join(root, _RENDERINGS_DIR), 'md')
    key = posixpath.splitext(posixpath.basename(name))[0]
    if rendering.docling_json is not None:
        docling_name = posixpath.join(root, _RENDERINGS_DIR, f'{key}.docling.json')
        bucket.blob(docling_name).upload_from_string(rendering.docling_json)

    manifest_blob = bucket.blob(manifest_path(doc_id))
    for _ in range(_MANIFEST_RMW_ATTEMPTS):
        manifest_blob.reload()  # NotFound when the paper does not exist; sets .generation
        generation = manifest_blob.generation
        try:
            manifest_bytes = manifest_blob.download_as_bytes(if_generation_match=generation)
        except api_exceptions.PreconditionFailed:
            continue  # a concurrent writer bumped the generation between reload and read; re-read
        manifest = litcache_pb2.Manifest.FromString(manifest_bytes)
        if key in manifest.renderings:
            return AddRenderingResult(hash=key, added=False)
        revision_hashes = {s.handle: {rev.hash for rev in s.revisions} for s in manifest.sources}
        _validate_rendering(rendering.rendering, revision_hashes)
        manifest.renderings[key].CopyFrom(rendering.rendering)
        try:
            manifest_blob.upload_from_string(manifest.SerializeToString(), if_generation_match=generation)
        except api_exceptions.PreconditionFailed:
            continue  # a concurrent writer bumped the generation; re-read and retry
        return AddRenderingResult(hash=key, added=True)
    raise ConcurrentWriteError(f'manifest RMW for {doc_id} did not converge in {_MANIFEST_RMW_ATTEMPTS} attempts')


def add_source_and_rendering(
    bucket: gcs.Bucket, doc_id: str, source: SourceInput, rendering: RenderingInput
) -> AddRenderingResult:
    """Add a new source and its rendering to an existing paper via a manifest RMW.

    The OA-fetch write-back: the fetch produced an XML source the paper lacked, so
    both the `Source` and the `Rendering` derived from it must land in one
    generation-matched read-modify-write. Writes the content-addressed source and
    markdown blobs first (idempotent, safe before the manifest), then reads the
    manifest at its current generation, merges the source into `manifest.sources`,
    adds the `Rendering` keyed by the markdown hash, and rewrites with
    `if_generation_match`; a concurrent writer invalidates the generation and the
    read-modify-write retries.

    The source merge is by `handle`: an absent lineage is appended whole; a present
    one gains the revision only if its hash is new. Idempotent throughout — a
    re-delivered fetch re-adds no source, revision, or rendering.

    Args:
        bucket: The cache bucket holding the paper.
        doc_id: The paper whose manifest gains the source and rendering.
        source: The new source lineage plus its single revision's bytes.
        rendering: The `Rendering` record plus its markdown. `from_source`/
            `from_revision` are validated against the post-merge manifest sources,
            so they must name `source` (or a source already present).

    Returns:
        An `AddRenderingResult`: the rendering hash and whether it was newly added.

    Raises:
        google.api_core.exceptions.NotFound: If the paper's manifest is absent.
        ValueError: If the rendering names no source/revision present after the
            merge, its `model`/converter are inconsistent, or an incoming lineage's
            media_type disagrees with the manifest's (permanent — do not retry).
        ConcurrentWriteError: If the RMW lost its generation race the whole retry
            budget (contention — retryable).
    """
    root = paper_dir(doc_id)
    source_proto = _write_source(bucket, root, source)
    markdown_bytes = rendering.markdown.encode('utf-8')
    name = storage.put_content_addressed(bucket, markdown_bytes, posixpath.join(root, _RENDERINGS_DIR), 'md')
    key = posixpath.splitext(posixpath.basename(name))[0]
    if rendering.docling_json is not None:
        docling_name = posixpath.join(root, _RENDERINGS_DIR, f'{key}.docling.json')
        bucket.blob(docling_name).upload_from_string(rendering.docling_json)

    manifest_blob = bucket.blob(manifest_path(doc_id))
    for _ in range(_MANIFEST_RMW_ATTEMPTS):
        manifest_blob.reload()  # NotFound when the paper does not exist; sets .generation
        generation = manifest_blob.generation
        try:
            manifest_bytes = manifest_blob.download_as_bytes(if_generation_match=generation)
        except api_exceptions.PreconditionFailed:
            continue  # a concurrent writer bumped the generation between reload and read; re-read
        manifest = litcache_pb2.Manifest.FromString(manifest_bytes)
        source_changed = _merge_source(manifest, source_proto)
        rendering_present = key in manifest.renderings
        if not source_changed and rendering_present:
            return AddRenderingResult(hash=key, added=False)
        if not rendering_present:
            revision_hashes = {s.handle: {rev.hash for rev in s.revisions} for s in manifest.sources}
            _validate_rendering(rendering.rendering, revision_hashes)
            manifest.renderings[key].CopyFrom(rendering.rendering)
        try:
            manifest_blob.upload_from_string(manifest.SerializeToString(), if_generation_match=generation)
        except api_exceptions.PreconditionFailed:
            continue  # a concurrent writer bumped the generation; re-read and retry
        return AddRenderingResult(hash=key, added=not rendering_present)
    raise ConcurrentWriteError(f'manifest RMW for {doc_id} did not converge in {_MANIFEST_RMW_ATTEMPTS} attempts')


def _merge_source(manifest: litcache_pb2.Manifest, source: litcache_pb2.Source) -> bool:
    """Merge `source` into `manifest.sources` by handle; return whether it changed anything.

    An absent lineage is appended whole; a present one gains only the revisions whose
    hash it lacks. Lineage-level fields (media_type, licence, access) are stable across
    a lineage's revisions, so a present lineage keeps its own and only the revisions merge.
    """
    existing = next((s for s in manifest.sources if s.handle == source.handle), None)
    if existing is None:
        manifest.sources.append(source)
        return True
    # The revision blob's path extension derives from media_type (`_write_source`), so a handle whose
    # media_type disagrees with the manifest's would advertise a revision under one extension with the
    # bytes at another — a dangling reference discovered only at read time. Fail loud instead.
    if existing.media_type != source.media_type:
        raise ValueError(
            f'source lineage {source.handle!r} is {existing.media_type!r} in the manifest, not {source.media_type!r}'
        )
    present = {rev.hash for rev in existing.revisions}
    changed = False
    for revision in source.revisions:
        if revision.hash not in present:
            existing.revisions.append(revision)
            present.add(revision.hash)
            changed = True
    return changed


def _validate_rendering(r: litcache_pb2.Rendering, revision_hashes: dict[str, set[str]]) -> None:
    if r.from_source not in revision_hashes:
        raise ValueError(f'rendering from_source {r.from_source!r} names no source')
    if r.from_revision not in revision_hashes[r.from_source]:
        raise ValueError(f'rendering from_revision {r.from_revision!r} names no revision of source {r.from_source!r}')
    # model identifies the LLM iff the converter is model-driven (llm-ocr).
    if (r.converter == litcache_pb2.Converter.CONVERTER_LLM_OCR) != r.HasField('model'):
        raise ValueError(f'rendering converter {r.converter!r} and model presence are inconsistent')


def _write_source(bucket: gcs.Bucket, root: str, src: SourceInput) -> litcache_pb2.Source:
    ext = _SOURCE_EXTENSIONS.get(src.media_type)
    if ext is None:
        raise ValueError(f'source {src.handle!r} has unknown media type {src.media_type!r}')
    name = storage.put_content_addressed(bucket, src.data, posixpath.join(root, _SOURCES_DIR, src.handle), ext)
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
    root: str,
    revision_hashes: dict[str, set[str]],
    rins: Sequence[RenderingInput],
) -> dict[str, litcache_pb2.Rendering]:
    renderings: dict[str, litcache_pb2.Rendering] = {}
    for rin in rins:
        r = rin.rendering
        _validate_rendering(r, revision_hashes)
        markdown_bytes = rin.markdown.encode('utf-8')
        name = storage.put_content_addressed(bucket, markdown_bytes, posixpath.join(root, _RENDERINGS_DIR), 'md')
        key = posixpath.splitext(posixpath.basename(name))[0]
        if key in renderings:
            raise ValueError(f'two renderings share the markdown hash {key}')
        if rin.docling_json is not None:
            docling_name = posixpath.join(root, _RENDERINGS_DIR, f'{key}.docling.json')
            bucket.blob(docling_name).upload_from_string(rin.docling_json)
        renderings[key] = r
    return renderings


def _write_files(bucket: gcs.Bucket, root: str, files: Sequence[FileInput]) -> list[litcache_pb2.AssociatedFile]:
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
            blob_name = storage.put_content_addressed(bucket, f.data, posixpath.join(root, _BLOB_DIRS[f.role]), ext)
            # Store the paper-relative path (the sha256 is its filename), not the
            # bare hash — consumers read the location directly, as for sources.
            entry.path = posixpath.relpath(blob_name, root)
        written.append(entry)
    return written


def _validate_metadata(metadata: bytes) -> None:
    paper_metadata.parse(metadata)
