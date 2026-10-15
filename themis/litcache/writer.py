"""Write a paper directory and commit it with the manifest.

litcache owns placement: given a resolved identity, the paper's source lineages
and renderings (each carrying its bytes), the bibliographic metadata, and the
known associated files, this module writes the per-paper GCS layout and computes
the manifest's paths and hashes. The caller (the per-paper pipeline) supplies
byte-bearing inputs and how each rendering was made; the writer content-addresses
every blob, hashes source bytes, records each rendering as a `Record` keyed by its
markdown's hash, points the `markdown` ref at the best of them, and assembles the
`Manifest` (see `docs/design/litcache-manifest.md`). Every manifest it writes is
validated against the manifest's own constraints first, so a ref or a dep that
resolves to nothing is refused rather than stored.

Layout written under `papers/{doc_id}/`:

    manifest.pb                         # the commit
    metadata.pb                         # bibliographic (a PaperMetadata envelope), after the commit
    sources/{handle}/{hex}.{ext}        # raw source bytes, content-addressed
    renderings/{hex}.md                 # rendering markdown, keyed by its hash
    renderings/{hex}.docling.json       # structured docling output (converter=docling)
    figures/{hash}.{ext}                # content-addressed blobs
    supplementary/{hash}.{ext}

The manifest write is the commit point: every content-addressed object is written
first, then the manifest. A crash before the manifest leaves no manifest, so a re-run
sees the paper as uncached and re-completes it, reusing the claimed `doc_id`.
Content-addressed writes are idempotent (identical bytes map to one name, and a
GCS upload is atomic), so a re-put is a no-op. `write_paper` skips a paper whose
manifest already exists; that manifest is the resumability checkpoint, not the
crosswalk row. The commit itself is create-only (`if_generation_match=0`), so if two
workers race past the skip check the first to commit wins. The loser joins the winner's
manifest, adding its work when every id it records is one the manifest records.

`metadata.pb` is written after the commit, by the writer whose commit won. A committed
paper without one is filled by `fill_absent_metadata` or `themis.litcache.refresh`
(`docs/design/litcache-manifest.md` § Path layout).

`add_rendering` and `add_source_and_rendering` are the paths that mutate a
committed paper. Both use a generation-matched read-modify-write, so a concurrent
writer is detected and retried rather than clobbered: `add_rendering` adds a
rendering of a source already in the manifest (the PDF-convert case);
`add_source_and_rendering` adds a new source and its rendering together (the
OA-fetch case, where the fetched XML is a source the paper lacked). Each moves the
`markdown` ref to the new rendering when it outranks the one named.
"""

from __future__ import annotations

import dataclasses
import datetime
import posixpath
from collections.abc import Mapping, Sequence

import protovalidate
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from google.cloud.storage import retry as gcs_retry
from google.protobuf import timestamp_pb2

from themis.common import storage
from themis.litcache import paper_metadata, records
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


