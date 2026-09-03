"""The reflog ref: one commit per publish, parented on every tip the publish set.

Nothing in a sheaf repository is ever deleted, and no ref is ever rewritten or removed, so every
commit pushed stays reachable from the ref it was pushed to. What the branch graph does not record
is which of those commits were ever a tip, and when. The reflog ref does: each publish that moves a
ref writes a commit here whose parents are the previous reflog commit and the new tips, with the
transitions in the message. A writer publishes it in the same compare-and-swap as the refs it
describes, so the two cannot disagree. Design: `docs/design/sheaf.md`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Protocol

from themis.sheaf import refdoc

REF = refdoc.REFLOG_REF
NAMESPACE = refdoc.SHEAF_NAMESPACE
# The one tree git knows without being told; written on first use so `commit-tree` can find it.
_EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
_ZERO = '0' * 40
_SUMMARY_PREFIX = 'sheaf: '
_ROOT_SUBJECT = 'sheaf: init'
_COMMITTER = {'GIT_COMMITTER_NAME': 'sheaf', 'GIT_COMMITTER_EMAIL': 'sheaf@localhost'}
_AUTHOR = {'GIT_AUTHOR_NAME': 'sheaf', 'GIT_AUTHOR_EMAIL': 'sheaf@localhost'}


class GitRunner(Protocol):
    """Run git against one object database, returning stdout; raises `RuntimeError` on failure."""

    def __call__(self, *args: str, stdin: bytes | None = None, env: Mapping[str, str] | None = None) -> bytes: ...


@dataclasses.dataclass(frozen=True)
class Transition:
    """One ref moving from `old` to `new` in a publish. `old` is None for a ref being created."""

    ref: str
    old: str | None
    new: str


def record(git: GitRunner, previous: str | None, transitions: Sequence[Transition]) -> str:
    """Write the reflog commit for one publish and return its id.

    Parents are the previous reflog commit and each new tip, deduplicated, so every commit that was
    ever a tip is reachable from `REF` and survives any repack. The tree is empty: the message is the
    record.

    On a repository's first publish a parentless root entry is written first and the real entry is
    parented on it, so the chain's first-parent walk ends on a commit sheaf wrote and never runs on
    into the pushed history — which the pushing side controls, and could shape to look like an entry.

    Args:
        git: Runs git against the object database holding the previous commit and the new tips.
        previous: The current reflog commit, or None on a repository's first publish.
        transitions: The ref moves this publish makes, in the order to record them.

    Raises:
        ValueError: If `transitions` is empty — a publish moving no ref has nothing to log.
        RuntimeError: If git cannot write the commit.
    """
    if not transitions:
        raise ValueError('a reflog entry records at least one ref transition')
    git('hash-object', '-w', '-t', 'tree', '--stdin', stdin=b'')
    if previous is None:
        previous = _commit(git, [], _ROOT_SUBJECT)
    parents = [previous]
    for sha in (t.new for t in transitions):
        if sha not in parents:
            parents.append(sha)
    summary = ', '.join(t.ref for t in transitions)
    body = ''.join(f'{t.ref} {t.old or _ZERO} {t.new}\n' for t in transitions)
    return _commit(git, parents, f'{_SUMMARY_PREFIX}{summary}\n\n{body}')


def _commit(git: GitRunner, parents: Sequence[str], message: str) -> str:
    args = ['commit-tree', _EMPTY_TREE]
    for parent in parents:
        args += ['-p', parent]
    args += ['-m', message]
    return git(*args, env={**_AUTHOR, **_COMMITTER}).decode().strip()


def read(git: GitRunner, tip: str) -> list[list[Transition]]:
    """Return every publish's transitions, newest first, walking the reflog chain from `tip`.

    The chain is self-delimiting: every entry's first parent is the previous entry, down to the
    parentless root `record` wrote first, so the walk never reaches a commit sheaf did not write.

    Raises:
        RuntimeError: If git cannot walk the chain.
        ValueError: If a commit on the chain is not one `record` wrote — the chain is sheaf's own,
            so that is damage, not something to skip past.
    """
    out = git('log', '--first-parent', '--format=%H %P%n%B%x00', tip).decode()
    entries: list[list[Transition]] = []
    for chunk in out.split('\x00'):
        if not chunk.strip():
            continue
        lines = [line for line in chunk.strip().splitlines() if line]
        # First line the commit id and its parents, second the summary, the rest one transition each.
        parents = lines[0].split()[1:]
        if not parents:
            if lines[1:] != [_ROOT_SUBJECT]:
                raise ValueError(f'the reflog chain ends on a commit sheaf did not write: {lines[0]}')
            break
        if len(lines) < 2 or not lines[1].startswith(_SUMMARY_PREFIX):
            raise ValueError(f'not a reflog entry: {lines[0]}')
        transitions = []
        for line in lines[2:]:
            parts = line.split(' ')
            if len(parts) != 3 or not parts[0].startswith('refs/'):
                raise ValueError(f'not a reflog entry line: {line!r}')
            ref, old, new = parts
            transitions.append(Transition(ref, None if old == _ZERO else old, new))
        entries.append(transitions)
    return entries
