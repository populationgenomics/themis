"""Restore ``/workspace`` from the Analysis repository, and push it back at teardown (sandbox-worker.md).

``/workspace`` is the Analysis repository, and the agent's: restore is a guest-side clone (`guest_git`), the agent
commits and pushes as it sees fit, and the worker's part at teardown is one guest-side push of every branch, so a
commit the agent made and forgot to push survives. The working document is an ordinary file in that repository,
committed and pushed like anything else; the worker keeps no copy of it. Restore is fail-closed: the repository is the
deliverable, so a clone that fails fails the spawn.

Between the clone and the session the worker clears what the clone put at ``skills`` unless it is a directory: the SDK
writes there as the worker. ``skills`` is a direct child of the workspace root, a host path the worker chose, so
``lstat`` and ``unlink`` on it never follow a symlink the guest planted there: neither follows the final component.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import stat

from themis.services.sandbox_worker import guest_git

# Where the SDK downloads the session agent's skills, each spawn.
_SKILLS_DIRNAME = 'skills'

_logger = logging.getLogger(__name__)


class WorkspaceSync:
    """Owns the repository's guest-side restore into ``/workspace`` and its teardown push."""

    def __init__(self, *, workspace: pathlib.Path, repository: guest_git.GuestGit) -> None:
        self._workspace = workspace
        self._repository = repository

    async def restore(self) -> None:
        """Clone the repository into ``/workspace``, then clear a non-directory the clone left at ``skills``.

        Raises:
            SheafError: If the repository's store cannot be read or is corrupt.
            guest_git.GitError: If a guest git command fails.
        """
        await asyncio.to_thread(self._repository.hydrate)
        self._clear_planted_skills()

    def _clear_planted_skills(self) -> None:
        """Remove what the clone put at ``skills``, unless it is a directory.

        The SDK resolves ``skills`` before it writes there as the worker, so a symlink the agent
        committed there would redirect the worker's write; anything but a directory is cleared rather
        than left for the worker to follow. A direct child of the root, handled without following it.
        """
        path = self._workspace / _SKILLS_DIRNAME
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISDIR(mode):
            return
        _logger.warning('the clone put a non-directory at %s; removing it', _SKILLS_DIRNAME)
        path.unlink()

    async def teardown(self, session_id: str) -> None:
        """The session's last word: push what the agent committed and left unpushed.

        Args:
            session_id: Names the stranded refs should the push be refused (`guest_git.GuestGit.push_all`).

        Raises:
            guest_git.GitError: If neither the push nor the stranded fallback lands.
        """
        await asyncio.to_thread(self._repository.push_all, session_id)