# The default policy retries only a generation-matched upload; rewriting the paper's own record is idempotent.
_RETRY_UNCONDITIONALLY = gcs_retry.ConditionalRetryPolicy(gcs_retry.DEFAULT_RETRY, lambda: True, [])


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
            manifest's `Source.handle`. A revision joins an existing lineage only on
            that lineage's terms, so a file obtained on other terms needs a handle of
            its own.
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
    """A rendering's markdown (and optional docling json) bytes, and how and from what it was made.

    The writer records it as a `Record` keyed by the markdown's content hash, whose one
    dep is `from_revision`; the manifest refuses the record if no lineage holds that
    revision.

    Attributes:
        construction: The converter, its version, and the model where a vision model
            transcribed a PDF.
        from_revision: The hash of the source revision's bytes the markdown was made from.
        created_at: When the rendering was made.
        markdown: The rendered markdown text.
        docling_json: The structured DoclingDocument json, written alongside the
            markdown when the converter is docling; None otherwise.
    """

    construction: litcache_pb2.RenderingConstruction
    from_revision: str
    created_at: datetime.datetime
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
            (lazy fetch), whose manifest entry has no `path`, hash or capture time.
        captured_at: When `data` was fetched; required exactly when `data` is present.
    """

    role: litcache_pb2.AssociatedFileRole
    name: str
    source_url: str | None = None
    data: bytes | None = None
    captured_at: datetime.datetime | None = None


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
        hash: The rendering's markdown content hash — its `Manifest.records` key
            and the `renderings/{hash}.md` blob name.
        added: True when this call added the rendering; False when a rendering with
            that hash was already present (its record is then the newer run's).
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

    Writes the sources, renderings, and blobs, commits the manifest, and then writes
    `metadata.pb`. A call that finds the paper's manifest already committed, before its
    own commit or by losing it, joins that manifest instead. When every id it records is
    one the manifest records, it adds its sources, renderings and files in one
    generation-matched read-modify-write; otherwise it leaves the manifest as it is, since
    it may have reached the `doc_id` through an id it shares with another paper. Either
    way it writes `metadata.pb` only through `fill_absent_metadata`.

    Args:
        bucket: The cache bucket to write into.
        paper: The fully-resolved paper inputs (identity, source lineages with
            bytes, renderings, metadata, associated files).

    Returns:
        A `WriteResult`: the manifest, and whether this call committed it.

    Raises:
        ValueError: On an inconsistency the writer refuses to commit — a manifest that
            breaks its own constraints (a rendering made from a revision no source
            holds, or carrying a `model` its converter does not take), two renderings
            with the same markdown hash, a source with an unknown media type, a blob
            with an unknown role or a name without an extension, a fetched file with no
            capture time, or `metadata` that is not a `PaperMetadata` envelope meeting
            its constraints (`paper_metadata.parse`). Joining a committed manifest, `metadata`
            is checked only when the ids let it be written (`fill_absent_metadata`).
        ConcurrentWriteError: Joining a committed manifest lost its generation race the whole
            retry budget (contention — retryable).
    """
    root = paper_dir(paper.doc_id)
    manifest_key = manifest_path(paper.doc_id)
    manifest_blob = bucket.blob(manifest_key)
    if manifest_blob.exists():
        return _join_committed(bucket, paper)

    _validate_metadata(paper.metadata)
    sources = [_write_source(bucket, root, s) for s in paper.sources]
    rendering_records = _write_renderings(bucket, root, paper.renderings)
    files = _write_files(bucket, root, paper.files)

    manifest = litcache_pb2.Manifest(
        doc_id=paper.doc_id,
        external_ids=paper.external_ids,
        claim_key=paper.claim_key,
        equivalence=paper.equivalence,
        retraction=paper.retraction,
        sources=sources,
        files=files,
    )
    for key, record in rendering_records.items():
        _put_record(manifest, key, record, records.MARKDOWN_REF)
    validate_manifest(manifest)
    try:
        # if_generation_match=0 (create-only): the first writer to commit this doc_id
        # wins, closing the exists()-then-write race between concurrent workers.
        manifest_blob.upload_from_string(manifest.SerializeToString(), if_generation_match=0)
    except api_exceptions.PreconditionFailed:
        # also reached by our own commit, when its response was lost and the client retried it: joining it adds nothing
        return _join_committed(bucket, paper)
    write_metadata(bucket, paper.doc_id, paper.metadata)
    return WriteResult(manifest=manifest, written=True)


