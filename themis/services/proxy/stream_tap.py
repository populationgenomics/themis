"""Detect the ``end_turn`` idle on the forwarded Anthropic SSE stream (self-hosted-sandbox.md §4, §9).

The proxy reverse-proxies the worker's session event stream, so the checkpoint is driven off that same
stream: an SSE decoder accumulates forwarded chunks into events and reports the idle-with-``end_turn``
that starts the release grace. The exact event field names are verify-at-build (§12) — isolated in
``_is_end_turn`` so only it changes once confirmed against the live session stream.
"""

from __future__ import annotations

import json

_DATA_PREFIX = b'data:'
_EVENT_SEPARATOR = b'\n\n'


class EndTurnDetector:
    """Feeds forwarded SSE chunks; reports when an idle/``end_turn`` event completes."""

    def __init__(self) -> None:
        self._buffer = b''

    def feed(self, chunk: bytes) -> bool:
        """Return whether a chunk completed an idle/``end_turn`` event (buffers partial events)."""
        self._buffer += chunk
        fired = False
        while _EVENT_SEPARATOR in self._buffer:
            raw_event, self._buffer = self._buffer.split(_EVENT_SEPARATOR, 1)
            if _is_end_turn(_event_data(raw_event)):
                fired = True
        return fired


def _event_data(raw_event: bytes) -> object:
    data = b''.join(
        line[len(_DATA_PREFIX) :].strip() for line in raw_event.split(b'\n') if line.startswith(_DATA_PREFIX)
    )
    if not data:
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def _is_end_turn(event: object) -> bool:
    return (
        isinstance(event, dict)
        and event.get('type') == 'session.status_idle'
        and event.get('stop_reason') == 'end_turn'
    )
