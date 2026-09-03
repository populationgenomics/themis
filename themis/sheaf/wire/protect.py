"""What a push may not do: rewrite or delete history, touch sheaf's own refs, or write a protected path.

The first two hold for every repository and are not configurable. Protected paths are a `Protection`
built from the process environment rather than from anything in the repository, so a push cannot
relax the policy it is being checked against. Design: `docs/design/sheaf.md`.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os

from themis.sheaf import store as store_mod
from themis.sheaf.wire import bare, reflog

PATHS_ENV = 'SHEAF_PROTECTED_PATHS'
SEPARATOR = ':'


@dataclasses.dataclass(frozen=True)
class Protection:
    """Which paths are off limits to the pushing side.

    Patterns are `fnmatch` globs, so `*` crosses `/`: `annotations/*` covers everything beneath it.
    Empty means no protected paths — this half is opt-in, and the storage layer stays free of it.

    Raises:
        ValueError: If a pattern contains `SEPARATOR`. Colons are legal in POSIX paths, and the
            patterns reach the hook as one separator-joined environment variable each, so a pattern
            carrying one would arrive as two that match nothing — the protection silently gone on
            the boundary the whole fabrication defence rests on.
    """

    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        splittable = sorted(p for p in self.paths if SEPARATOR in p)
        if splittable:
            raise ValueError(f'a protection pattern may not contain {SEPARATOR!r}: {splittable}')

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Protection:
        """Read protection from the environment the server passed through.

        Args:
            env: Mapping to read instead of `os.environ`.
        """
        env = dict(os.environ if env is None else env)
        return cls(paths=tuple(p for p in env.get(PATHS_ENV, '').split(SEPARATOR) if p))

    def as_env(self) -> dict[str, str]:
        """Render for passing to the hook."""
        return {PATHS_ENV: SEPARATOR.join(self.paths)}

    @property
    def active(self) -> bool:
        """Whether any path is protected."""
        return bool(self.paths)

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

    History first, for every ref: no deletion, no rewrite, and nothing under sheaf's own namespace.
    These are what make the store append-only, and they are the hook's to enforce rather than
    receive-pack's — `receive.denyNonFastForwards` and `receive.denyDeletes` are checked *after*
    the pre-receive hook, and only for branches, so relying on them would publish the rewrite before
    git refused it. Then protected paths: what each commit introduces at one, compared against every
    parent so a merge taking the other side's edit verbatim passes.

    Raises:
        RuntimeError: If git cannot walk the pushed commits, in which case nothing is decided and
            the push must not be accepted.
    """
    reasons = []
    for ref, update in sorted(updates.items()):
        reasons.extend(history_reasons(repo, ref, update))
        if update.new is None or not protection.active:
            continue
        for commit in new_commits(repo, update.new):
            offending = sorted(p for p in introduced_paths(repo, commit) if protection.forbids(p))
            if offending:
                reasons.append(f'{commit[:12]} writes protected {", ".join(offending)}')
    return reasons


def history_reasons(repo: bare.BareRepo, ref: str, update: store_mod.RefUpdate) -> list[str]:
    """Refuse deleting or rewriting any ref, and any write under sheaf's own namespace."""
    if ref.startswith(reflog.NAMESPACE):
        return [f'{ref} is written by sheaf, not by a push']
    if update.new is None:
        return [f'{ref} may not be deleted: history here is append-only']
    if update.old is None:
        return []
    try:
        bare.git('merge-base', '--is-ancestor', update.old, update.new, cwd=repo.path)
    except RuntimeError as exc:
        # `--is-ancestor` exits 1 for "not an ancestor" and 128 for an unreadable object, and both
        # arrive as one RuntimeError -- so git's own message is carried through rather than
        # reporting an unreadable repository as a rewrite.
        return [f'{ref} may only fast-forward (rewriting it would drop commits): {exc}']
    return []