def fill_absent_metadata(
    bucket: gcs.Bucket,
    doc_id: str,
    manifest: litcache_pb2.Manifest,
    external_ids: litcache_pb2.ExternalIds,
    metadata: bytes,
) -> None:
    """Write a committed paper's `metadata.pb` if it has none and `external_ids` are its manifest's.

    The ids decide whether `metadata` is this paper's record: a caller that reached the
    `doc_id` through a shared identifier carries other ids, and writes nothing. The write is
    create-only, so a record that lands first is kept.

    Args:
        bucket: The cache bucket.
        doc_id: The paper whose directory `manifest` was read from.
        manifest: The paper's committed manifest.
        external_ids: The identifiers `metadata` was resolved under: the ids the caller's
            paper claimed, as its manifest would record them.
        metadata: The caller's `metadata.pb` bytes, a serialized `PaperMetadata` envelope.

    Raises:
        ValueError: If `manifest` names a `doc_id` other than its directory's, or `external_ids`
            are the manifest's and `metadata` is not a `PaperMetadata` envelope meeting its
            constraints (`paper_metadata.parse`).
    """
    if manifest.doc_id != doc_id:
        raise ValueError(f'manifest in {doc_id} names doc_id {manifest.doc_id!r}')
    if external_ids != manifest.external_ids:
        return
    _validate_metadata(metadata)
    try:
        bucket.blob(metadata_path(doc_id)).upload_from_string(metadata, if_generation_match=0)
    except api_exceptions.PreconditionFailed:
        return


def write_metadata(bucket: gcs.Bucket, doc_id: str, metadata: bytes) -> None:
    """Write `doc_id`'s `metadata.pb`, replacing whatever is there.

    Nothing is compared, so `metadata` has to be the paper's own record; a caller that
    may hold another paper's, having reached the `doc_id` through a shared identifier,
    uses `fill_absent_metadata`.

    Args:
        bucket: The cache bucket.
        doc_id: The paper whose record is written.
        metadata: The `metadata.pb` bytes — a serialized `PaperMetadata` envelope.

    Raises:
        ValueError: If `metadata` is not a `PaperMetadata` envelope meeting its constraints
            (`paper_metadata.parse`).
    """
    _validate_metadata(metadata)
    bucket.blob(metadata_path(doc_id)).upload_from_string(metadata, retry=_RETRY_UNCONDITIONALLY)


_MANIFEST_RMW_ATTEMPTS = 5


def _claims_only(held: litcache_pb2.ExternalIds, claimed: litcache_pb2.ExternalIds) -> bool:
    """Whether every id `claimed` records is one `held` records too.

    Only the schemes `ExternalIds` has a field for are compared; a `pii` or `binhash` claim is in neither.
    """
    return all(
        held.HasField(field.name) and getattr(held, field.name) == value for field, value in claimed.ListFields()
    )


def _merge_work(
    manifest: litcache_pb2.Manifest,
    rendering_records: Mapping[str, litcache_pb2.Record],
    files: Sequence[litcache_pb2.AssociatedFile],
) -> None:
    """Add a joining writer's renderings and files to the manifest it joins.

    Each rendering goes in by `records.put_record`, so a held one is replaced by a newer run's, and a file entry the
    manifest already lists is not listed again.
    """
    for key, record in rendering_records.items():
        _put_record(manifest, key, record, records.MARKDOWN_REF)
    listed = {(f.role, f.name, f.hash) for f in manifest.files}
    manifest.files.extend(f for f in files if (f.role, f.name, f.hash) not in listed)


