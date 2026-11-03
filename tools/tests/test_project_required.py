"""Every operator tool that acts on a GCP project refuses to run until it is told which one."""

from __future__ import annotations

import sys
from collections.abc import Callable

import pytest

from tools import bring_up, clone_analysis, pi_analysis, psql, session_token, sheaf_remote
from tools.litcache import deposit, run_local
from tools.resources import mirror
from tools.run_review import __main__ as run_review

_TOOLS: list[tuple[str, Callable[[], object], list[str]]] = [
    ('bring_up', bring_up.main, []),
    ('clone_analysis', clone_analysis.main, []),
    ('pi_analysis', pi_analysis.main, []),
    ('psql', psql.main, []),
    ('session_token', session_token.main, []),
    ('sheaf_remote', sheaf_remote.main, []),
    ('mirror', mirror.main, []),
    ('deposit', deposit.main, []),
    ('run_local', run_local.main, ['--scratch-bucket', 'scratch']),
    ('run_review', run_review.main, ['prepare', '--round', '2026-10-30-0900', '--workbench-project', 'validation']),
]


@pytest.mark.parametrize(('tool', 'main', 'args'), _TOOLS, ids=[tool for tool, _, _ in _TOOLS])
def test_a_tool_refuses_to_run_without_a_project(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tool: str,
    main: Callable[[], object],
    args: list[str],
) -> None:
    monkeypatch.setattr(sys, 'argv', [tool, *args])
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 2
    assert '--project' in capsys.readouterr().err
