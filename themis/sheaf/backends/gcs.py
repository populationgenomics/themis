"""A Google Cloud Storage backend.

`ifGenerationMatch` on a single object is the compare-and-swap the protocol needs, and
`ifGenerationMatch=0` means "only if absent", which is how a repository is created exactly once.
Object versioning on the bucket makes every superseded ref document a retained noncurrent
generation — the durable reflog, at no cost in code. Design:
`docs/design/sheaf.md`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import override

from google.api_core import exceptions as api_exceptions
from google.cloud import storage

from themis.sheaf import backend, errors

# GCS spells "the object must not exist" as a generation precondition of zero.
MUST_NOT_EXIST = 0


_READ_ATTEMPTS = 8


def _generation_of(blob: storage.Blob) -> backend.Generation:
    """The generation the server assigned `blob`.

    Raises:
        SheafError: If the client carries none. A locally-constructed blob has no generation; one
            the server has answered for always does, so its absence means the response was not the
            round-trip this code assumes and the precondition cannot be trusted.
    """
    if blob.generation is None:
        raise errors.SheafError(f'{blob.name}: the storage client reported no generation')
    return blob.generation


def _size_of(blob: storage.Blob) -> int:
    """The blob's size in bytes.

    Raises:
        SheafError: If the client carries none. Compaction's ratio is computed from it, so a
            default silently reprices the manifest it is judging.
    """
    if blob.size is None:
        raise errors.SheafError(f'{blob.name}: the storage client reported no size')
    return blob.size


class GcsBackend(backend.Backend):
    """Object-store semantics over one GCS bucket, optionally under a key prefix."""

    def __init__(self, bucket: storage.Bucket, prefix: str = '') -> None:
        self.bucket = bucket
        self.prefix = prefix.strip('/')

    def _key(self, key: str) -> str:
        return f'{self.prefix}/{key}' if self.prefix else key

    @override
    def get_mutable(self, key: str) -> backend.StoredBlob:
        """Read the live generation of `key`.

        Raises:
            NotFound: If the object does not exist.
            SheafError: If the client reports no generation for it.
        """
        for _ in range(_READ_ATTEMPTS):
            blob = self.bucket.get_blob(self._key(key))
            if blob is None:
                raise errors.NotFound(key)
            generation = _generation_of(blob)
            # Pin the download to the generation just observed, so the bytes and the token agree
            # even if another writer lands mid-read. On an unversioned bucket that overwrite makes
            # the pinned generation unfetchable, so the read starts over.
            try:
                data = blob.download_as_bytes(if_generation_match=generation)
            except (api_exceptions.NotFound, api_exceptions.PreconditionFailed):
                continue
            return backend.StoredBlob(data=data, generation=generation)
        raise errors.SheafError(f'{key}: overwritten on every one of {_READ_ATTEMPTS} reads')

    @override
    def cas_mutable(self, key: str, data: bytes, expected: backend.Generation | None) -> backend.Generation:
        """Write `key` only if its generation is still `expected`.

        Raises:
            PreconditionFailed: If the generation moved, or the object exists and `expected` is
                None.
            SheafError: If the client reports no generation for the object it just wrote.
        """
        blob = self.bucket.blob(self._key(key))
        precondition = MUST_NOT_EXIST if expected is None else expected
        try:
            blob.upload_from_string(
                data,
                content_type='application/x-protobuf',
                if_generation_match=precondition,
            )
        except api_exceptions.PreconditionFailed as exc:
            raise errors.PreconditionFailed(f'{key}: generation {expected} is stale') from exc
        return _generation_of(blob)

    @override
    def history_mutable(self, key: str) -> list[backend.StoredBlob]:
        """Return retained generations of `key`, newest first.

        Needs object versioning on the bucket; without it only the live generation comes back.

        Raises:
            SheafError: If the client reports no generation for a listed version.
        """
        full = self._key(key)
        versions = [
            (b, _generation_of(b)) for b in self.bucket.list_blobs(prefix=full, versions=True) if b.name == full
        ]
        versions.sort(key=lambda pair: pair[1], reverse=True)
        return [
            backend.StoredBlob(
                data=blob.download_as_bytes(if_generation_match=generation),
                generation=generation,
            )
            for blob, generation in versions
        ]

    @override
    def put_immutable(self, key: str, data: bytes) -> None:
        """Upload an immutable object unless one is already at `key`.

        `if_generation_match=0` is GCS's create-if-absent; the failed precondition is the existing
        object, which content addressing makes identical. One request, and the precondition puts the
        upload under the client's default retry policy.
        """
        try:
            self.bucket.blob(self._key(key)).upload_from_string(
                data, content_type='application/x-git-packed-objects', if_generation_match=0
            )
        except api_exceptions.PreconditionFailed:
            return

    @override
    def get_immutable(self, key: str) -> bytes:
        """Download an immutable object.

        Raises:
            NotFound: If the object is absent.
        """
        blob = self.bucket.blob(self._key(key))
        try:
            return blob.download_as_bytes()
        except api_exceptions.NotFound as exc:
            raise errors.NotFound(key) from exc

    @override
    def list_immutable(self, prefix: str) -> Iterator[backend.ObjectInfo]:
        """Enumerate immutable objects under `prefix`."""
        offset = len(self.prefix) + 1 if self.prefix else 0
        for blob in self.bucket.list_blobs(prefix=self._key(prefix)):
            yield backend.ObjectInfo(key=blob.name[offset:], size=_size_of(blob))