def _join_committed(bucket: gcs.Bucket, paper: PaperInput) -> WriteResult:
    """Join the manifest another writer committed for `paper.doc_id`: see `write_paper`."""
    root = paper_dir(paper.doc_id)
    staged: tuple[dict[str, litcache_pb2.Record], list[litcache_pb2.AssociatedFile]] | None = None
    for _ in range(_MANIFEST_RMW_ATTEMPTS):
        # a fresh handle each attempt: a reloaded one pins its generation, so reloading it again reads that one
        manifest_blob = bucket.blob(manifest_path(paper.doc_id))
        manifest_blob.reload()
        generation = manifest_blob.generation
        try:
            manifest_bytes = manifest_blob.download_as_bytes(if_generation_match=generation)
        except api_exceptions.PreconditionFailed:
            continue  # a concurrent writer bumped the generation between reload and read; re-read
        manifest = litcache_pb2.Manifest.FromString(manifest_bytes)
        if manifest.doc_id != paper.doc_id:
            raise ValueError(f'manifest in {paper.doc_id} names doc_id {manifest.doc_id!r}')
        if not _claims_only(manifest.external_ids, paper.external_ids):
            # An id the manifest does not record: this writer may have reached the doc_id through an id it shares with
            # another paper, so its work stays out.
            return WriteResult(manifest=manifest, written=False)
        if staged is None:
            staged = (_write_renderings(bucket, root, paper.renderings), _write_files(bucket, root, paper.files))
        rendering_records, files = staged
        before = manifest.SerializeToString(deterministic=True)
        # Each source's handle is chosen from the manifest this attempt read; its bytes go under it, content-addressed,
        # so writing them again on a retry is a no-op.
        for source in paper.sources:
            placed = dataclasses.replace(source, handle=_lineage_handle(manifest, source))
            _merge_source(manifest, _write_source(bucket, root, placed))
        _merge_work(manifest, rendering_records, files)
        if manifest.SerializeToString(deterministic=True) != before:
            validate_manifest(manifest)
            try:
                manifest_blob.upload_from_string(manifest.SerializeToString(), if_generation_match=generation)
            except api_exceptions.PreconditionFailed:
                continue  # a concurrent writer bumped the generation; re-read and retry
        fill_absent_metadata(bucket, paper.doc_id, manifest, paper.external_ids, paper.metadata)
        return WriteResult(manifest=manifest, written=False)
    raise ConcurrentWriteError(
        f'joining the manifest of {paper.doc_id} did not converge in {_MANIFEST_RMW_ATTEMPTS} attempts'
    )


class ManifestRefusedError(ValueError):
    """The writer refuses a write, and will refuse it on every attempt — **permanent**.

    The manifest would break its own constraints, a revision arrives on other terms than its lineage's, or an input
    cannot be placed. Distinct from a transient storage fault, which surfaces as the storage client's own error.
    """


class ConcurrentWriteError(Exception):
    """A manifest RMW lost its generation race `_MANIFEST_RMW_ATTEMPTS` times — **retryable**.

    Distinct from the `ValueError` these functions raise for a malformed rendering, which is a
    permanent programming error: a caller (the convert worker) retries this but not that.
    """


def add_rendering(bucket: gcs.Bucket, doc_id: str, rendering: RenderingInput) -> AddRenderingResult:
    """Add a rendering to an existing paper via a generation-matched manifest RMW.

    Writes the markdown blob (content-addressed, so idempotent and safe before the
    manifest), then reads the manifest at its current generation, adds the
    rendering's record keyed by the markdown hash, moves the `markdown` ref to it if
    it outranks the one named, and rewrites the manifest with `if_generation_match`.
    A concurrent writer (another conversion of the same paper, or an ingestion
    re-commit) invalidates the generation; the read-modify-write retries against the
    new manifest. A rendering whose hash is already present keeps its key, and its
    record is the newer run's; a re-delivered conversion adds no second record.

    Args:
        bucket: The cache bucket holding the paper.
        doc_id: The paper whose manifest gains the rendering.
        rendering: The rendering's markdown (and optional docling json), and how and
            from which revision it was made; the revision must be one the manifest holds.

    Returns:
        An `AddRenderingResult`: the rendering hash and whether it was newly added.

    Raises:
        google.api_core.exceptions.NotFound: If the paper's manifest is absent.
        ManifestRefusedError: If the manifest would break its own constraints — the
            rendering's revision is one it does not hold, or its `model` and converter
            disagree (permanent — do not retry).
        ConcurrentWriteError: If the RMW lost its generation race the whole retry
            budget (contention — retryable).
    """
    root = paper_dir(doc_id)
    key = _write_rendering_blobs(bucket, root, rendering)

    for _ in range(_MANIFEST_RMW_ATTEMPTS):
        # a fresh handle each attempt: a reloaded one pins its generation, so reloading it again reads that one
        manifest_blob = bucket.blob(manifest_path(doc_id))
        manifest_blob.reload()  # NotFound when the paper does not exist; sets .generation
        generation = manifest_blob.generation
        try:
            manifest_bytes = manifest_blob.download_as_bytes(if_generation_match=generation)
        except api_exceptions.PreconditionFailed:
            continue  # a concurrent writer bumped the generation between reload and read; re-read
        manifest = litcache_pb2.Manifest.FromString(manifest_bytes)
        present = key in manifest.records
        if not _put_rendering_record(manifest, key, rendering):
            return AddRenderingResult(hash=key, added=False)
        validate_manifest(manifest)
        try:
            manifest_blob.upload_from_string(manifest.SerializeToString(), if_generation_match=generation)
        except api_exceptions.PreconditionFailed:
            continue  # a concurrent writer bumped the generation; re-read and retry
        return AddRenderingResult(hash=key, added=not present)
    raise ConcurrentWriteError(f'manifest RMW for {doc_id} did not converge in {_MANIFEST_RMW_ATTEMPTS} attempts')


