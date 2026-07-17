"""The sandbox agent's worker entrypoint — a first-party Python `EnvironmentWorker`.

Runs one already-claimed self-hosted work item: builds the async Anthropic client against the
credential proxy (`ANTHROPIC_BASE_URL`) with the placeholder environment key the proxy rewrites, then
drives `EnvironmentWorker.handle_item()` — which reads `ANTHROPIC_SESSION_ID` / `_WORK_ID` /
`_ENVIRONMENT_ID` / `_ENVIRONMENT_KEY` from the per-execution env the dispatcher injects, dispatches the
session's tool calls against the local toolset, and posts each result back.

The toolset is the prebuilt `agent_toolset_20260401` set with its bash replaced by a custom `shell` tool
that carries a model-stated `intent` label (see `_shell_tool`). The tool is named `shell`, not `bash`,
because the agent API reserves the prebuilt tool names — a custom tool may not shadow `bash` even when
the prebuilt bash is disabled. The runner constructs every posted tool result, so the empty-output case
is guarded by the SDK (`anthropic.lib.tools._beta_session_runner._to_session_content` maps empty output
to `"(no output)"`) rather than a per-tool patch.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

import anthropic
from anthropic.lib import environments, tools
from anthropic.lib.tools import agent_toolset

log = logging.getLogger(__name__)


def _shell_tool(env: agent_toolset.AgentToolContext) -> environments.BetaAnyRunnableTool:
    """A custom `shell` tool that records a model-stated `intent` alongside the command.

    Executes on the context's persistent `BashSession` (self-healing if the shell has exited), so cwd,
    env, and background jobs survive across calls. A non-zero exit is surfaced as a `ToolError` so the
    posted result carries `is_error`, mirroring the prebuilt bash tool. `intent` is required so the model
    always states it; it is model→UI metadata, never read during execution.

    Args:
        env: The per-session tool context whose `BashSession` runs the command.

    Returns:
        The custom `shell` tool, ready to add to the session's toolset.
    """

    @anthropic.beta_async_tool(name='shell')
    async def shell(command: str, intent: str, timeout_ms: int | None = None) -> str:  # noqa: ARG001
        """Run a shell command in the sandbox's persistent bash session.

        Args:
            command: The shell command to run.
            intent: Required on every call. A short present-tense phrase naming what this command does and
                why — shown to the user as this action's label.
            timeout_ms: Optional timeout in milliseconds (default 120000).
        """
        # `intent` is a schema-only, model-stated label surfaced through the tool-call event; the shell
        # never sees it.
        session = await env.bash()
        if session.closed:
            await env.close()
            session = await env.bash()
        timeout = timeout_ms / 1000.0 if timeout_ms else agent_toolset.BASH_DEFAULT_TIMEOUT
        try:
            out, code = await session.exec(command, timeout=timeout)
            if code != 0:
                raise tools.ToolError(out)
            return out
        except (RuntimeError, TimeoutError) as e:
            raise tools.ToolError(f'shell: {e}') from e

    return shell


def _build_tools(env: agent_toolset.AgentToolContext) -> list[environments.BetaAnyRunnableTool]:
    """The self-hosted toolset with the prebuilt bash swapped for the intent-carrying `shell` tool.

    Args:
        env: The per-session tool context the worker binds each tool to.

    Returns:
        The read/write/edit/glob/grep prebuilt tools plus the custom `shell` tool.
    """
    prebuilt = [tool for tool in agent_toolset.beta_agent_toolset_20260401(env) if tool.name != 'bash']
    return [*prebuilt, _shell_tool(env)]


async def _run(max_idle: float) -> None:
    """Service the claimed work item, then return.

    Args:
        max_idle: Seconds the worker keeps running after the session goes idle with `stop_reason`
            `end_turn` before releasing.
    """
    async with anthropic.AsyncAnthropic(
        base_url=os.environ['ANTHROPIC_BASE_URL'],
        auth_token=os.environ['ANTHROPIC_ENVIRONMENT_KEY'],
    ) as client:
        worker = environments.EnvironmentWorker(
            client,
            tools=_build_tools,
            workdir='/workspace',
            max_idle=max_idle,
        )
        await worker.handle_item()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description='Run one self-hosted sandbox work item.')
    parser.add_argument(
        '--max-idle',
        type=float,
        default=environments.DEFAULT_MAX_IDLE,
        help='Seconds to keep running after an end_turn idle before releasing the sandbox.',
    )
    args = parser.parse_args()
    asyncio.run(_run(args.max_idle))


if __name__ == '__main__':
    main()
