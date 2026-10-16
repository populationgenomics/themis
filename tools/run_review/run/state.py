"""Whether a run has ended, read from its session's raw event log.

The session's status aggregates its threads, so the session-level status events say when the agent
and every thread it spawned have stopped. The read anchors on the kickoff, the session's first
`user.message`, by the time the platform processed it, and only status events processed from then on
count. The session is idle before its first message as well, so an idle counts as an end only once
the session has reported running after the kickoff. An error event is recorded and decides nothing:
it carries no thread id, so it may be a child thread's while the coordinator carries on, and the
session-level status that follows says what the run came to. The first status after the kickoff that
decides anything decides the run; an event this module does not model stops the deciding rather than
being guessed at (docs/design/run-review-loop.md §"When a run has ended").

Events are the log's raw JSON objects, as the SDK dumps them, so this reads exactly what collection
writes to disk.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from collections.abc import Mapping, Sequence

Event = Mapping[str, object]

_KICKOFF = 'user.message'
_STATUS_PREFIX = 'session.status_'
_RUNNING = 'session.status_running'
_RESCHEDULED = 'session.status_rescheduled'
_IDLE = 'session.status_idle'
_TERMINATED = 'session.status_terminated'
_ERROR = 'session.error'
# `retrying`: the platform retries. `exhausted`: the session idles with `retries_exhausted` next.
# `terminal`: the session terminates next.
_RETRY_STATUSES = frozenset({'retrying', 'exhausted', 'terminal'})


class Kind(enum.Enum):
    WORKING = 'working'
    # The agent handed the turn back and waits for a user; in a round nobody answers.
    ENDED = 'ended'
    FAILED = 'failed'
    # An event the harness has no model of; it stops deciding rather than guess what it means.
    UNMODELLED = 'unmodelled'


@dataclasses.dataclass(frozen=True)
class State:
    """Where a run stands, the event that decided it, and every error the session reported after the kickoff."""

    kind: Kind
    decided_by: Event | None
    errors: tuple[Event, ...]


def processed_at(event: Event) -> datetime.datetime | None:
    """When the platform processed `event`; None while it is queued, or where the log names no time."""
    value = event.get('processed_at')
    return datetime.datetime.fromisoformat(value) if isinstance(value, str) else None


def read(events: Sequence[Event]) -> State:
    """The run's state from its session's events, in any order."""
    processed: list[tuple[datetime.datetime, Event]] = []
    for event in events:
        at = processed_at(event)
        if at is not None:
            processed.append((at, event))
    processed.sort(key=lambda item: item[0])
    kickoff = next(((at, event) for at, event in processed if event.get('type') == _KICKOFF), None)
    if kickoff is None:
        return State(Kind.WORKING, None, ())
    kickoff_at, kickoff_event = kickoff
    # At or after: an event the platform stamped with the kickoff's own instant may be listed before it.
    after = [event for at, event in processed if at >= kickoff_at and event is not kickoff_event]
    errors: list[Event] = []
    ran = False
    for event in after:
        kind = event.get('type')
        if kind == _ERROR:
            errors.append(event)
            if _nested_type(event, 'error', 'retry_status') not in _RETRY_STATUSES:
                return State(Kind.UNMODELLED, event, tuple(errors))
            continue
        if not isinstance(kind, str):
            return State(Kind.UNMODELLED, event, tuple(errors))
        if not kind.startswith(_STATUS_PREFIX):
            continue
        decided = _status(kind, event, ran=ran)
        if kind == _RUNNING:
            ran = True
        if decided is not None:
            return State(decided, event, tuple(errors))
    return State(Kind.WORKING, None, tuple(errors))


def _status(kind: str, event: Event, *, ran: bool) -> Kind | None:
    """What one session status means for the run; None for one that decides nothing."""
    if kind in (_RUNNING, _RESCHEDULED):
        return None
    if kind == _TERMINATED:
        return Kind.FAILED
    if kind != _IDLE:
        return Kind.UNMODELLED
    stop_reason = _nested_type(event, 'stop_reason')
    if stop_reason == 'requires_action':
        # The worker is answering a shell call.
        return None
    if stop_reason == 'end_turn':
        # Before the session ran after the kickoff, this is its idle before the first message.
        return Kind.ENDED if ran else None
    if stop_reason == 'retries_exhausted':
        return Kind.FAILED
    return Kind.UNMODELLED


def _nested_type(event: Event, *path: str) -> str | None:
    """The `type` of the object at `path` inside `event`, or None where any step is not an object."""
    node: object = event
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    if not isinstance(node, Mapping):
        return None
    kind = node.get('type')
    return kind if isinstance(kind, str) else None
