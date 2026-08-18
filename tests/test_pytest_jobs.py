"""The tests workflow runs pytest twice, and only complementary marker expressions keep the suite whole.

`.github/workflows/tests.yml` runs `-m 'not sandbox'` in one job and — after installing bubblewrap —
`-m sandbox` in another. A test both expressions deselect is collected, never run, and reported by
neither job: green from a suite that skipped it. Editing one expression without the other is what this
fails on. `testpaths` coverage, the other half of "everything runs", is `test_testpaths.py`.
"""

from __future__ import annotations

import pathlib
import re
import shlex

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / '.github' / 'workflows' / 'tests.yml'
_PYTEST_STEP = re.compile(r'^\s*uv run pytest\b', re.MULTILINE)
# `sandbox` is the marker pyproject declares; a job pair splitting on it covers the suite exactly once.
_PARTITION = {'sandbox', 'not sandbox'}


def _pytest_commands() -> list[str]:
    workflow = yaml.safe_load(_WORKFLOW.read_text('utf-8'))
    return [
        step['run']
        for job in workflow['jobs'].values()
        for step in job['steps']
        if 'run' in step and _PYTEST_STEP.search(step['run'])
    ]


def _marker_expression(command: str) -> str:
    """The argument the command passes to `-m`, shell quoting resolved."""
    argv = shlex.split(command)
    assert '-m' in argv, command
    return argv[argv.index('-m') + 1]


def test_the_jobs_partition_the_suite() -> None:
    commands = _pytest_commands()
    assert len(commands) == 2, f'expected two pytest jobs, found {len(commands)}'
    assert {_marker_expression(command) for command in commands} == _PARTITION
