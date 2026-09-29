"""Whether an asset file is one a push carries: tracked in the repository's index, as a regular file, and not ignored.

An ``::embed`` resolves in the revision the browser reads, which holds exactly what was committed, so a file on
disk that git does not track, or ignores, draws nowhere. Both questions are git's, asked of the repository holding
the given directory.
"""

from __future__ import annotations

import pathlib
import subprocess

# The modes of a regular file in a git tree; a symbolic link (120000) or a submodule (160000) is not an asset.
REGULAR_FILE_MODES = frozenset({'100644', '100755'})
_TIMEOUT_S = 30


class TrackingError(RuntimeError):
    """git could not answer, as when the directory is in no repository."""


def _git(directory: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ['git', '-C', str(directory), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT_S,
    )


def ignored(directory: pathlib.Path, path: str) -> bool:
    """Whether git ignores ``path``, relative to ``directory``.

    Raises:
        TrackingError: If git cannot tell, as when ``directory`` is in no repository.
    """
    result = _git(directory, 'check-ignore', '--quiet', '--', path)
    if result.returncode not in (0, 1):
        raise TrackingError(f'git check-ignore failed in {directory}: {result.stderr.strip()}')
    return result.returncode == 0


def index_mode(directory: pathlib.Path, path: str) -> str | None:
    """The mode the index records for ``path``, relative to ``directory``, or None when git does not track it.

    Raises:
        TrackingError: If git cannot tell, as when ``directory`` is in no repository.
    """
    result = _git(directory, 'ls-files', '--stage', '--', path)
    if result.returncode != 0:
        raise TrackingError(f'git ls-files failed in {directory}: {result.stderr.strip()}')
    entries = [line for line in result.stdout.splitlines() if line]
    if not entries:
        return None
    if len(entries) != 1 or not entries[0].endswith(f'\t{path}'):
        raise TrackingError(f'git ls-files names more than {path} for it: {entries}')
    return entries[0].split(' ', 1)[0]


def _revision(directory: pathlib.Path, revision: str) -> str | None:
    """The commit `revision` names, or None when it names none; raises when `directory` is in no repository."""
    found = _git(directory, 'rev-parse', '--verify', '--quiet', f'{revision}^{{commit}}')
    if found.returncode == 0:
        return found.stdout.strip()
    if _git(directory, 'rev-parse', '--git-dir').returncode != 0:
        raise TrackingError(f'{directory} is in no git repository')
    return None


def upstream(directory: pathlib.Path) -> str | None:
    """The commit the current branch's upstream stood at when last fetched, or None when it has none.

    Raises:
        TrackingError: If `directory` is in no repository.
    """
    return _revision(directory, '@{upstream}')


def git(directory: pathlib.Path, *args: str, stdin: bytes | None = None) -> bytes:
    """What git writes to stdout for `args`, run in the repository holding `directory`.

    Raises:
        TrackingError: If git exits non-zero, as when `directory` is in no repository.
    """
    result = subprocess.run(  # noqa: S603
        ['git', '-C', str(directory), *args],  # noqa: S607
        input=stdin,
        capture_output=True,
        check=False,
        timeout=_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise TrackingError(f'git {args[0]} failed in {directory}: {result.stderr.decode(errors="replace").strip()}')
    return result.stdout


def unpushed(directory: pathlib.Path) -> list[tuple[str, list[str]]]:
    """Each commit a push of the current branch carries, oldest first, with its parents.

    Those reachable from HEAD and not from the upstream as last fetched; none when the branch has no upstream, as on
    a detached HEAD, or no commit yet.

    Raises:
        TrackingError: If `directory` is in no repository.
    """
    pushed = upstream(directory)
    if pushed is None or _revision(directory, 'HEAD') is None:
        return []
    listed = git(directory, 'rev-list', '--reverse', '--parents', f'{pushed}..HEAD').decode()
    return [(commit, parents) for commit, *parents in (line.split() for line in listed.splitlines())]


def merging(directory: pathlib.Path) -> bool:
    """Whether a merge is in progress in the repository holding `directory`.

    Raises:
        TrackingError: If `directory` is in no repository.
    """
    return _revision(directory, 'MERGE_HEAD') is not None


def committed(directory: pathlib.Path, path: str, revision: str) -> bytes | None:
    """The bytes of the regular file at `path`, relative to `directory`, in the commit `revision` names.

    None when that commit holds no regular file there, or `revision` names no commit (a branch with none yet).

    Raises:
        TrackingError: If git cannot tell, as when `directory` is in no repository.
    """
    commit = _revision(directory, revision)
    if commit is None:
        return None
    listed = _git(directory, 'ls-tree', '-z', commit, '--', path)
    if listed.returncode != 0:
        raise TrackingError(f'git ls-tree failed in {directory}: {listed.stderr.strip()}')
    entry = listed.stdout.split('\x00')[0]
    if not entry or entry.split(' ', 1)[0] not in REGULAR_FILE_MODES:
        return None
    blob = subprocess.run(  # noqa: S603
        ['git', '-C', str(directory), 'cat-file', 'blob', entry.split()[2]],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=_TIMEOUT_S,
    )
    if blob.returncode != 0:
        raise TrackingError(f'git cat-file failed in {directory}: {blob.stderr.decode(errors="replace").strip()}')
    return blob.stdout
