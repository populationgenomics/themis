"""A session's tool calls are spans in its trace, carrying the tool's name and never the call's data."""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any, cast

import anyio
import postern
import pytest
from anthropic import AsyncAnthropic
from anthropic.lib import environments, tools
from opentelemetry import trace
from opentelemetry.sdk.trace.export import in_memory_span_exporter

from themis.services.sandbox_worker import tool, tool_tracing
from themis.services.sandbox_worker.tests import fakes

_SECRET = 'variant c.1A>G in the workspace'


def _shell(tmp_path: pathlib.Path, result: postern.ProcResult) -> tools.BetaAsyncFunctionTool:
    sandbox = fakes.FakeSandbox(tmp_path, lambda _c: result)
    return tool.make_shell(cast('postern.Sandbox', sandbox), timeout=30)


@tools.beta_async_tool
async def _failing(path: str) -> str:
    """Fail the way a file tool does, quoting what it was given."""
    raise tools.ToolError(f'{path}: {_SECRET}')


def test_a_dispatched_call_is_a_child_of_the_session_span(
    tmp_path: pathlib.Path, recorded_spans: in_memory_span_exporter.InMemorySpanExporter
) -> None:
    """The real SDK runner dispatches the call, so the span reaches the session's trace through its task groups."""
    shell = tool_tracing.TracedTool(_shell(tmp_path, postern.ProcResult(0, 'hi from guest', '')))
    events = fakes.FakeEvents(
        [
            fakes.tool_use_event('tu-1', 'shell', {'command': 'echo hi', 'intent': 'say hi'}),
            fakes.idle_end_turn_event(),
            fakes.terminated_event(),
        ]
    )
    runner = environments.SessionToolRunner(
        cast('AsyncAnthropic', fakes.FakeClient(events)), 'sesn_1', tools=[cast('Any', shell)], max_idle=None
    )

    async def drain() -> list[environments.DispatchedToolCall]:
        with tool_tracing.session('sesn_1'):
            return [call async for call in runner]

    (call,) = asyncio.run(drain())

    assert not call.is_error
    call_span, session_span = sorted(recorded_spans.get_finished_spans(), key=lambda span: span.name)
    assert session_span.name == 'sandbox session'
    assert session_span.attributes == {'gen_ai.conversation.id': 'sesn_1'}
    assert call_span.name == 'execute_tool shell'
    assert call_span.attributes == {'gen_ai.operation.name': 'execute_tool', 'gen_ai.tool.name': 'shell'}
    assert call_span.parent is not None
    assert session_span.context is not None
    assert call_span.parent.span_id == session_span.context.span_id
    assert call_span.status.status_code is trace.StatusCode.UNSET


def test_a_failed_call_names_the_error_type_and_nothing_it_said(
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    traced = tool_tracing.TracedTool(_failing)

    with pytest.raises(tools.ToolError):
        asyncio.run(traced.call({'path': 'notes.md'}))

    (span,) = recorded_spans.get_finished_spans()
    assert span.status.status_code is trace.StatusCode.ERROR
    assert span.attributes is not None
    assert span.attributes['error.type'] == f'{tools.ToolError.__module__}.ToolError'
    assert not span.events
    recorded = repr((span.attributes, span.status.description, span.events))
    assert _SECRET not in recorded
    assert 'notes.md' not in recorded


def test_a_session_that_fails_names_the_error_type_and_nothing_it_said(
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    """A failed push at teardown, say, carries the guest's stderr."""
    with pytest.raises(RuntimeError), tool_tracing.session('sesn_1'):
        raise RuntimeError(f'push failed: {_SECRET}')

    (span,) = recorded_spans.get_finished_spans()
    assert span.status.status_code is trace.StatusCode.ERROR
    assert span.attributes is not None
    assert span.attributes['error.type'] == 'builtins.RuntimeError'
    assert not span.events
    assert _SECRET not in repr((span.attributes, span.status.description, span.events))


@tools.beta_async_tool
async def _slow() -> str:
    """Outlast the SDK's per-call deadline."""
    await anyio.sleep(10)
    return 'never'


def test_a_call_cut_off_by_its_deadline_is_an_error(
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    """The SDK bounds each call with `anyio.fail_after`, which reaches the tool as a cancellation."""
    traced = tool_tracing.TracedTool(_slow)

    async def call() -> None:
        with anyio.fail_after(0.01):
            await traced.call({})

    with pytest.raises(TimeoutError):
        asyncio.run(call())

    (span,) = recorded_spans.get_finished_spans()
    assert span.status.status_code is trace.StatusCode.ERROR
    assert span.attributes is not None
    assert span.attributes['error.type'] == 'asyncio.exceptions.CancelledError'


def test_the_session_is_shown_the_wrapped_tool(tmp_path: pathlib.Path) -> None:
    shell = _shell(tmp_path, postern.ProcResult(0, '', ''))
    traced = tool_tracing.TracedTool(shell)
    assert traced.name == 'shell'
    assert traced.to_dict() == shell.to_dict()


@pytest.mark.parametrize('cleanup', ['aclose', 'close', '_context_manager'])
def test_a_tool_with_a_cleanup_is_refused(tmp_path: pathlib.Path, cleanup: str) -> None:
    shell = _shell(tmp_path, postern.ProcResult(0, '', ''))
    setattr(shell, cleanup, object())
    with pytest.raises(ValueError, match='cleanup'):
        tool_tracing.TracedTool(shell)
