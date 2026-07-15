"""Resolve and read committed files at a baseline git ref.

The backward-compat gate diffs each committed artifact against its baseline — the
released line, stood in by the PR base branch (CI passes ``origin/<base>``). The
buf gate (``tools.schema.buf_compat``) needs two operations against that ref:
verify it resolves, and read a path's content at it.
"""

from __future__ import annotations

import pathlib
import subprocess

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def require_ref(ref: str) -> None:
    """Fail loud unless ``ref`` resolves to a commit.

    Separates a genuinely-new domain (path absent at a *valid* baseline, a correct
    skip in ``show_at_ref``) from an unresolvable baseline ref (typo, unfetched
    branch, detached state) — an operational error that must not degrade to a
    silent green pass.
    """
    result = subprocess.run(  # noqa: S603
        ['git', 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}'],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f'baseline ref {ref!r} does not resolve to a commit (unfetched branch, typo, or detached?)')


def show_at_ref(ref: str, repo_rel_path: str) -> str | None:
    """Return the file's content at ``<ref>:<path>``, or ``None`` if absent there.

    Assumes ``ref`` is already known to resolve (see ``require_ref``), so a
    non-zero exit means the path doesn't exist at that ref — a brand-new domain
    with nothing to be incompatible with.
    """
    result = subprocess.run(  # noqa: S603
        ['git', 'show', f'{ref}:{repo_rel_path}'],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None