def add_source_and_rendering(
    bucket: gcs.Bucket, doc_id: str, source: SourceInput, rendering: RenderingInput
) -> AddRenderingResult:
    """Add a new source and its rendering to an existing paper via a manifest RMW.

    The OA-fetch write-back: the fetch produced an XML source the paper lacked, so
    both the source and the rendering made from it must land in one
    generation-matched read-modify-write. Writes the content-addressed source and
    markdown blobs first (idempotent, safe before the manifest), then reads the
    manifest at its current generation, merges the source into `manifest.sources`,
    adds the rendering's record keyed by the markdown hash, moves the `markdown` ref
    to it if it outranks the one named, and rewrites with `if_generation_match`; a
    concurrent writer invalidates the generation and the read-modify-write retries.

    The source joins the lineage of its handle when it arrives on that lineage's
    terms, and otherwise a numbered lineage beside it (`xml-2`) holding it on its own,
    chosen from the manifest each attempt reads. A lineage gains the revision only if
    its hash is new. Idempotent throughout — a re-delivered fetch re-adds no source,
    revision, or rendering.

    Args:
        bucket: The cache bucket holding the paper.
        doc_id: The paper whose manifest gains the source and rendering.
        source: The new source lineage plus its single revision's bytes.
        rendering: The rendering's markdown, and how and from which revision it was
            made; the revision must be `source`'s or one the manifest already holds.

    Returns:
        An `AddRenderingResult`: the rendering hash and whether it was newly added.

    Raises:
        google.api_core.exceptions.NotFound: If the paper's manifest is absent.
        ManifestRefusedError: If the manifest would break its own constraints, or an
            incoming lineage's media type disagrees with the manifest's lineage of that
            handle (permanent — do not retry).
        ConcurrentWriteError: If the RMW lost its generation race the whole retry
            budget (contention — retryable).
    """
    root = paper_dir(doc_id)
    key = _write_rendering_blobs(bucket, root, rendering)

    for _ in range(_MANIFEST_RMW_ATTEMPTS):
        # a fresh handle each attempt: a reloaded one pins its generation, so reloading it again reads that one
        manifest_blob = bucket.blob(manifest_path(doc_id))
        manifest_blob.reload()  # NotFound when the paper does not exist; sets .generation
        generation = manifest_blob.generation
        try:
            manifest_bytes = manifest_blob.download_as_bytes(if_generation_match=generation)
        except api_exceptions.PreconditionFailed:
            continue  # a concurrent writer bumped the generation between reload and read; re-read
        manifest = litcache_pb2.Manifest.FromString(manifest_bytes)
        # The handle is chosen from the manifest this attempt read, so a lineage a concurrent writer added on other
        # terms is seen; the bytes go under it, content-addressed, so writing them again on a retry is a no-op.
        placed = dataclasses.replace(source, handle=_lineage_handle(manifest, source))
        source_changed = _merge_source(manifest, _write_source(bucket, root, placed))
        rendering_present = key in manifest.records
        record_changed = _put_rendering_record(manifest, key, rendering)
        if not source_changed and not record_changed:
            return AddRenderingResult(hash=key, added=False)
        validate_manifest(manifest)
        try:
            manifest_blob.upload_from_string(manifest.SerializeToString(), if_generation_match=generation)
        except api_exceptions.PreconditionFailed:
            continue  # a concurrent writer bumped the generation; re-read and retry
        return AddRenderingResult(hash=key, added=not rendering_present)
    raise ConcurrentWriteError(f'manifest RMW for {doc_id} did not converge in {_MANIFEST_RMW_ATTEMPTS} attempts')


