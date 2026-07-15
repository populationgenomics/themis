"""Tests for the SSE end_turn detector."""

from __future__ import annotations

import json

from themis.services.proxy import stream_tap


def _sse(event_type: str, **fields: str) -> bytes:
    return f'data: {json.dumps({"type": event_type, **fields})}\n\n'.encode()


def test_detects_idle_end_turn() -> None:
    detector = stream_tap.EndTurnDetector()
    assert detector.feed(_sse('session.status_idle', stop_reason='end_turn'))


def test_ignores_other_events() -> None:
    detector = stream_tap.EndTurnDetector()
    assert not detector.feed(_sse('agent.tool_use'))
    assert not detector.feed(_sse('session.status_idle', stop_reason='requires_action'))


def test_reassembles_a_split_event() -> None:
    detector = stream_tap.EndTurnDetector()
    raw = _sse('session.status_idle', stop_reason='end_turn')
    assert not detector.feed(raw[:12])  # partial event — buffered, not yet fired
    assert detector.feed(raw[12:])  # completing chunk fires
