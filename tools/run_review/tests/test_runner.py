"""The run half against a scripted session: when it interrupts, what it records, when it collects."""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
from collections.abc import Mapping
from typing import override

import pytest

from themis.workbench.models import workbench_pb2
from tools.run_review.run import runner, sessions

START = datetime.datetime(2026, 10, 5, 9, 0, tzinfo=datetime.UTC)
COMMIT = 'c' * 40
DIGEST = 'sha256:' + 'd' * 64
TIMING = runner.Timing(
    deadline=datetime.timedelta(minutes=60),
    settle=datetime.timedelta(minutes=3),
    poll_every=datetime.timedelta(minutes=1),
)
INPUTS = workbench_pb2.AnalysisInputs(
    variant_classification=workbench_pb2.VariantClassificationInputs(
        transcript='NM_000138.5', hgvs_c='c.7003C>T', clinical_context='proband_narrative: tall'
    )
)
REQUEST = runner.RunRequest(project_id='p', inputs=INPUTS)


class UnreachableError(Exception):
    """What the fakes raise for a call that may succeed if made again."""


class RefusedError(Exception):
    """What the fake app raises for a request it refused before acting on it."""


def idle(stop_reason: str) -> dict[str, object]:
    return {'type': 'session.status_idle', 'id': f'idle-{stop_reason}', 'stop_reason': {'type': stop_reason}}


RUNNING: dict[str, object] = {'type': 'session.status_running', 'id': 'running'}


class Clock:
    def __init__(self) -> None:
        self.at = START

    def now(self) -> datetime.datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += datetime.timedelta(seconds=seconds)


@dataclasses.dataclass
class Session(sessions.Sessions):
    """A session whose events are processed at fixed offsets from the start; an interrupt ends it a minute later."""

    clock: Clock
    script: list[tuple[datetime.timedelta, Mapping[str, object]]]
    # Reads of the log that fail, transiently, before one succeeds.
    unreachable_reads: int = 0
    # Events listed with no `processed_at` until their offset: queued behind earlier ones.
    queued: frozenset[str] = frozenset()
    thread_ids: tuple[object, ...] = ('thread_main', 'thread_reviewer')

    def end_after_interrupt(self) -> None:
        offset = self.clock.at - START + datetime.timedelta(minutes=1)
        self.script.append((offset, {**idle('end_turn'), 'id': f'idle-after-interrupt-{offset}'}))

    def _listed(self) -> list[dict[str, object]]:
        listed: list[dict[str, object]] = []
        for offset, event in sorted(self.script, key=lambda item: item[0]):
            if START + offset <= self.clock.at:
                listed.append({**event, 'processed_at': (START + offset).isoformat()})
            elif event.get('id') in self.queued:
                listed.append({**event, 'processed_at': None})
        return listed

    @override
    def session(self, session_id: str) -> dict[str, object]:
        return {
            'id': session_id,
            'agent': {
                'id': 'agent_1',
                'version': 7,
                'model': {'id': 'claude-model'},
                'skills': [{'type': 'custom', 'skill_id': 's', 'version': '3'}],
            },
        }

    @override
    def events(self, session_id: str, *, since: datetime.datetime | None) -> list[dict[str, object]]:
        if self.unreachable_reads:
            self.unreachable_reads -= 1
            raise UnreachableError('the API is unreachable')
        return [
            event
            for event in self._listed()
            if since is None
            or event['processed_at'] is None
            or datetime.datetime.fromisoformat(str(event['processed_at'])) >= since
        ]

    @override
    def threads(self, session_id: str) -> list[dict[str, object]]:
        return [{'id': thread_id} for thread_id in self.thread_ids]

    @override
    def thread_events(self, session_id: str, thread_id: str) -> list[dict[str, object]]:
        late: dict[str, object] = {
            'type': 'agent.message',
            'id': 'late',
            'processed_at': (START + datetime.timedelta(minutes=9)).isoformat(),
        }
        early: dict[str, object] = {'type': 'agent.message', 'id': 'early', 'processed_at': START.isoformat()}
        return [late, early]

    @override
    def agent_metadata(self, agent_id: str, version: int) -> dict[str, str]:
        return {'skill:classifying': f's@3 {DIGEST} {COMMIT}'}


