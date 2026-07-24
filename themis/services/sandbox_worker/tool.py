"""The ``shell`` tool the worker exposes to the agent (postern-sandbox-swap.md §4).

``EnvironmentWorker`` dispatches each ``custom_tool_use`` to this ``@beta_async_tool``; its inferred name/schema is
``shell(command, intent)``. Every call runs ``command`` inside the postern sandbox and checkpoints ``/workspace`` on
return. The command runs via postern's hatch-bound ``run_python`` path (a subprocess shim), so a ``python3`` the command
spawns inherits ``$POSTERN_HATCH`` and can reach the allowlisted internal services in code mode.
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
# into the shim (``{command!r}``), never concatenated.
_SHELL_SHIM = 'import subprocess, sys\nsys.exit(subprocess.run({command!r}, shell=True).returncode)'


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
    sandbox: postern.Sandbox, workspace_sync: sync_mod.WorkspaceSync, *, timeout: float = 60
) -> tools.BetaAsyncFunctionTool:
    """Build the ``shell`` tool bound to ``sandbox``, checkpointing after each call."""

    @tools.beta_async_tool
    async def shell(command: str, intent: str) -> str:
        """Run a shell command in the sandbox's ``/workspace`` and return its combined output.

        The command runs with no network access and no filesystem outside ``/workspace`` (persisted across
        calls). Reach the allowlisted internal services in code mode — write Python and run it
        (``python3 -c '…'`` or ``python3 script.py``). ``intent`` is a short present-tense phrase naming what
        the command does; it is shown to the user as this action's label.
        """
        code = _SHELL_SHIM.format(command=command)
        result = await to_thread.run_sync(functools.partial(sandbox.run_python, code, timeout=timeout))
        # intent is the model's own label for the action; logging it (with the exit code) gives a
        # worker-side audit of what ran in the sandbox, alongside the BFF's per-event copy.
        _logger.info('shell [%s] exit=%d', intent, result.returncode)
        await workspace_sync.checkpoint()
        return _format(result)

    return shell
