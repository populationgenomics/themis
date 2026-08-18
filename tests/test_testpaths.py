"""`testpaths` is an allowlist, so a tests directory it omits is never collected.

CI runs `uv run pytest` with no path argument (`.github/workflows/tests.yml`), which honours `testpaths`. A tests
directory nobody adds to it is not an error and not a skip — the suite reports green having never
imported the file. A `testpaths` entry naming a path that no longer exists is the same fault from the
other side: pytest ignores it silently. These tests are what fails on either.
"""

from __future__ import annotations

import pathlib
import tomllib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / 'pyproject.toml'

# The repo's Python trees, enumerated rather than scanned from the root: a root-level `**/tests` walks
# into `.claude/worktrees` and `.venv`, and would report another checkout's directories as this one's.
_PACKAGE_ROOTS = ('themis', 'tools', 'infra', 'schema')

# pytest's own `python_files` default, which `pyproject.toml` does not override.
_TEST_FILE_PATTERNS = ('test_*.py', '*_test.py')


def _testpaths() -> set[str]:
    config = tomllib.loads(_PYPROJECT.read_text('utf-8'))
    return set(config['tool']['pytest']['ini_options']['testpaths'])


def _collected(path: str, testpaths: set[str]) -> bool:
    """Whether `path` is a testpath or sits under one — pytest recurses into an entry it collects."""
    return any(path == entry or path.startswith(f'{entry}/') for entry in testpaths)


def test_every_tests_directory_is_collected() -> None:
    testpaths = _testpaths()
    assert testpaths, 'pyproject declares no testpaths'
    # A directory is only evidence of a gap if it holds tests: a `git rm`-ed one whose `__pycache__`
    # survives locally would otherwise fail this, pointing at a path with nothing left to run. Both of
    # pytest's default patterns, searched recursively — anything it would collect counts as held.
    present = {
        str(directory.relative_to(_REPO_ROOT))
        for root in _PACKAGE_ROOTS
        for directory in _REPO_ROOT.glob(f'{root}/**/tests')
        if directory.is_dir() and any(f for pattern in _TEST_FILE_PATTERNS for f in directory.rglob(pattern))
    }
    assert present  # a repo with no package tests at all would pass vacuously
    uncovered = sorted(path for path in present if not _collected(path, testpaths))
    assert not uncovered, f'tests directories absent from testpaths: {uncovered}'


def test_every_testpath_exists() -> None:
    missing = sorted(entry for entry in _testpaths() if not (_REPO_ROOT / entry).is_dir())
    assert not missing, f'testpaths entries naming no directory: {missing}'
