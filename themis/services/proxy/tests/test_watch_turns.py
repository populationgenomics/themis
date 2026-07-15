"""Tests for the working-document checkpoint trigger — the proxy's only checkpoint path.

Covers the turn-boundary predicate, history consolidation (seed on first connect, checkpoint gaps on
reconnect), and the reconnect/error classification of the event-stream watcher.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Self

import anthropic
import httpx
import pytest
from anthropic.types.beta import sessions as anthropic_sessions
from anthropic.types.beta.sessions import beta_managed_agents_session_end_turn as end_turn_mod
from anthropic.types.beta.sessions import beta_managed_agents_session_requires_action as requires_action_mod

from themis.services.proxy import __main__ as entrypoint
from themis.services.proxy import sync as sync_mod

_Event = anthropic_sessions.BetaManagedAgentsStreamSessionEvents


def _idle_end_turn(event_id: str) -> _Event:
    return anthropic_sessions.BetaManagedAgentsSessionStatusIdleEvent.model_construct(
        id=event_id,
        type='session.status_idle',
        stop_reason=end_turn_mod.BetaManagedAgentsSessionEndTurn.model_construct(type='end_turn'),
    )


def _idle_requires_action(event_id: str) -> _Event:
    return anthropic_sessions.BetaManagedAgentsSessionStatusIdleEvent.model_construct(
        id=event_id,
        type='session.status_idle',
        stop_reason=requires_action_mod.BetaManagedAgentsSessionRequiresAction.model_construct(type='requires_action'),
    )


def _running(event_id: str) -> _Event:
    return anthropic_sessions.BetaManagedAgentsSessionStatusRunningEvent.model_construct(
        id=event_id, type='session.status_running'
    )


def _terminated(event_id: str) -> _Event:
    return anthropic_sessions.BetaManagedAgentsSessionStatusTerminatedEvent.model_construct(
        id=event_id, type='session.status_terminated'
    )


class _SpyCheckpoints(sync_mod.WorkspaceSync):
    """A WorkspaceSync that only records how many times ``checkpoint`` was called."""

    def __init__(self) -> None:
        self.count = 0

    async def checkpoint(self) -> None:
        self.count += 1


async def _aiter(items: Iterable[_Event]) -> AsyncIterator[_Event]:
    for item in items:
        yield item


class _FakeStream:
    """One event-stream connection: yields the given events, then closes (as a real stream drop would)."""

    def __init__(self, events: Sequence[_Event]) -> None:
        self._events = events

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def __aiter__(self) -> AsyncIterator[_Event]:
        return _aiter(self._events)


# Each connect either raises (a fault/cancel) or yields a batch of events then closes.
_StreamEffect = BaseException | Sequence[_Event]


class _FakeEvents:
    def __init__(self, history: Sequence[_Event] = (), stream_effects: Sequence[_StreamEffect] = ()) -> None:
        self._history = history
        self._stream_effects = list(stream_effects)
        self.stream_calls = 0

    def list(self, session_id: str) -> AsyncIterator[_Event]:  # noqa: ARG002 — SDK signature
        return _aiter(self._history)

    async def stream(self, session_id: str) -> _FakeStream:  # noqa: ARG002 — SDK signature
        self.stream_calls += 1
        effect = self._stream_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return _FakeStream(effect)


class _FakeSessions:
    def __init__(self, events: _FakeEvents) -> None:
        self.events = events


class _FakeBeta:
    def __init__(self, events: _FakeEvents) -> None:
        self.sessions = _FakeSessions(events)


class _FakeClient:
    def __init__(self, events: _FakeEvents) -> None:
        self.beta = _FakeBeta(events)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _status_error(status: int) -> anthropic.APIStatusError:
    response = httpx.Response(status, request=httpx.Request('GET', 'https://upstream'))
    return anthropic.APIStatusError('boom', response=response, body=None)


def test_is_new_turn_boundary_true_once_per_end_turn_idle() -> None:
    seen: set[str] = set()
    event = _idle_end_turn('e1')
    assert entrypoint._is_new_turn_boundary(event, seen) is True
    assert seen == {'e1'}
    assert entrypoint._is_new_turn_boundary(event, seen) is False  # id-deduped


def test_is_new_turn_boundary_ignores_non_end_turn_idle() -> None:
    assert entrypoint._is_new_turn_boundary(_idle_requires_action('e1'), set()) is False


def test_is_new_turn_boundary_ignores_non_idle_events() -> None:
    assert entrypoint._is_new_turn_boundary(_running('e1'), set()) is False


def test_process_events_seeds_without_checkpointing() -> None:
    sync = _SpyCheckpoints()
    seen: set[str] = set()
    terminated = asyncio.run(
        entrypoint._process_events(_aiter([_idle_end_turn('e1'), _idle_end_turn('e2')]), sync, seen, checkpoint=False)
    )
    assert terminated is False
    assert sync.count == 0  # the first pass seeds the high-water mark; the restore already reflects it
    assert seen == {'e1', 'e2'}


def test_process_events_checkpoints_only_the_gap_on_reconnect() -> None:
    sync = _SpyCheckpoints()
    seen = {'e1'}  # e1 was seen before the drop; e2 landed during the gap
    terminated = asyncio.run(
        entrypoint._process_events(_aiter([_idle_end_turn('e1'), _idle_end_turn('e2')]), sync, seen, checkpoint=True)
    )
    assert terminated is False
    assert sync.count == 1  # only the unseen gap boundary checkpoints
    assert seen == {'e1', 'e2'}


def test_process_events_returns_true_on_terminated() -> None:
    sync = _SpyCheckpoints()
    terminated = asyncio.run(entrypoint._process_events(_aiter([_terminated('e1')]), sync, set(), checkpoint=True))
    assert terminated is True


def test_watch_turns_propagates_unrecoverable_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint, '_RECONNECT_BACKOFF_S', 0)
    events = _FakeEvents(stream_effects=[_status_error(403)])
    monkeypatch.setattr(entrypoint.anthropic, 'AsyncAnthropic', lambda **_kw: _FakeClient(events))
    with pytest.raises(anthropic.APIStatusError):
        asyncio.run(entrypoint._watch_turns('https://up', 'sess', 'key', _SpyCheckpoints()))


def test_watch_turns_reconnects_on_retryable_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint, '_RECONNECT_BACKOFF_S', 0)
    # A 429 reconnects rather than propagating; a later CancelledError ends the watcher.
    events = _FakeEvents(stream_effects=[_status_error(429), asyncio.CancelledError()])
    monkeypatch.setattr(entrypoint.anthropic, 'AsyncAnthropic', lambda **_kw: _FakeClient(events))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(entrypoint._watch_turns('https://up', 'sess', 'key', _SpyCheckpoints()))
    assert events.stream_calls == 2  # reconnected past the 429


def test_watch_turns_checkpoints_boundary_then_returns_on_terminated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint, '_RECONNECT_BACKOFF_S', 0)
    sync = _SpyCheckpoints()
    events = _FakeEvents(stream_effects=[[_idle_end_turn('e1'), _terminated('e2')]])
    monkeypatch.setattr(entrypoint.anthropic, 'AsyncAnthropic', lambda **_kw: _FakeClient(events))
    asyncio.run(entrypoint._watch_turns('https://up', 'sess', 'key', sync))  # returns cleanly, no exception
    assert sync.count == 2  # the end_turn boundary, then the final flush on termination
    assert events.stream_calls == 1  # returned without reconnecting


def test_watch_turns_returns_on_terminated_without_a_trailing_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint, '_RECONNECT_BACKOFF_S', 0)
    sync = _SpyCheckpoints()
    events = _FakeEvents(stream_effects=[[_terminated('e1')]])
    monkeypatch.setattr(entrypoint.anthropic, 'AsyncAnthropic', lambda **_kw: _FakeClient(events))
    asyncio.run(entrypoint._watch_turns('https://up', 'sess', 'key', sync))
    assert sync.count == 1  # a final checkpoint even when the session ends mid-turn
    assert events.stream_calls == 1


def test_watch_turns_shuts_down_on_terminated_in_history(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint, '_RECONNECT_BACKOFF_S', 0)
    sync = _SpyCheckpoints()
    # The session terminated during a drop gap: on reconnect the terminated event is in the history
    # re-page, so the watcher shuts down without ever streaming the already-ended session.
    events = _FakeEvents(history=[_terminated('e1')])
    monkeypatch.setattr(entrypoint.anthropic, 'AsyncAnthropic', lambda **_kw: _FakeClient(events))
    asyncio.run(entrypoint._watch_turns('https://up', 'sess', 'key', sync))
    assert sync.count == 1  # the final flush on termination
    assert events.stream_calls == 0  # never streamed the terminated session