@dataclasses.dataclass
class App:
    """The BFF: it lists every Analysis it created, and can fail a create after making it."""

    session: Session
    analyses: list[workbench_pb2.Analysis] = dataclasses.field(default_factory=list)
    interrupted: list[datetime.datetime] = dataclasses.field(default_factory=list)
    # How many interrupts the session ignores before it idles; a session whose worker died ignores all.
    ignored_interrupts: int = 0
    unreachable_interrupts: int = 0
    # Listing fails once the run's Analysis exists, between the create and the read of its session.
    list_fails_once_created: bool = False
    create_fails_after_creating: bool = False
    create_refused: bool = False
    # How far the app's clock is behind the harness's, as the Analysis's `created_at` shows it.
    clock_behind: datetime.timedelta = datetime.timedelta(0)

    def __post_init__(self) -> None:
        other = workbench_pb2.Analysis(id='an_other', session_id='sesn_other', inputs=INPUTS)
        other.created_at.FromDatetime(START - datetime.timedelta(days=1))
        self.analyses.append(other)

    @property
    def created(self) -> int:
        return len(self.analyses) - 1

    def list_analyses(self, project_id: str) -> workbench_pb2.ListAnalysesResponse:
        if self.list_fails_once_created and self.created:
            raise UnreachableError('the BFF is unreachable')
        return workbench_pb2.ListAnalysesResponse(analyses=self.analyses)

    def create_analysis(self, request: workbench_pb2.CreateAnalysisRequest) -> workbench_pb2.CreateAnalysisResponse:
        if self.create_refused:
            raise RefusedError('invalid_argument: the request was refused at the boundary')
        n = self.created + 1
        analysis = workbench_pb2.Analysis(id=f'an_{n}', session_id=f'sesn_{n}', inputs=request.inputs)
        analysis.created_at.FromDatetime(self.session.clock.at - self.clock_behind)
        self.analyses.append(analysis)
        if self.create_fails_after_creating:
            raise UnreachableError('the answer to CreateAnalysis was lost')
        return workbench_pb2.CreateAnalysisResponse(id=analysis.id)

    def interrupt(self, analysis_id: str) -> None:
        if self.unreachable_interrupts:
            self.unreachable_interrupts -= 1
            raise UnreachableError('the BFF is unreachable')
        self.interrupted.append(self.session.clock.at)
        if self.ignored_interrupts:
            self.ignored_interrupts -= 1
        else:
            self.session.end_after_interrupt()


@dataclasses.dataclass
class Harness:
    clock: Clock
    session: Session
    app: App
    cloned_at: list[datetime.datetime]
    clone_fails: bool = False
    logged: list[str] = dataclasses.field(default_factory=list)

    def deps(self) -> runner.Deps:
        return runner.Deps(
            workbench=self.app,
            sessions=self.session,
            clone=self.clone,
            now=self.clock.now,
            sleep=self.clock.sleep,
            log=self.logged.append,
            our_skills={'s': 'classifying'},
            transient=(UnreachableError,),
            rejected=(RefusedError,),
        )

    def clone(self, analysis_id: str, destination: pathlib.Path) -> None:
        if self.clone_fails:
            raise runner.CloneError('git clone failed')
        destination.mkdir(parents=True)
        self.cloned_at.append(self.clock.at)

    def execute(self, run_dir: pathlib.Path) -> runner.RunRecord:
        return runner.execute(REQUEST, run_dir, timing=TIMING, deps=self.deps())


def harness(*script: tuple[int, Mapping[str, object]]) -> Harness:
    clock = Clock()
    session = Session(clock, [(datetime.timedelta(minutes=minutes), event) for minutes, event in script])
    return Harness(clock, session, App(session), [])


