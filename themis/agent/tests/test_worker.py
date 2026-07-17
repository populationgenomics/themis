"""Unit tests for the sandbox worker's entrypoint, custom `shell` tool, and toolset assembly.

The `handle_item()` path (a live session through the proxy) can't be exercised offline; these cover the
pieces that can — the model-stated-intent `shell` tool executing on a real `BashSession`, the toolset swap
that replaces the prebuilt bash with it, and the `--max-idle` parsing `SandboxJob` passes.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from unittest import mock

import pytest
from anthropic.lib import tools
from anthropic.lib.tools import agent_toolset

from themis.agent import worker


def test_max_idle_takes_plain_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    run = mock.AsyncMock()
    monkeypatch.setattr(worker, '_run', run)
    monkeypatch.setattr(sys, 'argv', ['worker', '--max-idle', '300'])
    worker.main()
    run.assert_awaited_once_with(300.0)


def test_max_idle_rejects_a_duration_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    run = mock.AsyncMock()
    monkeypatch.setattr(worker, '_run', run)
    monkeypatch.setattr(sys, 'argv', ['worker', '--max-idle', '300s'])
    with pytest.raises(SystemExit):
        worker.main()
    run.assert_not_awaited()


def test_max_idle_defaults_to_the_sdk_default(monkeypatch: pytest.MonkeyPatch) -> None:
    run = mock.AsyncMock()
    monkeypatch.setattr(worker, '_run', run)
    monkeypatch.setattr(sys, 'argv', ['worker'])
    worker.main()
    run.assert_awaited_once_with(worker.environments.DEFAULT_MAX_IDLE)


def test_build_tools_swaps_bash_for_shell(tmp_path: pathlib.Path) -> None:
    env = agent_toolset.AgentToolContext(workdir=str(tmp_path))
    names = [tool.name for tool in worker._build_tools(env)]
    assert names.count('shell') == 1
    assert 'bash' not in names
    assert {'read', 'write', 'edit', 'glob', 'grep', 'shell'} == set(names)


def test_shell_schema_requires_command_and_intent(tmp_path: pathlib.Path) -> None:
    env = agent_toolset.AgentToolContext(workdir=str(tmp_path))
    shell = worker._shell_tool(env)
    assert isinstance(shell, tools.BetaAsyncFunctionTool)
    # Round-trip through JSON so the assertions read plain dicts, not the SDK's partial TypedDict.
    schema = json.loads(json.dumps(shell.to_dict()['input_schema']))
    assert set(schema['required']) == {'command', 'intent'}
    assert schema['properties']['intent']['type'] == 'string'


def test_shell_returns_stdout(tmp_path: pathlib.Path) -> None:
    async def run() -> None:
        env = agent_toolset.AgentToolContext(workdir=str(tmp_path))
        shell = worker._shell_tool(env)
        assert isinstance(shell, tools.BetaAsyncFunctionTool)
        try:
            out = await shell.call({'command': 'echo hi', 'intent': 'greet'})
            assert out == 'hi'
        finally:
            await env.close()

    asyncio.run(run())


def test_shell_nonzero_exit_raises_tool_error(tmp_path: pathlib.Path) -> None:
    async def run() -> None:
        env = agent_toolset.AgentToolContext(workdir=str(tmp_path))
        shell = worker._shell_tool(env)
        assert isinstance(shell, tools.BetaAsyncFunctionTool)
        try:
            with pytest.raises(tools.ToolError):
                await shell.call({'command': 'echo boom >&2; exit 3', 'intent': 'fail'})
        finally:
            await env.close()

    asyncio.run(run())