def same_terms(lineage: litcache_pb2.Source, source: litcache_pb2.Source | SourceInput) -> bool:
    """Whether `source` arrives on `lineage`'s terms: the same licence, licence basis and access."""
    return (lineage.licence, lineage.licence_basis, lineage.access) == (
        source.licence,
        source.licence_basis,
        source.access,
    )


def _lineage_handle(manifest: litcache_pb2.Manifest, source: SourceInput) -> str:
    """The handle `source`'s revision should join: its own, or the first `{handle}-{n}` holding it on its terms.

    A revision joins a lineage only on that lineage's terms, so bytes arriving on other terms than the lineage of
    their handle start, or join, a numbered lineage beside it rather than being refused.
    """
    candidate, n = source.handle, 1
    while True:
        lineage = next((s for s in manifest.sources if s.handle == candidate), None)
        if lineage is None or same_terms(lineage, source):
            return candidate
        n += 1
        candidate = f'{source.handle}-{n}'


def _merge_source(manifest: litcache_pb2.Manifest, source: litcache_pb2.Source) -> bool:
    """Merge `source` into `manifest.sources` by handle; return whether it changed anything.

    An absent lineage is appended whole; a present one gains only the revisions whose
    hash it lacks. Lineage-level fields (media_type, licence, access) hold for every
    revision of a lineage, so a revision on other terms is refused: it belongs in a
    lineage of its own, under its own handle.
    """
    existing = next((s for s in manifest.sources if s.handle == source.handle), None)
    if existing is None:
        manifest.sources.append(source)
        return True
    # The revision blob's path extension derives from media_type (`_write_source`), so a handle whose
    # media_type disagrees with the manifest's would advertise a revision under one extension with the
    # bytes at another — a dangling reference discovered only at read time. Fail loud instead.
    if existing.media_type != source.media_type:
        raise ManifestRefusedError(
            f'source lineage {source.handle!r} is {existing.media_type!r} in the manifest, not {source.media_type!r}'
        )
    present = {rev.hash for rev in existing.revisions}
    incoming = [revision for revision in source.revisions if revision.hash not in present]
    if not incoming:
        return False  # bytes the lineage already holds: a re-delivery, whatever terms it arrives with
    if not same_terms(existing, source):
        raise ManifestRefusedError(
            f'source lineage {source.handle!r} holds its revisions on other terms; '
            'a revision on these terms needs a lineage of its own'
        )
    existing.revisions.extend(incoming)
    return True


def _violations(error: protovalidate.ValidationError) -> str:
    return '; '.join(f'{v.proto.rule_id}: {v.proto.message}' for v in error.violations)


def validate_manifest(manifest: litcache_pb2.Manifest) -> None:
    """Refuse a manifest that breaks its own constraints; every write here checks the whole manifest with it.

    Raises:
        ManifestRefusedError: If `manifest` breaks any of its constraints, naming each rule broken.
    """
    try:
        protovalidate.validate(manifest)
    except protovalidate.ValidationError as e:
        raise ManifestRefusedError(f'manifest {manifest.doc_id} breaks its constraints: {_violations(e)}') from e


