"""A local-directory backend, implementing the compare-and-swap contract without a network.

Generations are held in filenames and every one is retained, which mirrors a versioning-enabled
bucket down to the history that stands in for a server-side reflog. Design:
`docs/design/sheaf.md`.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import urllib.parse
from collections.abc import Iterator
from typing import override

from themis.sheaf import backend, errors


class LocalBackend(backend.Backend):
    """Object-store semantics over a directory tree."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = pathlib.Path(root)
        self._mutable = self.root / 'mutable'
        self._immutable = self.root / 'immutable'
        self._mutable.mkdir(parents=True, exist_ok=True)
        self._immutable.mkdir(parents=True, exist_ok=True)

    def _mutable_dir(self, key: str) -> pathlib.Path:
        return self._mutable / urllib.parse.quote(key, safe='')

    def _generations(self, key: str) -> list[int]:
        directory = self._mutable_dir(key)
        if not directory.is_dir():
            return []
        return sorted(int(p.name) for p in directory.iterdir() if p.name.isdigit())

    def _immutable_path(self, key: str) -> pathlib.Path:
        return self._immutable / urllib.parse.quote(key, safe='')

    @override
    def get_mutable(self, key: str) -> backend.StoredBlob:
        """Read the newest generation of `key`.

        Raises:
            NotFound: If the key has never been written.
        """
        generations = self._generations(key)
        if not generations:
            raise errors.NotFound(key)
        newest = generations[-1]
        path = self._mutable_dir(key) / str(newest)
        return backend.StoredBlob(data=path.read_bytes(), generation=newest)

    @override
    def cas_mutable(self, key: str, data: bytes, expected: backend.Generation | None) -> backend.Generation:
        """Write the next generation of `key`, but only if `expected` is still current.

        Raises:
            PreconditionFailed: If the generation moved, or another writer took the next one.
        """
        directory = self._mutable_dir(key)
        directory.mkdir(parents=True, exist_ok=True)
        current = self._generations(key)
        observed = current[-1] if current else None
        if observed != expected:
            raise errors.PreconditionFailed(f'{key}: generation {expected} is stale (now {observed})')
        target = directory / str((expected or 0) + 1)
        handle, temp = tempfile.mkstemp(dir=directory, prefix='.tmp-')
        try:
            with os.fdopen(handle, 'wb') as fh:
                fh.write(data)
            try:
                # `os.link` fails with EEXIST rather than clobbering, and publishes the full
                # contents in one step: creating the target with O_EXCL and writing into it
                # afterwards would leave a window where a reader sees the newest generation empty.
                os.link(temp, target)
            except FileExistsError as exc:
                raise errors.PreconditionFailed(f'{key}: generation {expected} was taken by another writer') from exc
        finally:
            pathlib.Path(temp).unlink(missing_ok=True)
        return int(target.name)

    @override
    def history_mutable(self, key: str) -> list[backend.StoredBlob]:
        """Return every retained generation of `key`, newest first."""
        directory = self._mutable_dir(key)
        blobs = []
        for generation in reversed(self._generations(key)):
            path = directory / str(generation)
            blobs.append(backend.StoredBlob(data=path.read_bytes(), generation=generation))
        return blobs

    @override
    def put_immutable(self, key: str, data: bytes) -> None:
        """Write an immutable object, leaving an existing one untouched."""
        path = self._immutable_path(key)
        # Present means done — the key is the hash of these bytes. Rewriting would move the mtime
        # `list_immutable` reports as the creation time, which is the age a sweep judges.
        if path.exists():
            return
        handle, temp = tempfile.mkstemp(dir=self._immutable, prefix='.tmp-')
        with os.fdopen(handle, 'wb') as fh:
            fh.write(data)
        os.replace(temp, path)

    @override
    def get_immutable(self, key: str) -> bytes:
        """Read an immutable object.

        Raises:
            NotFound: If the object is absent.
        """
        path = self._immutable_path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise errors.NotFound(key) from exc

    @override
    def list_immutable(self, prefix: str) -> Iterator[backend.ObjectInfo]:
        """Enumerate immutable objects whose key starts with `prefix`."""
        for path in sorted(self._immutable.iterdir()):
            if path.name.startswith('.tmp-'):
                continue
            key = urllib.parse.unquote(path.name)
            if not key.startswith(prefix):
                continue
            stat = path.stat()
            yield backend.ObjectInfo(key=key, size=stat.st_size, created_at=stat.st_mtime)

    @override
    def delete_immutable(self, key: str) -> None:
        """Remove an immutable object."""
        self._immutable_path(key).unlink(missing_ok=True)
