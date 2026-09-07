"""The worker's own git commands against `/workspace`, every one run inside the guest.

The guest owns `/workspace`, `.git` included, so a `git` the trusted worker ran there would execute whatever
`core.hooksPath` or a clean filter pointed at, in the process holding the credential (sheaf-changeover.md). So the
worker never runs git host-side against the working tree: hydration is a guest-side `git clone` over the
upload-pack hatch, and the teardown push is a guest-side `git push` over the receive-pack hatch, with a fetch over the
upload-pack hatch to tell what the store still lacks. The worker makes
no commits on the agent's behalf; its one commit is the `.gitignore` that seeds an otherwise empty repository, made
before the agent runs.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

import postern

from themis.services.sandbox_worker import git_hatches

GITIGNORE = 'scratch/\nskills/\n'
STRANDED_NAMESPACE = 'refs/stranded'
_GITIGNORE_CODE = f'import pathlib\npathlib.Path(".gitignore").write_text({GITIGNORE!r})\n'
_REF_COMPONENT = re.compile(r'^[A-Za-z0-9_-]+$')

_logger = logging.getLogger(__name__)


class Guest(Protocol):
    """What this module needs of a sandbox: `postern.Sandbox`'s two entry points, cwd `/workspace`."""

    def run(self, argv: list[str], *, timeout: float = 60) -> postern.ProcResult: ...

    def run_python(self, code: str, *, timeout: float = 60) -> postern.ProcResult: ...


class GitError(RuntimeError):
    """A command the worker ran in the guest failed; the message carries its stderr."""