def _rendering_record(rendering: RenderingInput) -> litcache_pb2.Record:
    created = timestamp_pb2.Timestamp()
    created.FromDatetime(rendering.created_at)
    return litcache_pb2.Record(deps=[rendering.from_revision], created_at=created, rendering=rendering.construction)


def _put_record(manifest: litcache_pb2.Manifest, key: str, record: litcache_pb2.Record, ref: str | None) -> bool:
    """`records.put_record`, with its refusal raised as this module's permanent one."""
    try:
        return records.put_record(manifest, key, record, ref)
    except records.RecordRefusedError as e:
        raise ManifestRefusedError(str(e)) from e


def _put_rendering_record(manifest: litcache_pb2.Manifest, key: str, rendering: RenderingInput) -> bool:
    """Record `rendering` under `key`, competing for the `markdown` ref; return whether anything changed."""
    return _put_record(manifest, key, _rendering_record(rendering), records.MARKDOWN_REF)


def _write_rendering_blobs(bucket: gcs.Bucket, root: str, rendering: RenderingInput) -> str:
    """Write the rendering's markdown (and docling json), content-addressed; return the markdown's hash."""
    name = storage.put_content_addressed(
        bucket, rendering.markdown.encode('utf-8'), posixpath.join(root, _RENDERINGS_DIR), 'md'
    )
    key = posixpath.splitext(posixpath.basename(name))[0]
    if rendering.docling_json is not None:
        docling_name = posixpath.join(root, _RENDERINGS_DIR, f'{key}.docling.json')
        bucket.blob(docling_name).upload_from_string(rendering.docling_json)
    return key


def _write_source(bucket: gcs.Bucket, root: str, src: SourceInput) -> litcache_pb2.Source:
    ext = _SOURCE_EXTENSIONS.get(src.media_type)
    if ext is None:
        raise ManifestRefusedError(f'source {src.handle!r} has unknown media type {src.media_type!r}')
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


def _write_renderings(bucket: gcs.Bucket, root: str, rins: Sequence[RenderingInput]) -> dict[str, litcache_pb2.Record]:
    written: dict[str, litcache_pb2.Record] = {}
    for rin in rins:
        key = _write_rendering_blobs(bucket, root, rin)
        if key in written:
            raise ManifestRefusedError(f'two renderings share the markdown hash {key}')
        written[key] = _rendering_record(rin)
    return written


def _write_files(bucket: gcs.Bucket, root: str, files: Sequence[FileInput]) -> list[litcache_pb2.AssociatedFile]:
    written: list[litcache_pb2.AssociatedFile] = []
    for f in files:
        entry = litcache_pb2.AssociatedFile(role=f.role, name=f.name)
        if f.source_url is not None:
            entry.source_url = f.source_url
        if f.data is not None:
            if f.role not in _BLOB_DIRS:
                raise ManifestRefusedError(f'cannot place blob with unknown role {f.role!r}')
            ext = posixpath.splitext(f.name)[1].lstrip('.')
            if not ext:
                raise ManifestRefusedError(f'associated file {f.name!r} has no extension to content-address against')
            if f.captured_at is None:
                raise ManifestRefusedError(f'associated file {f.name!r} has bytes but no capture time')
            blob_name = storage.put_content_addressed(bucket, f.data, posixpath.join(root, _BLOB_DIRS[f.role]), ext)
            # The paper-relative path is what consumers read the bytes from; the hash, its stem, is what a record's
            # dep names.
            entry.path = posixpath.relpath(blob_name, root)
            entry.hash = posixpath.splitext(posixpath.basename(blob_name))[0]
            entry.captured_at.FromDatetime(f.captured_at)
        written.append(entry)
    return written


def _validate_metadata(metadata: bytes) -> None:
    paper_metadata.parse(metadata)
