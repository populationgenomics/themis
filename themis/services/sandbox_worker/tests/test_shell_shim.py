"""The shim the ``shell`` tool runs a command through, exercised as the program it is.

Run here with the host interpreter rather than inside postern: what is under test is the program's own
behaviour — the bound it applies, the environment it sets, the exit code it passes on — none of which depends on
the sandbox around it.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
import typing

import postern
import pytest

from themis.services.sandbox_worker import sync, tool

_OUTER_TIMEOUT_S = 30  # a shim that failed to bound its command fails this way, never by hanging the suite


def _run(command: str, *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — the shim under test, run as postern would run it
        [sys.executable, '-c', tool.shim(command, timeout=timeout)],
        capture_output=True,
        text=True,
        timeout=_OUTER_TIMEOUT_S,
        check=False,
    )


def _python(snippet: str) -> str:
    return f'{shlex.quote(sys.executable)} -c {shlex.quote(snippet)}'


def test_a_command_that_finishes_passes_its_output_and_exit_code_through() -> None:
    result = _run(f'{_python("print(1)")}; exit 3', timeout=tool._KILL_MARGIN_S + 20)
    assert result.stdout == '1\n'
    assert result.returncode == 3


def test_a_python_the_command_spawns_writes_unbuffered() -> None:
    result = _run(_python('import sys; print(sys.stdout.write_through)'), timeout=tool._KILL_MARGIN_S + 20)
    assert result.stdout == 'True\n', result.stderr


def test_a_command_that_overruns_is_killed_under_the_shims_own_bound_with_its_output_intact() -> None:
    """The progress a run printed before stalling survives the kill, and the model is told which bound fired.

    Buffered, the print would still be in the child's block buffer when SIGKILL lands, and the tool result would
    say nothing but that the run timed out.
    """
    started = time.monotonic()
    result = _run(_python('import time; print("progress"); time.sleep(60)'), timeout=tool._KILL_MARGIN_S + 1)
    assert time.monotonic() - started < _OUTER_TIMEOUT_S / 2
    assert result.stdout == 'progress\n'
    assert '[shell] command killed after 1 s' in result.stderr
    assert result.returncode == 124


def test_the_kill_reaches_a_pipeline_the_shell_forked() -> None:
    """`sh -c` forks a pipeline's stages; killing the shell alone leaves them holding the pipes open."""
    started = time.monotonic()
    result = _run(f'{_python("import time; time.sleep(60)")} | cat', timeout=tool._KILL_MARGIN_S + 1)
    assert time.monotonic() - started < _OUTER_TIMEOUT_S / 2
    assert result.returncode == 124


def test_a_timeout_inside_the_kill_margin_fails_the_precondition() -> None:
    with pytest.raises(ValueError, match='kill margin'):
        tool.shim('true', timeout=tool._KILL_MARGIN_S)


def test_a_timeout_past_the_tool_budget_fails_the_precondition() -> None:
    """Past the SDK's per-tool deadline the call is abandoned with its checkpoint skipped, whatever the shim does."""
    with pytest.raises(ValueError, match='SDK allows'):
        tool.shim('true', timeout=tool.SHELL_TIMEOUT_S + 1)


def test_the_tool_refuses_a_bound_it_could_not_run_a_command_under_at_construction() -> None:
    """A bad bound fails where the tool is built, not on the model's first call."""
    sandbox, workspace_sync = typing.cast('postern.Sandbox', object()), typing.cast('sync.WorkspaceSync', object())
    with pytest.raises(ValueError, match='kill margin'):
        tool.make_shell(sandbox, workspace_sync, timeout=1)


def test_the_default_shim_names_the_one_command_bound() -> None:
    """The figure the model is told is the one constant the worker, the shim and the skill all read."""
    assert f'killed after {tool.COMMAND_TIMEOUT_S:g} s' in tool.shim('true')
    assert tool.COMMAND_TIMEOUT_S < tool.SHELL_TIMEOUT_S