class GuestGit:
    """The Analysis repository as the guest sees it: cloned from, and pushed to, the hatches."""

    def __init__(
        self,
        guest: Guest,
        hatches: git_hatches.GitHatches,
        *,
        fetch_url: str,
        push_url: str,
        hydrate_timeout: float,
        teardown_timeout: float,
    ) -> None:
        """Bind the guest to its repository.

        Args:
            guest: The sandbox; its cwd is the working tree.
            hatches: The mirror's two doors, synced before the clone so the store is read once
                in the worker's own thread and a store fault surfaces here rather than as a git
                error the guest reported.
            fetch_url: The guest's `ext::` URL for the upload-pack hatch.
            push_url: The guest's `ext::` URL for the receive-pack hatch.
            hydrate_timeout: Seconds one guest command of the restore may take; the restore runs before
                the work item is acked, so this bounds how long the item stays reclaimable.
            teardown_timeout: Seconds one guest command of the teardown may take; a push's wall time
                includes the hook's pack upload to the store.
        """
        self._guest = guest
        self._hatches = hatches
        self._fetch_url = fetch_url
        self._push_url = push_url
        self._hydrate_timeout = hydrate_timeout
        self._teardown_timeout = teardown_timeout

    def hydrate(self) -> None:
        """Clone the repository into the guest's `/workspace`, seeding an empty one with its first commit.

        Fail-closed: the repository is the deliverable, so any failure propagates and the spawn does
        not serve. The working tree has to be empty — the clone runs before anything else lands in it.

        Raises:
            SheafError: If the store cannot be read, or names a pack it no longer holds.
            GitError: If a guest command fails; the message carries git's stderr.
        """
        timeout = self._hydrate_timeout
        snapshot = self._hatches.sync()
        self._git('clone', self._fetch_url, '.', timeout=timeout)
        self._git('remote', 'set-url', '--push', 'origin', self._push_url, timeout=timeout)
        # Decided from the clone, not the snapshot: the clone's own sync may have picked up another
        # session's first commit in between.
        if not self._guest.run(['git', 'rev-parse', '-q', '--verify', 'HEAD'], timeout=timeout).ok:
            self._first_commit(timeout)
        _logger.info('hydrated /workspace (%d refs)', len(snapshot.refs))

    def _first_commit(self, timeout: float) -> None:
        written = self._guest.run_python(_GITIGNORE_CODE, timeout=timeout)
        if not written.ok:
            raise GitError(f'writing .gitignore in the guest failed: {written.stderr.strip()}')
        self._git('add', '.gitignore', timeout=timeout)
        self._git('commit', '-q', '-m', 'Ignore scratch and skills', timeout=timeout)
        self._git('push', '-q', 'origin', 'HEAD', timeout=timeout)

    def push_all(self, session_id: str) -> None:
        """Publish every branch; what the store refuses is preserved under `refs/stranded/<session-id>/`.

        The hook refuses a push whole, so after a refused `--all` each branch with unpublished commits
        is pushed on its own first — one refused sibling must not demote a clean branch — and only a
        branch refused on its own is stranded, as is a detached HEAD with unpublished commits (as
        `HEAD`). Creating a ref is always allowed, so a stranded tip lands whenever the store is
        reachable and its commits write no protected path. A branch that merely fell behind holds
        nothing the store lacks and leaves no ref.

        Args:
            session_id: Names the stranded refs, so a later session can find this one's work.

        Raises:
            ValueError: If `session_id` cannot be a ref name component.
            GitError: If the fetch that tells what the store lacks fails, or a tip is refused both as
                itself and stranded; that tip's unpublished work is then lost.
        """
        if not _REF_COMPONENT.match(session_id):
            raise ValueError(f'session id {session_id!r} cannot name a ref')
        timeout = self._teardown_timeout
        pushed = self._guest.run(['git', 'push', 'origin', '--all'], timeout=timeout)
        if pushed.ok:
            _logger.info('teardown push: %s', pushed.stderr.strip())
        else:
            _logger.warning('teardown push refused: %s', pushed.stderr.strip())
        fetched = self._guest.run(['git', 'fetch', '-q', 'origin'], timeout=timeout)
        if not fetched.ok:
            raise GitError(f'cannot tell what the store lacks, so nothing is stranded: {fetched.stderr.strip()}')
        stranded = f'{STRANDED_NAMESPACE}/{session_id}'
        lost = []
        for tip in self._unpublished_tips(timeout):
            if tip == 'HEAD':
                # Last, and re-checked: a branch push above may just have landed the commit it sits on.
                if not self._unpublished(tip, timeout):
                    continue
            else:
                alone = self._guest.run(['git', 'push', '-q', 'origin', f'{tip}:{tip}'], timeout=timeout)
                if alone.ok:
                    _logger.info('%s landed on its own', tip)
                    continue
                _logger.warning('%s refused on its own: %s', tip, alone.stderr.strip())
            name = tip.removeprefix('refs/heads/')
            result = self._guest.run(['git', 'push', 'origin', f'{tip}:{stranded}/{name}'], timeout=timeout)
            if result.ok:
                _logger.warning('unpublished %s preserved as %s/%s', tip, stranded, name)
            else:
                _logger.error('unpublished %s could not be stranded: %s', tip, result.stderr.strip())
                lost.append(tip)
        if lost:
            raise GitError(f'unpublished work on {lost} was refused twice and is lost with this session')

    def _unpublished_tips(self, timeout: float) -> list[str]:
        """Every local branch, then `HEAD` when detached, whose history origin does not have."""
        branches = self._git('for-each-ref', '--format=%(refname)', 'refs/heads/', timeout=timeout).stdout.split()
        detached = not self._guest.run(['git', 'symbolic-ref', '-q', 'HEAD'], timeout=timeout).ok
        return [tip for tip in [*branches, *(['HEAD'] if detached else [])] if self._unpublished(tip, timeout)]

    def _unpublished(self, tip: str, timeout: float) -> bool:
        return bool(self._git('rev-list', '-n', '1', tip, '--not', '--remotes=origin', timeout=timeout).stdout.strip())

    def _git(self, *args: str, timeout: float) -> postern.ProcResult:
        result = self._guest.run(['git', *args], timeout=timeout)
        if not result.ok:
            raise GitError(f'git {" ".join(args)} failed in the guest: {result.stderr.strip()}')
        return result
