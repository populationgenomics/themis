"""Restore and checkpoint ``/workspace`` through the store (postern-sandbox-swap.md §4).

Restore runs before the first ``run_python``; checkpoint runs on each turn boundary and at session end. The working
document is fail-closed (any store error but a positive ``NOT_FOUND`` fails the spawn, so a served turn never mints a
version over a blank restore); the ephemeral scratch is fail-open (empty on any error — the next checkpoint overwrites
it). The checkpoint writes the document first, then the scratch.

All ``/workspace`` access goes through a postern :class:`~postern.Workspace`, the reference-closed accessor: every
read, write, pack, and extract resolves one component at a time under ``O_NOFOLLOW``, so a symlink/``..``/special the
guest planted (e.g. ``document.md`` → ``/proc/self/environ``) is never followed out of the tree in the trusted worker.

Concurrent checkpoints are serialized, and a document unchanged since its last write mints no new version — so a no-op
turn does not inflate the version history. ``exclude`` names top-level entries pruned from the scratch snapshot: the
working document (persisted separately) and paths re-materialised each spawn — the SDK re-downloads its skills into
``/workspace/skills`` every session, so a stale copy must not persist in a checkpoint.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Iterable

import postern

from themis.services.sandbox_worker import store_client

_DOCUMENT_NAME = 'document.md'
_MAX_ENTRIES = 20_000
_MAX_TOTAL_BYTES = 512 * 1024 * 1024  # 512 MiB

_logger = logging.getLogger(__name__)


class WorkspaceSync:
    """Owns the document + scratch round-trip between ``/workspace`` and the store, via the confined accessor."""

    def __init__(
        self,
        store: store_client.Store,
        *,
        accessor: postern.Workspace,
        document_name: str = _DOCUMENT_NAME,
        exclude: Iterable[str] = (),
    ) -> None:
        self._store = store
        self._accessor = accessor
        self._document_name = document_name
        self._excluded_top = frozenset({document_name, *exclude})
        self._checkpoint_lock = asyncio.Lock()
        self._last_document: str | None = None

    def _exclude(self, rel: str) -> bool:
        """Whether a workspace-relative path is pruned from the scratch pack (by top-level entry)."""
        return rel.split('/', 1)[0] in self._excluded_top

    async def restore(self) -> None:
        """Restore the working document (fail-closed) then the scratch (fail-open) into ``/workspace``.

        Raises:
            Exception: Any store failure resolving the working document other than a positive
                NOT_FOUND — the spawn must fail rather than boot onto a blank document.
        """
        document = await self._store.get_working_document()
        if document is not None:
            (self._accessor / self._document_name).write_text(document)
        self._last_document = document
        await self._restore_scratch()

    async def _restore_scratch(self) -> None:
        try:
            archive = await self._store.get_workspace()
            if archive:
                report = self._accessor.restore_tar(
                    io.BytesIO(archive), max_entries=_MAX_ENTRIES, max_bytes=_MAX_TOTAL_BYTES
                )
                if not report.ok:
                    _logger.warning(
                        'scratch restore neutralized %d unsafe entries: %s', len(report.skipped), report.skipped
                    )
        except Exception:
            _logger.exception('scratch restore failed; continuing with empty scratch')

    async def checkpoint(self) -> None:
        """Snapshot the durable document (a new version only if it changed) then the scratch to the store."""
        async with self._checkpoint_lock:
            document_path = self._accessor / self._document_name
            if document_path.is_file():
                document = document_path.read_text()
                if document != self._last_document:
                    await self._store.put_working_document(document)
                    self._last_document = document
            elif document_path.exists():
                # A non-regular document (guest replaced it with a symlink/special) is skipped, never
                # dereferenced — the confined read would ELOOP anyway, but skip loudly and leave the store version.
                _logger.warning('working document is not a regular file; not checkpointing it this turn')
            buffer = io.BytesIO()
            report = self._accessor.pack_tar(buffer, exclude=self._exclude)
            if not report.ok:
                _logger.warning('checkpoint neutralized %d unsafe entries: %s', len(report.skipped), report.skipped)
            await self._store.put_workspace(buffer.getvalue())