KICKOFF = (0, {'type': 'user.message', 'id': 'k'})
WORKING = (1, RUNNING)
SHELL_CALL = (5, idle('requires_action'))


def _ending(record: runner.RunRecord) -> str:
    assert record.ending is not None
    return record.ending.kind


def test_a_run_that_ends_is_collected_after_the_settle_period(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, SHELL_CALL, (41, idle('end_turn')))
    record = h.execute(tmp_path)
    assert _ending(record) == 'ended'
    assert record.interrupts == []
    assert h.cloned_at[0] >= START + datetime.timedelta(minutes=41) + TIMING.settle
    assert record.collected_at is not None
    assert record.provenance is not None
    assert record.provenance['agent_version'] == 7
    assert (tmp_path / runner.EVENTS).read_text('utf-8').count('\n') == 4
    assert {path.name for path in (tmp_path / runner.THREADS_DIR).iterdir()} == {
        'thread_main.jsonl',
        'thread_reviewer.jsonl',
    }


def test_thread_events_are_written_oldest_first(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, (10, idle('end_turn')))
    h.execute(tmp_path)
    lines = (tmp_path / runner.THREADS_DIR / 'thread_main.jsonl').read_text('utf-8').splitlines()
    assert [json.loads(line)['id'] for line in lines] == ['early', 'late']


def test_a_thread_without_an_id_fails_the_collection_loudly(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, (10, idle('end_turn')))
    h.session.thread_ids = ('thread_main', None)
    with pytest.raises(runner.RunError, match='lists a thread whose id cannot name a file'):
        h.execute(tmp_path)


def test_a_run_past_its_deadline_is_interrupted_once_and_still_collected(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, SHELL_CALL)
    record = h.execute(tmp_path)
    assert len(h.app.interrupted) == 1
    assert h.app.interrupted[0] >= START + TIMING.deadline
    assert len(record.interrupts) == 1
    assert _ending(record) == 'ended'
    assert record.collected_at is not None


def test_an_unanswered_interrupt_is_sent_again(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, SHELL_CALL)
    h.app.ignored_interrupts = 1
    record = h.execute(tmp_path)
    assert len(record.interrupts) == 2
    assert record.collected_at is not None


def test_an_interrupt_the_session_never_answers_stops_the_run_uncollected(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, SHELL_CALL)
    h.app.ignored_interrupts = 1_000
    record = h.execute(tmp_path)
    assert _ending(record) == runner.INTERRUPT_UNANSWERED
    assert 1 < len(record.interrupts) == len(h.app.interrupted) < 10
    assert record.stopped
    assert h.cloned_at == []


def test_an_event_the_harness_does_not_model_stops_the_deciding_and_the_deadline_still_interrupts(
    tmp_path: pathlib.Path,
) -> None:
    h = harness(KICKOFF, WORKING, (3, idle('budget_reached')), (20, RUNNING), (30, idle('end_turn')))
    record = h.execute(tmp_path)
    assert record.stopped
    assert record.ending is not None
    assert record.ending.event is not None
    assert record.ending.event['id'] == 'idle-budget_reached'
    assert len(h.app.interrupted) == 1
    assert h.app.interrupted[0] >= START + TIMING.deadline
    assert record.collected_at is None


def test_a_stopped_run_is_read_again_on_the_next_pass(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, SHELL_CALL)
    h.app.ignored_interrupts = 1_000
    first = h.execute(tmp_path)
    h.session.end_after_interrupt()
    h.clock.sleep(datetime.timedelta(minutes=2).total_seconds())
    record = h.execute(tmp_path)
    assert len(record.interrupts) == len(first.interrupts)
    assert (record.stopped, record.collected_at is not None) == (False, True)


def test_transient_failures_in_the_watch_are_retried_and_logged(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, SHELL_CALL)
    h.session.unreachable_reads = 3
    h.app.unreachable_interrupts = 2
    record = h.execute(tmp_path)
    assert record.collected_at is not None
    assert len(record.interrupts) == 1
    assert sum('failed' in line for line in h.logged) == 5


