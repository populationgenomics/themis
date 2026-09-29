"""Content checks the hook's tests hand it by name, as a deployment would: plain functions over git's objects."""

from __future__ import annotations

import pathlib
import subprocess
from collections.abc import Sequence

FORBIDDEN = 'forbidden.txt'


def refuse_a_forbidden_file(git_dir: pathlib.Path, commit: str, parents: Sequence[str]) -> list[str]:
    """Refuse a commit whose tree holds `FORBIDDEN`, naming how many parents the hook passed."""
    listed = subprocess.run(
        ['git', '--git-dir', str(git_dir), 'ls-tree', '--name-only', commit],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [f'holds {FORBIDDEN} (parents: {len(parents)})'] if FORBIDDEN in listed else []


NOT_CALLABLE = 'a string'


def fail(git_dir: pathlib.Path, commit: str, parents: Sequence[str]) -> list[str]:
    """A check that raises rather than answers, as a check whose git times out would."""
    del git_dir, parents
    raise subprocess.TimeoutExpired(['git', 'cat-file', commit], 60)
