"""The storage seam: the object-store operations sheaf needs.

Implement this to put a sheaf repository on a new storage system. Design:
`docs/design/sheaf.md`.
"""

from __future__ import annotations

import abc
import dataclasses
from collections.abc import Iterator

# An opaque version token for a mutable key. Compare it for equality and hand it back; it is
# neither dense nor ordered (a GCS generation is a microsecond timestamp).
Generation = int


@dataclasses.dataclass(frozen=True)
class StoredBlob:
    """The contents of a mutable key at one generation."""

    data: bytes
    generation: Generation


@dataclasses.dataclass(frozen=True)
class ObjectInfo:
    """Metadata for one immutable object, as returned by a listing."""

    key: str
    size: int
    created_at: float


class Backend(abc.ABC):
    """Object-store operations sheaf depends on.

    Two namespaces: one mutable key per repository, updated only by compare-and-swap, and an
    immutable content-addressed namespace for packfiles.
    """

    @abc.abstractmethod
    def get_mutable(self, key: str) -> StoredBlob:
        """Read the current value and generation of `key`.

        Raises:
            NotFound: If the key has never been written.
        """

    @abc.abstractmethod
    def cas_mutable(self, key: str, data: bytes, expected: Generation | None) -> Generation:
        """Replace `key` only if its generation is still `expected`.

        Args:
            key: The mutable key.
            data: New contents.
            expected: Generation the caller last observed, or None to require that the key not
                exist.

        Returns:
            The generation of the value just written.

        Raises:
            PreconditionFailed: If the generation moved. Not a fault: this is how concurrent
                writers are serialised.
            SheafError: If the backend cannot report the generation it just wrote, which leaves the
                caller with no token to publish against.
        """

    @abc.abstractmethod
    def history_mutable(self, key: str) -> list[StoredBlob]:
        """Return the retained generations of `key`, newest first.

        A backend that retains nothing returns the live generation alone, so a caller must treat
        the result as an audit convenience and never as a correctness input.

        Raises:
            SheafError: If the backend cannot report a retained generation's version token.
        """

    @abc.abstractmethod
    def put_immutable(self, key: str, data: bytes) -> None:
        """Write `data` at `key`, leaving an existing object untouched.

        Keys are content-addressed, so a concurrent write of the same key carries the same bytes
        and there is nothing to overwrite. An implementation must not refresh the object either:
        `list_immutable` reports a creation time, garbage collection prices its grace window on it,
        and a backend that reset it on every re-put would give the two backends different sweep
        behaviour on the one path that destroys data.
        """

    @abc.abstractmethod
    def get_immutable(self, key: str) -> bytes:
        """Read an immutable object.

        Raises:
            NotFound: If the object is absent.
        """

    @abc.abstractmethod
    def list_immutable(self, prefix: str) -> Iterator[ObjectInfo]:
        """Enumerate immutable objects under `prefix`."""

    @abc.abstractmethod
    def delete_immutable(self, key: str) -> None:
        """Remove an immutable object."""
