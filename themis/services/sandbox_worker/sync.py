"""Restore and checkpoint ``/workspace`` (sandbox-worker.md, "The workspace is a repository").

``/workspace`` is the Analysis repository, and the agent's: restore is a guest-side clone (`guest_git`), the agent
commits and pushes as it sees fit, and the worker's part at teardown is one guest-side push of every branch, so a
commit the agent made and forgot to push survives. The working document is an ordinary file in that repository,
committed and pushed like anything else. The worker also keeps it checkpointed through the store rpc — when a
sandboxed command returns, and once more at teardown — for the reader that still takes the document from the store
rather than from git; the checkpoint is a copy of the tree's file, never the other way round. At restore the
repository's copy wins: the store's is written into the working tree only when the clone left no document, and fails
closed — any store error but a positive ``NOT_FOUND`` fails the spawn, so a served turn never mints a version over a
blank restore. Both restores are fail-closed.

The document is read and written through a postern :class:`~postern.Workspace`, the reference-closed accessor:
every access resolves one component at a time under ``O_NOFOLLOW``, so a symlink or special the guest planted
(``working_document.md`` → ``/proc/self/environ``, say) is never followed out of the tree in the trusted worker.
Concurrent checkpoints are serialized, and a document unchanged since its last write mints no new version.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import stat
from collections.abc import Callable

import postern

from themis.services.sandbox_worker import guest_git, store_client

_WORKING_DOCUMENT_NAME = 'working_document.md'
# Where the SDK downloads the session agent's skills, each spawn.
_SKILLS_DIRNAME = 'skills'

_logger = logging.getLogger(__name__)


class WorkspaceSync:
    """Owns the document round-trip with the store and the repository's guest-side restore and teardown push."""

    def __init__(
        self,
        store: store_client.Store,
        *,
        accessor: postern.Workspace,
        repository: guest_git.GuestGit,
        document_name: str = _WORKING_DOCUMENT_NAME,
    ) -> None:
        self._store = store
        self._accessor = accessor
        self._repository = repository
        self._document_name = document_name
        self._checkpoint_lock = asyncio.Lock()
        self._last_document: str | None = None

    async def restore(self) -> None:
        """Clone the repository into ``/workspace``, then the working document where the clone left none.

        The clone comes first because it needs an empty working tree. The repository's document, when
        it tracks one, is the one the session works on; the store's checkpoint is written into the tree
        only when the repository has no document yet — as an untracked file, for the agent to commit.
        Either way the checkpoint is read, and fails closed: what the store holds is the baseline the
        next checkpoint is compared against, so a tree that disagrees with it is checkpointed on the
        first command.

        Raises:
            SheafError: If the repository's store cannot be read or is corrupt.
            guest_git.GitError: If a guest git command fails.
            Exception: Any store failure resolving the working document other than a positive
                NOT_FOUND — the spawn must fail rather than boot onto a blank document.
        """
        await asyncio.to_thread(self._repository.hydrate)
        self._clear_planted(_SKILLS_DIRNAME, stat.S_ISDIR)
        self._clear_planted(self._document_name, stat.S_ISREG)
        checkpointed = await self._store.get_working_document()
        document_path = self._accessor / self._document_name
        if not document_path.is_file():
            if checkpointed is not None:
                document_path.write_text(checkpointed)
        elif checkpointed is not None and checkpointed.encode('utf-8') != document_path.read_bytes():
            _logger.warning(
                "the repository's working document differs from the store's checkpoint; the repository's stands"
                ' and the store is brought up to it on the first command'
            )
        self._last_document = checkpointed

    def _clear_planted(self, name: str, expected: Callable[[int], bool]) -> None:
        """Remove what the clone put at a top-level path the worker writes to, unless it is the expected kind.

        The SDK resolves `skills` before it writes there, and the confined document write would ELOOP
        on a symlink and wedge every later spawn; so anything but the expected kind is cleared rather
        than left for the worker to follow. A direct child of the root, handled without following it.
        """
        path = self._accessor.host_root / name
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if expected(mode):
            return
        _logger.warning('the clone put an unexpected entry at %s; removing it', name)
        if stat.S_ISDIR(mode):
            shutil.rmtree(path)
        else:
            path.unlink()

    async def checkpoint(self) -> None:
        """Snapshot the working document to the store — a new version only if it changed."""
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

    async def teardown(self, session_id: str) -> None:
        """The session's last word: checkpoint the document, then push what the agent committed and left unpushed.

        Args:
            session_id: Names the stranded refs should the push be refused (`guest_git.GuestGit.push_all`).

        Raises:
            guest_git.GitError: If neither the push nor the stranded fallback lands.
            Exception: Whatever the checkpoint raised, after the push has been attempted — the two are
                independent, and a store outage must not cost the commits the push exists to save.
        """
        checkpoint_failure: Exception | None = None
        try:
            await self.checkpoint()
        except Exception as exc:
            _logger.exception('the last checkpoint failed; pushing the repository regardless')
            checkpoint_failure = exc
        await asyncio.to_thread(self._repository.push_all, session_id)
        if checkpoint_failure is not None:
            raise checkpoint_failure