def test_an_interrupt_that_always_fails_ends_the_watch_while_reads_succeed(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, SHELL_CALL)
    h.app.unreachable_interrupts = 1_000
    with pytest.raises(runner.RunError, match='the interrupt failed 11 times in a row'):
        h.execute(tmp_path)
    record = runner.load(tmp_path)
    assert record is not None
    assert record.failure is not None
    assert record.interrupts == []


def test_a_run_going_long_past_its_deadline_ends_unanswered_however_the_interrupts_fared(
    tmp_path: pathlib.Path,
) -> None:
    """A pass resumed long after the deadline gives the run up rather than start interrupting afresh."""
    h = harness(KICKOFF, WORKING, SHELL_CALL)
    h.app.list_fails_once_created = True
    with pytest.raises(UnreachableError):
        h.execute(tmp_path)
    h.app.list_fails_once_created = False
    h.clock.sleep(datetime.timedelta(hours=3).total_seconds())
    record = h.execute(tmp_path)
    assert _ending(record) == runner.INTERRUPT_UNANSWERED
    assert h.cloned_at == []


def test_transient_failures_past_the_bound_fail_the_run_and_are_recorded(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, SHELL_CALL)
    h.session.unreachable_reads = 1_000
    with pytest.raises(runner.RunError, match='failed 11 times in a row'):
        h.execute(tmp_path)
    record = runner.load(tmp_path)
    assert record is not None
    assert record.failure is not None
    assert 'reading the event log failed 11 times in a row' in record.failure


def test_a_kickoff_listed_while_queued_still_anchors_the_run_once_processed(tmp_path: pathlib.Path) -> None:
    """An event read again with its processing time replaces the queued listing of it."""
    h = harness(WORKING, SHELL_CALL, (6, {'type': 'user.message', 'id': 'k'}), (7, RUNNING), (20, idle('end_turn')))
    h.session.queued = frozenset({'k'})
    record = h.execute(tmp_path)
    assert _ending(record) == 'ended'
    assert record.ending is not None
    assert record.ending.observed_at >= (START + datetime.timedelta(minutes=20)).isoformat()


def test_a_create_whose_answer_was_lost_is_found_rather_than_sent_again(tmp_path: pathlib.Path) -> None:
    """The match is by the Analyses listed before the create, so the app's clock does not enter it."""
    h = harness(KICKOFF, WORKING, (10, idle('end_turn')))
    h.app.clock_behind = datetime.timedelta(hours=2)
    h.app.create_fails_after_creating = True
    with pytest.raises(UnreachableError):
        h.execute(tmp_path)
    failed = runner.load(tmp_path)
    assert failed is not None
    assert (failed.created, failed.failure) == (None, 'UnreachableError: the answer to CreateAnalysis was lost')
    h.app.create_fails_after_creating = False
    record = h.execute(tmp_path)
    assert h.app.created == 1
    assert record.created is not None
    assert (record.created.analysis_id, record.session_id, record.failure) == ('an_1', 'sesn_1', None)
    assert record.collected_at is not None


@pytest.mark.parametrize('listed', [0, 2], ids=['none', 'two'])
def test_a_lost_create_that_does_not_match_exactly_one_analysis_is_refused(tmp_path: pathlib.Path, listed: int) -> None:
    h = harness(KICKOFF)
    h.app.create_fails_after_creating = True
    with pytest.raises(UnreachableError):
        h.execute(tmp_path)
    del h.app.analyses[1:]
    h.app.create_fails_after_creating = False
    for _ in range(listed):
        h.app.create_analysis(REQUEST.create_request())
    with pytest.raises(runner.RunError, match=f'lists {listed} new Analyses with these inputs'):
        h.execute(tmp_path)


def test_a_create_the_app_refused_leaves_no_intent_and_the_next_pass_creates(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, (10, idle('end_turn')))
    h.app.create_refused = True
    with pytest.raises(RefusedError):
        h.execute(tmp_path)
    refused = runner.load(tmp_path)
    assert refused is not None
    assert (refused.intent, refused.created) == (None, None)
    h.app.create_refused = False
    record = h.execute(tmp_path)
    assert h.app.created == 1
    assert record.collected_at is not None


