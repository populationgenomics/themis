"""The ``shell`` tool the worker exposes to the agent (sandbox-worker.md §"Only arbitrary execution is sandboxed").

``EnvironmentWorker`` dispatches each ``custom_tool_use`` to this ``@beta_async_tool``; its inferred name/schema is
``shell(command, intent)``. Every call runs ``command`` inside the postern sandbox and checkpoints the working
document on return; the rest of ``/workspace`` is the agent's repository, and the agent commits and pushes it itself.
The command runs via postern's hatch-bound ``run_python`` path (a subprocess shim), so a ``python3`` the command
spawns inherits ``$POSTERN_HATCH`` and can reach the allowlisted internal services in code mode.

The shim bounds the command's runtime itself, a margin inside postern's deadline, and runs it with Python's output
unbuffered. Postern's own deadline is a SIGKILL of the guest with a bare ``[postern] timed out``, and a ``python3``
whose stdout is a pipe holds its prints in a block buffer, so a run killed that way reported nothing of the progress
it had printed — the results already obtained were lost with the stall they would have located. Under the shim's
bound the output is out of the process as it prints, the whole process group is killed so a survivor cannot hold the
pipes open until postern's deadline, and the model is told which bound fired.
"""

from __future__ import annotations

import functools
import logging

import postern
from anthropic.lib import tools
from anyio import to_thread

from themis.services.sandbox_worker import sync as sync_mod

_logger = logging.getLogger(__name__)

# postern binds the hatch UDS and exports POSTERN_HATCH only on the run_python path; a subprocess inherits that
# env, so the model's command (and any python3 it launches) can dial unix:$POSTERN_HATCH. The command is repr'd
# into the shim (``{command!r}``), never concatenated. ``start_new_session`` makes the command's pid its process
# group, so the kill reaches everything ``sh -c`` forked.
_SHELL_SHIM = """\
import os, signal, subprocess, sys
proc = subprocess.Popen({command!r}, shell=True, start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED='1'))
try:
    sys.exit(proc.wait(timeout={timeout!r}))
except subprocess.TimeoutExpired:
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    print({message!r}, file=sys.stderr)
    sys.exit(124)
"""
# The bound one `shell` call gets, under the SDK's per-tool deadline (anthropic.lib TOOL_TIMEOUT, 150 s): a sandbox
# run that outlives that makes the SDK abort the (non-cancellable) tool call, reporting a spurious timeout and
# skipping the post-call checkpoint. The one figure every other bound — the hatch's forwarding ceiling, the guest
# channel's default deadline, the shim's own — sits under.
SHELL_TIMEOUT_S = 120
# How far inside postern's deadline the shim's own bound sits: room for the kill, the message and the exit.
_KILL_MARGIN_S = 10
# What a command gets before the shim kills it, under the tool's default bound; the shim names its bound to the
# model when it fires.
COMMAND_TIMEOUT_S = SHELL_TIMEOUT_S - _KILL_MARGIN_S


def _command_bound(timeout: float) -> float:
    """How long a command runs before the shim kills it, under postern's deadline ``timeout``.

    Raises:
        ValueError: If ``timeout`` leaves the command no time inside the kill margin, or exceeds the
            tool's budget, past which the SDK abandons the call.
    """
    bound = timeout - _KILL_MARGIN_S
    if bound <= 0:
        raise ValueError(f'a shell timeout of {timeout} s leaves nothing inside the {_KILL_MARGIN_S} s kill margin')
    if timeout > SHELL_TIMEOUT_S:
        raise ValueError(f'a shell timeout of {timeout} s exceeds the {SHELL_TIMEOUT_S} s the SDK allows a tool call')
    return bound


def shim(command: str, *, timeout: float = SHELL_TIMEOUT_S) -> str:
    """The Python the guest runs for ``command``: the command under its own bound, output unbuffered.

    Args:
        command: The model's shell command, run with ``sh -c``.
        timeout: postern's deadline for the whole run; the command is killed ``_KILL_MARGIN_S`` before it.

    Raises:
        ValueError: If ``timeout`` is outside what ``_command_bound`` admits.
    """
    bound = _command_bound(timeout)
    message = f'[shell] command killed after {bound:g} s; split the work across shell calls'
    return _SHELL_SHIM.format(command=command, timeout=bound, message=message)


def _format(result: postern.ProcResult) -> str:
    """Flatten a ``ProcResult`` into the string the tool returns to the model."""
    parts: list[str] = []
    if result.stdout.strip():
        parts.append(result.stdout.rstrip('\n'))
    if result.stderr.strip():
        parts.append(f'[stderr] {result.stderr.rstrip()}')
    if not result.ok:
        parts.append(f'[exit] {result.returncode}')
    return '\n'.join(parts) if parts else '(no output)'


def make_shell(
    sandbox: postern.Sandbox, workspace_sync: sync_mod.WorkspaceSync, *, timeout: float = SHELL_TIMEOUT_S
) -> tools.BetaAsyncFunctionTool:
    """Build the ``shell`` tool bound to ``sandbox``, checkpointing the working document after each call.

    Raises:
        ValueError: If ``timeout`` is outside what the shim can run a command under.
    """
    _command_bound(timeout)

    @tools.beta_async_tool
    async def shell(command: str, intent: str) -> str:
        """Run a shell command in the sandbox's ``/workspace`` and return its combined output.

        The command runs with no network access and no filesystem outside ``/workspace`` (persisted across
        calls). Reach the allowlisted internal services in code mode — write Python and run it
        (``python3 -c '…'`` or ``python3 script.py``). ``intent`` is a short present-tense phrase naming what
        the command does; it is shown to the user as this action's label.
        """
        code = shim(command, timeout=timeout)
        result = await to_thread.run_sync(functools.partial(sandbox.run_python, code, timeout=timeout))
        # intent is the model's own label for the action; logging it (with the exit code) gives a
        # worker-side audit of what ran in the sandbox, alongside the BFF's per-event copy.
        _logger.info('shell [%s] exit=%d', intent, result.returncode)
        await workspace_sync.checkpoint()
        return _format(result)

    return shell
