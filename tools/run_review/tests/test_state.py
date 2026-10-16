from __future__ import annotations

import datetime
from collections.abc import Mapping

import pytest

from tools.run_review.run import state

START = datetime.datetime(2026, 10, 5, 9, 0, tzinfo=datetime.UTC)


def at(minute: int, event: Mapping[str, object]) -> dict[str, object]:
    return {**event, 'processed_at': (START + datetime.timedelta(minutes=minute)).isoformat()}


KICKOFF: dict[str, object] = {'type': 'user.message', 'id': 'k'}
RUNNING: dict[str, object] = {'type': 'session.status_running', 'id': 'r'}


def idle(stop_reason: str) -> dict[str, object]:
    return {'type': 'session.status_idle', 'id': f'i-{stop_reason}', 'stop_reason': {'type': stop_reason}}


def error(retry_status: str) -> dict[str, object]:
    return {
        'type': 'session.error',
        'id': f'e-{retry_status}',
        'error': {'type': 'model_overloaded_error', 'message': 'busy', 'retry_status': {'type': retry_status}},
    }


def timed(*events: Mapping[str, object]) -> list[dict[str, object]]:
    """`events` processed a minute apart, in the order given."""
    return [at(minute, event) for minute, event in enumerate(events)]


@pytest.mark.parametrize(
    ('tail', 'kind'),
    [
        ([RUNNING, idle('requires_action'), RUNNING], state.Kind.WORKING),
        ([RUNNING, idle('requires_action'), RUNNING, idle('end_turn')], state.Kind.ENDED),
        ([RUNNING, error('exhausted'), idle('retries_exhausted')], state.Kind.FAILED),
        ([RUNNING, {'type': 'session.status_terminated', 'id': 't'}], state.Kind.FAILED),
        ([RUNNING, idle('budget_reached')], state.Kind.UNMODELLED),
        ([RUNNING, idle('a_reason_not_yet_invented')], state.Kind.UNMODELLED),
        ([RUNNING, error('retrying'), {'type': 'session.status_rescheduled', 'id': 's'}, RUNNING], state.Kind.WORKING),
        ([RUNNING, {'type': 'session.status_paused', 'id': 'p'}], state.Kind.UNMODELLED),
        ([RUNNING, error('a_retry_status_not_yet_invented')], state.Kind.UNMODELLED),
    ],
    ids=[
        'shell call',
        'end turn',
        'idle on exhausted retries',
        'terminated',
        'budget',
        'unknown stop reason',
        'rescheduled after a retried error',
        'unmodelled status',
        'unmodelled retry status',
    ],
)
def test_a_run_is_read_from_its_session_status_events(tail: list[dict[str, object]], kind: state.Kind) -> None:
    events = timed(idle('end_turn'), KICKOFF, *tail)
    read = state.read(events)
    assert read.kind is kind
    if kind is not state.Kind.WORKING:
        assert read.decided_by in events[2:]


@pytest.mark.parametrize('retry_status', ['retrying', 'exhausted', 'terminal'])
def test_an_error_is_recorded_and_decides_nothing_on_its_own(retry_status: str) -> None:
    """An error may be a child thread's while the coordinator carries on; the status that follows decides."""
    failed = error(retry_status)
    still_going = state.read(timed(KICKOFF, RUNNING, failed))
    assert (still_going.kind, still_going.errors) == (state.Kind.WORKING, (at(2, failed),))
    assert state.read(timed(KICKOFF, RUNNING, failed, RUNNING, idle('end_turn'))).kind is state.Kind.ENDED


def test_an_idle_counts_as_an_end_only_after_the_session_ran_after_the_kickoff() -> None:
    assert state.read(timed(KICKOFF, idle('end_turn'))).kind is state.Kind.WORKING
    assert state.read(timed(RUNNING, idle('end_turn'), KICKOFF)).kind is state.Kind.WORKING
    assert state.read(timed(KICKOFF, idle('end_turn'), RUNNING, idle('end_turn'))).kind is state.Kind.ENDED


def test_the_kickoff_is_placed_by_when_it_was_processed_not_where_it_is_listed() -> None:
    kickoff, running, end = at(1, KICKOFF), at(2, RUNNING), at(3, idle('end_turn'))
    assert state.read([running, end, kickoff]).kind is state.Kind.ENDED
    late_kickoff = at(5, KICKOFF)
    assert state.read([late_kickoff, running, end]).kind is state.Kind.WORKING


def test_a_kickoff_still_queued_anchors_nothing() -> None:
    assert state.read([KICKOFF, at(1, RUNNING), at(2, idle('end_turn'))]).kind is state.Kind.WORKING


def test_the_first_deciding_event_decides() -> None:
    read = state.read(timed(KICKOFF, RUNNING, idle('end_turn'), RUNNING, idle('budget_reached')))
    assert read.kind is state.Kind.ENDED


def test_events_that_are_not_session_statuses_decide_nothing() -> None:
    content: list[dict[str, object]] = [
        {'type': 'agent.message', 'id': 'm'},
        {'type': 'session.thread_status_idle', 'id': 'ti', 'stop_reason': {'type': 'end_turn'}},
        {'type': 'span.model_request_end', 'id': 'sp'},
    ]
    assert state.read(timed(KICKOFF, RUNNING, *content)).kind is state.Kind.WORKING
