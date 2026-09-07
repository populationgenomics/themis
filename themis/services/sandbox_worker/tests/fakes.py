"""Test doubles for the worker's Anthropic seam and sandbox.

The narrowest real boundary is ``client.beta.sessions.events`` — the transport ``SessionToolRunner`` reads tool-call
events from (``list`` / ``stream``) and posts results to (``send``). Faking it drives the *real* runner + tool dispatch
without a live managed-agents session or credentials. ``FakeSandbox`` stands in for ``postern.Sandbox`` where a real
bwrap guest is not available (the isolation itself is exercised in ``test_session_integration.py`` on a bwrap host);
``HostGitSandbox`` runs the worker's own git commands host-side against the real hatches.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import types
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import postern
from postern import stream

# The worker's guest git has the identity and the ext:: allowance from the guest rootfs's /etc/gitconfig; a host
# stand-in supplies them per invocation, and reads no configuration of the developer's own.
_GIT_CONFIG = ['-c', 'protocol.ext.allow=always', '-c', 'user.name=Themis agent', '-c', 'user.email=agent@localhost']
_CONNECT = pathlib.Path(stream.__file__).parent / '_stream_connect.py'


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


class HostGitSandbox:
    """A `guest_git.Guest` for hosts without bubblewrap: the commands run host-side in `workspace`.

    Where the real guest reaches a hatch at ``/run/postern/<name>.sock`` through the bound-in connector, this runs
    the same connector against the hatch's host-side socket (`url`), so everything but the isolation is the
    production path: the ``ext::`` transport, the sync-before-serve handler, the hook. Only the two commands the
    worker itself issues — ``git`` and Python — are runnable.
    """

    def __init__(self, workspace: pathlib.Path) -> None:
        self.workspace = workspace
        self.calls: list[list[str]] = []

    @staticmethod
    def url(hatch: stream.StreamHatch) -> str:
        """The ``ext::`` URL a host-side git reaches `hatch` by."""
        return f'ext::{sys.executable} {_CONNECT} {hatch.socket_path}'

    def run(self, argv: list[str], *, timeout: float = 60) -> postern.ProcResult:
        if argv[0] != 'git':
            raise ValueError(f'the worker runs only git in the guest, not {argv[0]!r}')
        self.calls.append(argv)
        return self._run(['git', *_GIT_CONFIG, *argv[1:]], timeout=timeout)

    def run_python(self, code: str, *, timeout: float = 60) -> postern.ProcResult:
        return self._run([sys.executable, '-c', code], timeout=timeout)

    def _run(self, argv: list[str], *, timeout: float) -> postern.ProcResult:
        env = {'PATH': os.environ['PATH'], 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}
        result = subprocess.run(  # noqa: S603 — a test double for the sandbox; argv is the worker's own
            argv, cwd=self.workspace, env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
        return postern.ProcResult(result.returncode, result.stdout, result.stderr)
