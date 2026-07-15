"""Restore and checkpoint ``/workspace`` through the store (self-hosted-sandbox.md §9).

Restore runs before the agent starts; checkpoint runs on the ``end_turn`` idle. The working document
is fail-closed (any store error but a positive ``NOT_FOUND`` fails the spawn, so a served turn never
mints a version over a blank restore); the ephemeral scratch is fail-open (empty on any error — the
next checkpoint overwrites it). The checkpoint writes the document first, then the scratch.

Concurrent checkpoints (the end_turn task and the SIGTERM backstop can overlap in the release grace)
are serialized, and a document unchanged since its last write mints no new version — so a no-op turn,
or a backstop after a completed checkpoint, does not inflate the version history.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib

from themis.services.proxy import store_client, workspace

_logger = logging.getLogger(__name__)


class WorkspaceSync:
    """Owns the document + scratch round-trip between ``/workspace`` and the store."""

    def __init__(self, store: store_client.Store, *, root: pathlib.Path, document_path: pathlib.Path) -> None:
        self._store = store
        self._root = root
        self._document_path = document_path
        self._checkpoint_lock = asyncio.Lock()
        self._last_document: str | None = None

    async def restore(self) -> None:
        """Restore the working document (fail-closed) then the scratch (fail-open) into ``/workspace``.

        Raises:
            Exception: Any store failure resolving the working document other than a positive
                NOT_FOUND — the spawn must fail rather than boot onto a blank document.
        """
        document = await self._store.get_working_document()  # non-NOT_FOUND errors propagate: fail the spawn
        if document is not None:
            self._document_path.parent.mkdir(parents=True, exist_ok=True)
            self._document_path.write_text(document)
        self._last_document = document  # a turn that leaves the document unedited then mints no duplicate
        await self._restore_scratch()

    async def _restore_scratch(self) -> None:
        try:
            archive = await self._store.get_workspace()
            if archive:
                workspace.unpack(archive, self._root)
        except Exception:  # scratch fails open to empty on any error (§9); the next checkpoint overwrites it
            _logger.exception('scratch restore failed; continuing with empty scratch')

    async def checkpoint(self) -> None:
        """Snapshot the durable document (a new version only if it changed) then the scratch to the store."""
        async with self._checkpoint_lock:
            if self._document_path.exists():
                document = self._document_path.read_text()
                if document != self._last_document:
                    await self._store.put_working_document(document)
                    self._last_document = document
            await self._store.put_workspace(workspace.pack(self._root, exclude={self._document_path}))
