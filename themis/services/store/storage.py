"""The store's blob-persistence port and its offline fixture (self-hosted-sandbox.md §9).

The store fronts two artifacts keyed server-side by the resolved Analysis: the working
document — an append-only sequence of immutable versions, the store the sequence authority —
and the ephemeral workspace, a single overwrite-on-put archive. The real backend is Google
Cloud Storage (``gcs.GcsStorage``); the in-memory ``FixtureStorage`` runs offline and in tests.
``version_key`` / ``next_version`` are the shared sequence logic both backends use.

The port is async so a blocking adapter (GCS) offloads without stalling the ``grpc.aio`` event
loop. The workspace crosses the port as whole ``bytes``; the servicer marshals it to and from
the RPC's ``WorkspaceChunk`` stream (streaming the wire keeps a large archive under gRPC's
per-message limit).
"""

from __future__ import annotations

import abc
from collections.abc import Iterable, Mapping
from typing import NamedTuple

# Zero-padded so a prefix listing orders numerically and the newest is the last key
# (self-hosted-sandbox.md §9); wide enough that the pad never overflows in practice.
_VERSION_WIDTH = 12


class WorkingDocument(NamedTuple):
    version: int
    markdown: str


class Storage(abc.ABC):
    @abc.abstractmethod
    async def put_working_document(self, analysis_id: str, markdown: str) -> int:
        """Append an immutable version; return the store-assigned version number."""
        ...

    @abc.abstractmethod
    async def get_working_document(self, analysis_id: str) -> WorkingDocument | None:
        """The latest version, or ``None`` when the Analysis has none yet."""
        ...

    @abc.abstractmethod
    async def put_workspace(self, analysis_id: str, archive: bytes) -> None:
        """Overwrite the Analysis's ephemeral workspace archive."""
        ...

    @abc.abstractmethod
    async def get_workspace(self, analysis_id: str) -> bytes | None:
        """The workspace archive, or ``None`` when none is stored yet."""
        ...


def version_key(analysis_id: str, version: int) -> str:
    """The blob key for one working-document version under its Analysis."""
    return f'{analysis_id}/versions/{version:0{_VERSION_WIDTH}d}'


def next_version(existing_keys: Iterable[str]) -> int:
    """The successor of the highest committed version (1 when there are none).

    Derived from the stored keys, never from restored content, so a fallback restore of an
    earlier version still mints a superseding higher number.
    """
    versions = [int(key.rsplit('/', 1)[-1]) for key in existing_keys]
    return max(versions) + 1 if versions else 1


class FixtureStorage(Storage):
    """In-memory ``Storage`` for offline runs and tests.

    Models the same invariants as the GCS backend: working-document versions are append-only
    and 1-indexed; the workspace is a single overwrite-on-put value.
    """

    def __init__(self, working_documents: Mapping[str, list[str]] | None = None) -> None:
        self._working_documents: dict[str, list[str]] = {k: list(v) for k, v in (working_documents or {}).items()}
        self._workspaces: dict[str, bytes] = {}

    async def put_working_document(self, analysis_id: str, markdown: str) -> int:
        versions = self._working_documents.setdefault(analysis_id, [])
        versions.append(markdown)
        return len(versions)

    async def get_working_document(self, analysis_id: str) -> WorkingDocument | None:
        versions = self._working_documents.get(analysis_id)
        if not versions:
            return None
        return WorkingDocument(version=len(versions), markdown=versions[-1])

    async def put_workspace(self, analysis_id: str, archive: bytes) -> None:
        self._workspaces[analysis_id] = archive

    async def get_workspace(self, analysis_id: str) -> bytes | None:
        return self._workspaces.get(analysis_id)
