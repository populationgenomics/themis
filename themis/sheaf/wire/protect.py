"""Paths the pushing side may not write, and refs it may not rewrite.

A `Protection` is built from the process environment rather than from anything in the repository, so
a push cannot relax the policy it is being checked against. Design: `docs/design/sheaf.md`.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os

from themis.sheaf import store as store_mod
from themis.sheaf.wire import bare

PATHS_ENV = 'SHEAF_PROTECTED_PATHS'
REFS_ENV = 'SHEAF_PROTECTED_REFS'
SEPARATOR = ':'


@dataclasses.dataclass(frozen=True)
class Protection:
    """Which paths are off limits, and which refs may not be rewritten.

    Patterns are `fnmatch` globs, so `*` crosses `/`: `annotations/*` covers everything beneath it.
    Empty means no protection — policy is opt-in, and the storage layer stays free of it.

    Raises:
        ValueError: If a pattern contains `SEPARATOR`. Colons are legal in POSIX paths, and the
            patterns reach the hook as one separator-joined environment variable each, so a pattern
            carrying one would arrive as two that match nothing — the protection silently gone on
            the boundary the whole fabrication defence rests on.
    """

    paths: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        splittable = sorted(p for p in (*self.paths, *self.refs) if SEPARATOR in p)
        if splittable:
            raise ValueError(f'a protection pattern may not contain {SEPARATOR!r}: {splittable}')

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Protection:
        """Read protection from the environment the server passed through.

        Args:
            env: Mapping to read instead of `os.environ`.
        """
        env = dict(os.environ if env is None else env)
        return cls(
            paths=tuple(p for p in env.get(PATHS_ENV, '').split(SEPARATOR) if p),
            refs=tuple(r for r in env.get(REFS_ENV, '').split(SEPARATOR) if r),
        )

    def as_env(self) -> dict[str, str]:
        """Render for passing to the hook."""
        return {PATHS_ENV: SEPARATOR.join(self.paths), REFS_ENV: SEPARATOR.join(self.refs)}

    @property
    def active(self) -> bool:
        """Whether anything is protected at all."""
        return bool(self.paths or self.refs)

    def guards(self, ref: str) -> bool:
        """Whether `ref` may not be rewritten."""
        return any(fnmatch.fnmatch(ref, pattern) for pattern in self.refs)

    def forbids(self, path: str) -> bool:
        """Whether `path` is off limits."""
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.paths)


def introduced_paths(repo: bare.BareRepo, commit: str) -> list[str]:
    """Paths where `commit` differs from every parent.

    For an ordinary commit this is its diff; for a merge it is `git diff-tree -c`, the combined
    diff, so content taken unchanged from one side does not count as introduced by the merge — a
    merge that brings in a protected file verbatim has to pass, or the pushing side can never merge.

    `--root` is what makes that allowance safe. Without it git prints nothing for a parentless
    commit, so an orphan root can carry a protected path unseen and a merge resolved in its favour
    inherits the same silence — neither commit differs from every parent.

    Raises:
        RuntimeError: If git cannot read the commit.
    """
    # -z, because otherwise git C-quotes any path with a byte outside printable ASCII and the
    # quoted form (leading '"') matches no glob; surrogateescape so an arbitrary-byte path still
    # reaches the glob intact.
    out = bare.git('diff-tree', '-r', '-c', '--root', '--no-commit-id', '--name-only', '-z', commit, cwd=repo.path)
    return [entry.decode('utf-8', 'surrogateescape') for entry in out.split(b'\x00') if entry]


def new_commits(repo: bare.BareRepo, tip: str) -> list[str]:
    """Commits reachable from `tip` that the store does not already have.

    `--all` is the mirror's pre-update refs, because a pre-receive hook runs before any ref moves.

    Raises:
        RuntimeError: If git cannot walk from the tip.
    """
    out = bare.git('rev-list', tip, '--not', '--all', cwd=repo.path)
    return [line for line in out.decode().splitlines() if line]


def violations(repo: bare.BareRepo, updates: dict[str, store_mod.RefUpdate], protection: Protection) -> list[str]:
    """Return human-readable reasons the push must be refused, empty if it is allowed.

    Two checks, and neither is sufficient alone: what each commit introduces at a protected path,
    and fast-forward-only on a protected ref. Without the second, a protected file need never be
    written at all — rebase the commit that added it away and force-push.

    Raises:
        RuntimeError: If git cannot walk the pushed commits, in which case nothing is decided and
            the push must not be accepted.
    """
    if not protection.active:
        return []

    reasons = []
    for ref, update in sorted(updates.items()):
        if protection.guards(ref):
            reasons.extend(_ancestry_reasons(repo, ref, update))
        if update.new is None:
            continue
        for commit in new_commits(repo, update.new):
            offending = sorted(p for p in introduced_paths(repo, commit) if protection.forbids(p))
            if offending:
                reasons.append(f'{commit[:12]} writes protected {", ".join(offending)}')
    return reasons


def _ancestry_reasons(repo: bare.BareRepo, ref: str, update: store_mod.RefUpdate) -> list[str]:
    """Refuse deleting or rewriting a protected ref."""
    if update.new is None:
        return [f'{ref} is protected and may not be deleted']
    if update.old is None:
        return []
    try:
        bare.git('merge-base', '--is-ancestor', update.old, update.new, cwd=repo.path)
    except RuntimeError as exc:
        # `--is-ancestor` exits 1 for "not an ancestor" and 128 for an unreadable object, and both
        # arrive as one RuntimeError -- so git's own message is carried through rather than
        # reporting an unreadable repository as a rewrite.
        return [f'{ref} is protected and may only fast-forward (rewriting it would drop commits): {exc}']
    return []
