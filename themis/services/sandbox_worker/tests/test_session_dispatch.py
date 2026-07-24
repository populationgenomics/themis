"""Drive the real SessionToolRunner over a faked event stream (postern-sandbox-swap.md §6).

Mocks Anthropic at the narrowest seam — ``client.beta.sessions.events`` — and runs the *real*
``anthropic.lib.environments.SessionToolRunner`` against our ``shell`` tool, so the dispatch contract is exercised end
to end (tool-call event in → command runs in the sandbox → ``/workspace`` checkpointed → result posted back) without a
live session, credentials, or bwrap. The sandbox itself is faked here; real isolation is covered in
``test_session_integration.py`` on a bwrap host.
"""

from __future__ import annotations

import asyncio
import io
import pathlib
from typing import Any, cast

import postern
from anthropic import AsyncAnthropic
from anthropic.lib import environments

from themis.services.sandbox_worker import store_client, sync, tool
from themis.services.sandbox_worker.tests import fakes


async def _drain(runner: environments.SessionToolRunner) -> list[environments.DispatchedToolCall]:
    return [call async for call in runner]


def _runner(events: fakes.FakeEvents, shell: object) -> environments.SessionToolRunner:
    # the fakes are structural stand-ins; cast past the concrete SDK/postern types the signatures name
    return environments.SessionToolRunner(
        cast('AsyncAnthropic', fakes.FakeClient(events)), 'sid', tools=[cast('Any', shell)], max_idle=None
    )


def test_tool_call_runs_in_the_sandbox_checkpoints_and_posts_a_result(tmp_path: pathlib.Path) -> None:
    workspace_dir = tmp_path / 'ws'
    workspace_dir.mkdir()

    def _run(code: str) -> postern.ProcResult:
        del code  # the subprocess shim the shell tool builds; the fake just simulates a guest write
        (workspace_dir / 'out.txt').write_text('from-guest')
        return postern.ProcResult(0, 'hi from guest', '')

    sandbox = fakes.FakeSandbox(workspace_dir, _run)
    store = store_client.FixtureStore()
    workspace_sync = sync.WorkspaceSync(store, accessor=postern.Workspace(workspace_dir), exclude={'skills'})
    shell = tool.make_shell(cast('postern.Sandbox', sandbox), workspace_sync, timeout=30)

    events = fakes.FakeEvents(
        [
            fakes.tool_use_event('tu-1', 'shell', {'command': 'echo hi', 'intent': 'say hi'}),
            fakes.idle_end_turn_event(),
            fakes.terminated_event(),
        ]
    )

    calls = asyncio.run(_drain(_runner(events, shell)))

    # the command was dispatched to our tool and marshaled into the sandbox (repr'd into the run_python shim)
    assert len(sandbox.calls) == 1
    assert "'echo hi'" in sandbox.calls[0]
    dispatched = [c for c in calls if c.name == 'shell']
    assert len(dispatched) == 1
    call = dispatched[0]
    assert not call.is_error
    result = cast('Any', call.result)
    assert result['content'][0]['text'] == 'hi from guest'

    # the result was posted back over the (faked) events transport as a custom_tool_result
    posted = [e for e in events.sent if e['type'] == 'user.custom_tool_result']
    assert len(posted) == 1
    assert posted[0]['custom_tool_use_id'] == 'tu-1'

    # the tool checkpointed /workspace, capturing the guest's write but not the excluded skills tree
    assert len(store.put_workspaces) == 1
    restored = tmp_path / 'restored'
    restored.mkdir()
    with postern.Workspace(restored) as ws:
        ws.restore_tar(io.BytesIO(store.put_workspaces[0]))
    assert (restored / 'out.txt').read_text() == 'from-guest'


def test_unowned_tool_call_is_left_pending_not_answered(tmp_path: pathlib.Path) -> None:
    workspace_dir = tmp_path / 'ws'
    workspace_dir.mkdir()
    sandbox = fakes.FakeSandbox(workspace_dir, lambda _c: postern.ProcResult(0, '', ''))
    store = store_client.FixtureStore()
    workspace_sync = sync.WorkspaceSync(store, accessor=postern.Workspace(workspace_dir))
    shell = tool.make_shell(cast('postern.Sandbox', sandbox), workspace_sync, timeout=30)

    events = fakes.FakeEvents(
        [
            fakes.tool_use_event('tu-2', 'some_other_tool', {'x': 1}),
            fakes.idle_end_turn_event(),
            fakes.terminated_event(),
        ]
    )

    calls = asyncio.run(_drain(_runner(events, shell)))

    # a tool this runner does not own is observed but not executed or answered (split-client contract)
    assert sandbox.calls == []
    assert events.sent == []
    unowned = [c for c in calls if c.name == 'some_other_tool']
    assert len(unowned) == 1
    assert not unowned[0].posted