def test_a_run_created_before_the_harness_failed_is_not_created_again(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, (10, idle('end_turn')))
    h.app.list_fails_once_created = True
    with pytest.raises(UnreachableError):
        h.execute(tmp_path)
    h.app.list_fails_once_created = False
    record = h.execute(tmp_path)
    assert h.app.created == 1
    assert (record.session_id, record.collected_at is not None) == ('sesn_1', True)


def test_a_clone_left_by_an_unrecorded_pass_is_replaced(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, (10, idle('end_turn')))
    h.execute(tmp_path)
    record = runner.load(tmp_path)
    assert record is not None
    record.collected_at = None
    (tmp_path / runner.RUN_RECORD).write_text(json.dumps(dataclasses.asdict(record)), 'utf-8')
    (tmp_path / runner.REPOSITORY / 'half-written').write_text('', 'utf-8')
    collected = h.execute(tmp_path)
    assert collected.collected_at is not None
    assert len(h.cloned_at) == 2
    assert not (tmp_path / runner.REPOSITORY / 'half-written').exists()


def test_a_failed_clone_is_recorded_and_retried_on_the_next_pass(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, (10, idle('end_turn')))
    h.clone_fails = True
    record = h.execute(tmp_path)
    assert record.collection_error == 'git clone failed'
    assert record.collected_at is None
    h.clone_fails = False
    retried = h.execute(tmp_path)
    assert h.app.created == 1
    assert (retried.collection_error, retried.collected_at is not None) == (None, True)
    assert runner.load(tmp_path) == retried


def test_a_superseded_run_is_kept_aside_with_what_was_derived_from_it(tmp_path: pathlib.Path) -> None:
    run_dir = tmp_path / 'run'
    h = harness(KICKOFF, WORKING, (10, idle('end_turn')))
    first = h.execute(run_dir)
    (tmp_path / 'review.md').write_text('Sound: as the curators\n', 'utf-8')
    moved = runner.supersede(run_dir, h.clock.now(), derived=[tmp_path / 'review.md', tmp_path / 'rendered'])
    assert runner.load(moved / 'run') == first
    assert (moved / 'review.md').is_file()
    assert not (tmp_path / 'review.md').exists()
    second = h.execute(run_dir)
    assert h.app.created == 2
    assert second.created is not None
    assert second.created.analysis_id == 'an_2'


def test_superseding_a_run_that_does_not_exist_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(runner.RunError, match='holds no run to move aside'):
        runner.supersede(tmp_path / 'run', START, derived=[])


def test_a_run_record_from_other_inputs_is_refused(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF, WORKING, (10, idle('end_turn')))
    h.app.list_fails_once_created = True
    with pytest.raises(UnreachableError):
        h.execute(tmp_path)
    other = runner.RunRequest(project_id='p', inputs=workbench_pb2.AnalysisInputs())
    with pytest.raises(runner.RunError, match='created from other inputs'):
        runner.execute(other, tmp_path, timing=TIMING, deps=h.deps())


def test_an_analysis_the_project_does_not_list_is_refused(tmp_path: pathlib.Path) -> None:
    h = harness(KICKOFF)

    class Unlisted(App):
        @override
        def list_analyses(self, project_id: str) -> workbench_pb2.ListAnalysesResponse:
            return workbench_pb2.ListAnalysesResponse()

    h.app = Unlisted(h.session)
    with pytest.raises(runner.RunError, match='does not list the created Analysis'):
        h.execute(tmp_path)


def test_a_settle_period_below_the_floor_is_refused() -> None:
    with pytest.raises(ValueError, match='below the floor'):
        runner.Timing(
            deadline=TIMING.deadline,
            settle=runner.SETTLE_FLOOR - datetime.timedelta(seconds=1),
            poll_every=TIMING.poll_every,
        )
