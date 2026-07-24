"""Test doubles for the worker's Anthropic seam and sandbox.

The narrowest real boundary is ``client.beta.sessions.events`` — the transport ``SessionToolRunner`` reads tool-call
events from (``list`` / ``stream``) and posts results to (``send``). Faking it drives the *real* runner + tool dispatch
without a live managed-agents session or credentials. ``FakeSandbox`` stands in for ``postern.Sandbox`` where a real
bwrap guest is not available (the isolation itself is exercised in ``test_session_integration.py`` on a bwrap host).
"""

from __future__ import annotations

import types
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import postern


def tool_use_event(tool_use_id: str, name: str, tool_input: dict[str, Any]) -> types.SimpleNamespace:
    """An ``agent.custom_tool_use`` event carrying the tool name and input."""
    return types.SimpleNamespace(type='agent.custom_tool_use', id=tool_use_id, name=name, input=tool_input)


def idle_end_turn_event() -> types.SimpleNamespace:
    """A ``session.status_idle`` / ``end_turn`` boundary — arms the runner's idle watchdog."""
    return types.SimpleNamespace(type='session.status_idle', stop_reason=types.SimpleNamespace(type='end_turn'))


def terminated_event() -> types.SimpleNamespace:
    """A ``session.status_terminated`` event — ends the runner's iteration."""
    return types.SimpleNamespace(type='session.status_terminated')


class _StreamCM:
    def __init__(self, events: Sequence[object]) -> None:
        self._events = events

    async def __aenter__(self) -> AsyncIterator[object]:
        async def _iter() -> AsyncIterator[object]:
            for event in self._events:
                yield event

        return _iter()

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class FakeEvents:
    """Fake ``AsyncEvents``: scripted live stream, empty history, recorded sends."""

    def __init__(self, stream_events: Sequence[object]) -> None:
        self._stream_events = stream_events
        self.sent: list[Any] = []

    def list(
        self, _session_id: str, *, limit: int | None = None, extra_headers: object = None
    ) -> AsyncIterator[object]:
        del limit, extra_headers

        async def _empty() -> AsyncIterator[object]:
            return
            yield  # pragma: no cover — makes this an async generator

        return _empty()

    async def stream(self, _session_id: str, *, extra_headers: object = None) -> _StreamCM:
        del extra_headers
        return _StreamCM(self._stream_events)

    async def send(self, _session_id: str, *, events: Sequence[object], extra_headers: object = None) -> None:
        del extra_headers
        self.sent.extend(events)


class FakeClient:
    """Fake ``AsyncAnthropic`` exposing only ``beta.sessions.events`` (what SessionToolRunner touches)."""

    def __init__(self, events: FakeEvents) -> None:
        self.beta = types.SimpleNamespace(sessions=types.SimpleNamespace(events=events))

    def with_options(self, **_kwargs: object) -> FakeClient:
        return self  # SessionToolRunner layers a telemetry header via with_options; state is not mutated


class FakeSandbox:
    """Stands in for ``postern.Sandbox``: ``run_python`` runs a callback against a real workspace dir."""

    def __init__(self, workspace: object, run: Callable[[str], postern.ProcResult]) -> None:
        self.workspace = workspace
        self._run = run
        self.calls: list[str] = []

    def run_python(self, code: str, *, timeout: float = 60) -> postern.ProcResult:
        del timeout
        self.calls.append(code)
        return self._run(code)
