"""One run, from creation to collection: create it as a curator would, wait for it to end, collect what it left.

The run is created with the BFF's `CreateAnalysis`. Its session's event log is read from Anthropic
until the session says the run ended (`state`). A run still going at its deadline is interrupted
through the BFF's `Interrupt`, and the record says the round sent it, since nothing in the log marks
the idle that follows as an interrupt. After the end the harness waits out the settle period, so the
worker's teardown push has landed, then writes the raw event logs, the session and its provenance, and
clones the repository. It renders nothing.

Every step is recorded in `run.json` as it happens. The intent to create comes first, with the ids of
the Analyses the Project already lists with the same inputs, so a harness that stopped between sending
`CreateAnalysis` and recording its answer finds the one Analysis new since then rather than creating a
second. A run the harness stopped on, an
event it does not model or an interrupt the session never answered, is left uncollected and read again
on the next pass. A run that raised is recorded with the cause and resumed on the next pass.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Protocol

from google.protobuf import json_format

from themis.workbench.models import workbench_pb2
from tools.run_review import rounds
from tools.run_review.run import provenance, sessions, state

RUN_RECORD = 'run.json'
SESSION = 'session.json'
EVENTS = 'events.jsonl'
THREADS_DIR = 'threads'
REPOSITORY = 'repository'

# The worker pushes about a minute after the end turn (docs/design/sandbox-worker.md); a settle
# period below this leaves the push too little margin to land before the clone.
SETTLE_FLOOR = datetime.timedelta(minutes=2)
# How long a session may take to idle after an interrupt before the round sends it again.
_INTERRUPT_GRACE = datetime.timedelta(minutes=10)
# Interrupts sent in all before a run that never idles counts as stuck; past the deadline by this many
# grace periods, it counts as stuck however many were sent.
_INTERRUPTS = 3
# Transient failures in a row of one operation, reading the log or interrupting, before the watch gives up.
_TRANSIENT_LIMIT = 10
INTERRUPT_UNANSWERED = 'interrupt_unanswered'
# The endings the harness stops on rather than collects.
_STOPPING = frozenset({state.Kind.UNMODELLED.value, INTERRUPT_UNANSWERED})
_SUPERSEDED = 'superseded-{at:%Y%m%dT%H%M%SZ}'
# Sorts an event with no `processed_at` after every processed one.
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)


class RunError(Exception):
    """The BFF or the session answered in a shape a run cannot proceed on, or the round's record of it is."""


class CloneError(Exception):
    """The repository could not be cloned; the run is recorded as uncollected."""


class Workbench(Protocol):
    def list_analyses(self, project_id: str) -> workbench_pb2.ListAnalysesResponse: ...
    def create_analysis(self, request: workbench_pb2.CreateAnalysisRequest) -> workbench_pb2.CreateAnalysisResponse: ...
    def interrupt(self, analysis_id: str) -> None: ...


@dataclasses.dataclass(frozen=True)
class RunRequest:
    """What a run is created from: the Project, and a scenario's inputs, exactly as `CreateAnalysis` takes them."""

    project_id: str
    inputs: workbench_pb2.AnalysisInputs

    def create_request(self) -> workbench_pb2.CreateAnalysisRequest:
        return workbench_pb2.CreateAnalysisRequest(project_id=self.project_id, inputs=self.inputs)


@dataclasses.dataclass(frozen=True)
class Timing:
    """The round's bounds on a run: when it is interrupted, how long the worker gets to push, how often to look.

    Raises:
        ValueError: If the settle period is below `SETTLE_FLOOR`, or the deadline or the poll interval is not
            positive.
    """

    deadline: datetime.timedelta
    settle: datetime.timedelta
    poll_every: datetime.timedelta

    def __post_init__(self) -> None:
        if self.settle < SETTLE_FLOOR:
            raise ValueError(f'a settle period of {self.settle} is below the floor of {SETTLE_FLOOR}')
        if self.deadline <= datetime.timedelta(0) or self.poll_every <= datetime.timedelta(0):
            raise ValueError('the deadline and the poll interval are positive')


@dataclasses.dataclass(frozen=True)
class Deps:
    workbench: Workbench
    sessions: sessions.Sessions
    # Clones the Analysis's repository into a directory that does not exist; raises CloneError.
    clone: Callable[[str, pathlib.Path], None]
    now: Callable[[], datetime.datetime]
    sleep: Callable[[float], None]
    log: Callable[[str], None]
    # The skills this repository publishes, by skill id, with their directories (`provenance.declared_skills`).
    our_skills: Mapping[str, str]
    # What a read of the log or an interrupt may raise that is worth trying again at the next poll.
    transient: tuple[type[Exception], ...]
    # What `create_analysis` raises when the app never received the request or refused it outright, so
    # nothing was created.
    rejected: tuple[type[Exception], ...]


@dataclasses.dataclass(frozen=True)
class Ending:
    """How a run ended: a `state.Kind` value or `interrupt_unanswered`, the event that decided it, and when."""

    kind: str
    event: rounds.Json | None
    observed_at: str

    @classmethod
    def from_json(cls, raw: object, where: str) -> Ending:
        fields = rounds.Fields(raw, where)
        ending = cls(
            kind=fields.text('kind'), event=fields.optional_mapping('event'), observed_at=fields.text('observed_at')
        )
        fields.finish()
        return ending


@dataclasses.dataclass(frozen=True)
class Created:
    """The Analysis the run is: its id, when it was created and when the round interrupts it, by the harness's clock."""

    analysis_id: str
    created_at: str
    deadline_at: str

    @classmethod
    def from_json(cls, raw: object, where: str) -> Created:
        fields = rounds.Fields(raw, where)
        created = cls(
            analysis_id=fields.text('analysis_id'),
            created_at=fields.text('created_at'),
            deadline_at=fields.text('deadline_at'),
        )
        fields.finish()
        return created


@dataclasses.dataclass(frozen=True)
class Intent:
    """A `CreateAnalysis` about to be sent: when, and the Analyses the Project listed with the same inputs before."""

    sent_at: str
    preexisting: list[str]

    @classmethod
    def from_json(cls, raw: object, where: str) -> Intent:
        fields = rounds.Fields(raw, where)
        intent = cls(sent_at=fields.text('sent_at'), preexisting=fields.texts('preexisting'))
        fields.finish()
        return intent


@dataclasses.dataclass
class RunRecord:
    """`run.json`: what the round did to one run and what it saw, written as each step happens.

    `intent` is written before `CreateAnalysis` is sent, and `created` once its answer is; an intent with
    no `created` is a create whose answer was lost. `session_id` is None until the Project's list names
    the session. `interrupts` holds the time of each interrupt the round sent. `failure` holds the cause
    of the last pass that raised.
    """

    inputs: rounds.Json
    intent: Intent | None = None
    created: Created | None = None
    session_id: str | None = None
    interrupts: list[str] = dataclasses.field(default_factory=list)
    ending: Ending | None = None
    errors: list[rounds.Json] = dataclasses.field(default_factory=list)
    collected_at: str | None = None
    collection_error: str | None = None
    provenance: rounds.Json | None = None
    failure: str | None = None

    @property
    def stopped(self) -> bool:
        """Whether the harness stopped on the run: an event it does not model, or an interrupt never answered."""
        return self.ending is not None and self.ending.kind in _STOPPING

    @classmethod
    def from_json(cls, raw: object, where: str) -> RunRecord:
        """Read back what `_save` wrote.

        Raises:
            rounds.RoundError: If a field is missing, of another type, or not one a run records.
        """
        fields = rounds.Fields(raw, where)
        intent = fields.optional_mapping('intent')
        created = fields.optional_mapping('created')
        ending = fields.optional_mapping('ending')
        record = cls(
            inputs=fields.mapping('inputs'),
            intent=None if intent is None else Intent.from_json(intent, f'{where}: `intent`'),
            created=None if created is None else Created.from_json(created, f'{where}: `created`'),
            session_id=fields.optional_text('session_id'),
            interrupts=fields.texts('interrupts'),
            ending=None if ending is None else Ending.from_json(ending, f'{where}: `ending`'),
            errors=fields.mappings('errors'),
            collected_at=fields.optional_text('collected_at'),
            collection_error=fields.optional_text('collection_error'),
            provenance=fields.optional_mapping('provenance'),
            failure=fields.optional_text('failure'),
        )
        fields.finish()
        return record


def load(run_dir: pathlib.Path) -> RunRecord | None:
    """The run's record, or None for a run not yet intended.

    Raises:
        rounds.RoundError: If the record exists and is not one.
    """
    raw = rounds.read_json(run_dir / RUN_RECORD)
    return None if raw is None else RunRecord.from_json(raw, str(run_dir / RUN_RECORD))


def _save(run_dir: pathlib.Path, record: RunRecord) -> None:
    rounds.write_json(run_dir / RUN_RECORD, dataclasses.asdict(record))


def supersede(run_dir: pathlib.Path, at: datetime.datetime, derived: Sequence[pathlib.Path]) -> pathlib.Path:
    """Move the run in `run_dir`, and what was derived from it, aside so the next `execute` creates a new one.

    Both go into one directory beside `run_dir`, stamped with `at`. The old run's session is left as it
    is; nothing is interrupted.

    Args:
        run_dir: The run's directory.
        at: The time the directory is stamped with.
        derived: Paths beside `run_dir` holding what was made from the run (a rendering, a review); those
            that exist move with it.

    Returns:
        The directory the old run and its derived files now sit in.

    Raises:
        RunError: If `run_dir` holds no run, a derived path is not beside it, or the stamped name is taken.
    """
    if not (run_dir / RUN_RECORD).is_file():
        raise RunError(f'{run_dir} holds no run to move aside')
    outside = [str(path) for path in derived if path.parent != run_dir.parent]
    if outside:
        raise RunError(f'derived paths are not beside {run_dir}: {outside}')
    target = run_dir.parent / _SUPERSEDED.format(at=at.astimezone(datetime.UTC))
    try:
        target.mkdir()
    except FileExistsError as e:
        raise RunError(f'{target} exists; a run was moved aside in this second already') from e
    for path in (run_dir, *derived):
        if path.exists():
            path.rename(target / path.name)
    return target


def execute(request: RunRequest, run_dir: pathlib.Path, *, timing: Timing, deps: Deps) -> RunRecord:
    """Create the run, or resume the one `run_dir` records, and take it as far as collection.

    Returns:
        The record. The run is collected where `collected_at` is set; otherwise `ending` or
        `collection_error` says what stopped it.

    Raises:
        RunError: If the BFF names no analysis id, an interrupted create cannot be matched to exactly one
            Analysis, the Project does not list the Analysis with a session, or the watch met more transient
            failures in a row than it allows.
        provenance.ProvenanceError: If the session's agent snapshot or the agent's records cannot be read.
        web_app.AccessRefusedError, web_app.RpcError, anthropic.APIError: From the calls, as raised.
        Each is recorded in `failure` before it propagates.
    """
    record = load(run_dir)
    if record is None:
        record = RunRecord(inputs=json_format.MessageToDict(request.inputs, preserving_proto_field_name=True))
        _save(run_dir, record)
    record.failure = None
    try:
        _advance(record, request, run_dir, timing=timing, deps=deps)
    except Exception as e:
        record.failure = f'{type(e).__name__}: {e}'
        _save(run_dir, record)
        raise
    return record


def _advance(record: RunRecord, request: RunRequest, run_dir: pathlib.Path, *, timing: Timing, deps: Deps) -> None:
    if record.collected_at is not None:
        return
    if json_format.ParseDict(record.inputs, workbench_pb2.AnalysisInputs()) != request.inputs:
        raise RunError(f'{run_dir / RUN_RECORD} was created from other inputs than the round now holds')
    if record.created is None:
        if record.intent is None:
            record.created = _create(record, request, run_dir, timing=timing, deps=deps)
        else:
            record.created, record.session_id = _reconcile(record.intent, request, timing=timing, deps=deps)
        _save(run_dir, record)
    if record.session_id is None:
        record.session_id = _session_of(record.created.analysis_id, request.project_id, deps)
        _save(run_dir, record)
    if record.ending is None or record.stopped:
        record.ending = _watch(record, record.created, record.session_id, run_dir, timing=timing, deps=deps)
        _save(run_dir, record)
    if record.stopped:
        deps.log(f'stopped, not collected: {record.ending.kind} {record.ending.event or ""}')
        return
    _settle(record.ending, timing.settle, deps)
    _collect(record, record.created.analysis_id, record.session_id, run_dir, deps)
    _save(run_dir, record)


def _create(record: RunRecord, request: RunRequest, run_dir: pathlib.Path, *, timing: Timing, deps: Deps) -> Created:
    """Record the intent, then send `CreateAnalysis`; a request the app provably did not act on clears the intent.

    Raises:
        RunError: If the BFF names no analysis id.
    """
    record.intent = Intent(sent_at=deps.now().isoformat(), preexisting=_same_inputs(request, deps))
    _save(run_dir, record)
    try:
        analysis_id = deps.workbench.create_analysis(request.create_request()).id
    except deps.rejected:
        record.intent = None
        _save(run_dir, record)
        raise
    created = deps.now()
    if not analysis_id:
        raise RunError('CreateAnalysis named no analysis id')
    deps.log(f'created {analysis_id}')
    return _created(analysis_id, created, timing)


def _same_inputs(request: RunRequest, deps: Deps) -> list[str]:
    """The ids of the Analyses the Project lists with the request's inputs, in list order."""
    return [a.id for a in deps.workbench.list_analyses(request.project_id).analyses if a.inputs == request.inputs]


def _reconcile(intent: Intent, request: RunRequest, *, timing: Timing, deps: Deps) -> tuple[Created, str | None]:
    """Find the Analysis a create whose answer was lost made: the one with its inputs the Project did not list before.

    Raises:
        RunError: If the Project lists no such Analysis, or more than one.
    """
    before = set(intent.preexisting)
    matches = [
        analysis
        for analysis in deps.workbench.list_analyses(request.project_id).analyses
        if analysis.inputs == request.inputs and analysis.id not in before
    ]
    if len(matches) != 1:
        raise RunError(
            f'CreateAnalysis was sent at {intent.sent_at} and its answer never recorded, and the Project lists '
            f'{len(matches)} new Analyses with these inputs ({", ".join(a.id for a in matches) or "none"}). '
            'Settle it by hand; `run --rerun` moves this run aside and creates a new one'
        )
    [analysis] = matches
    deps.log(f'found {analysis.id}, which an interrupted pass created')
    # The harness's clock, not the app's: the deadline is compared with the harness's own `now`.
    created = _created(analysis.id, datetime.datetime.fromisoformat(intent.sent_at), timing)
    return created, analysis.session_id or None


def _created(analysis_id: str, at: datetime.datetime, timing: Timing) -> Created:
    return Created(analysis_id=analysis_id, created_at=at.isoformat(), deadline_at=(at + timing.deadline).isoformat())


def _session_of(analysis_id: str, project_id: str, deps: Deps) -> str:
    """The session the BFF created for `analysis_id`, matched by id: another run's Analysis may be newer."""
    listed = [a for a in deps.workbench.list_analyses(project_id).analyses if a.id == analysis_id]
    if len(listed) != 1 or not listed[0].session_id:
        raise RunError(f'{project_id} does not list the created Analysis {analysis_id} with its session')
    deps.log(f'{analysis_id} runs in session {listed[0].session_id}')
    return listed[0].session_id


class _EventLog:
    """The session's events read so far, by id, each replaced by its latest listing.

    Every event is kept, whatever its type, so one the harness does not model is still in front of the
    read. The cursor is the latest `processed_at` seen; the next read asks for events at or after it, so
    an event sharing that instant is not skipped, and listing it again replaces it.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, rounds.Json] = {}

    def add(self, events: Iterable[rounds.Json]) -> None:
        for event in events:
            event_id = event.get('id')
            key = event_id if isinstance(event_id, str) and event_id else json.dumps(event, sort_keys=True)
            self._by_key[key] = event

    @property
    def events(self) -> list[rounds.Json]:
        return list(self._by_key.values())

    @property
    def cursor(self) -> datetime.datetime | None:
        times = [at for event in self._by_key.values() if (at := state.processed_at(event)) is not None]
        return max(times, default=None)


@dataclasses.dataclass
class _Transient:
    """One operation's transient failures in a row; each is logged, and one past the limit gives the run up."""

    deps: Deps
    what: str
    in_a_row: int = 0

    def failed(self, error: Exception) -> None:
        self.in_a_row += 1
        self.deps.log(f'{self.what} failed ({self.in_a_row} in a row): {type(error).__name__}: {error}')
        if self.in_a_row > _TRANSIENT_LIMIT:
            raise RunError(f'{self.what} failed {self.in_a_row} times in a row; the last: {error}') from error

    def succeeded(self) -> None:
        self.in_a_row = 0


def _watch(
    record: RunRecord, created: Created, session_id: str, run_dir: pathlib.Path, *, timing: Timing, deps: Deps
) -> Ending:
    """Read the session's events until the run ends; interrupt it at the deadline and record that it did.

    An event the harness does not model stops the deciding: the watch holds on to it, interrupts the run
    at the deadline all the same, and returns it as the ending once the interrupt is sent. A run still
    going `_INTERRUPTS` grace periods past its deadline ends as `interrupt_unanswered`, however many
    interrupts went out.

    Raises:
        RunError: If reading the log, or sending the interrupt, fails transiently more often in a row than
            `_TRANSIENT_LIMIT`.
    """
    deadline = datetime.datetime.fromisoformat(created.deadline_at)
    give_up_at = deadline + _INTERRUPT_GRACE * _INTERRUPTS
    log = _EventLog()
    reads = _Transient(deps, 'reading the event log')
    interrupts = _Transient(deps, 'the interrupt')
    unmodelled: Ending | None = None
    while True:
        at = deps.now()
        try:
            log.add(deps.sessions.events(session_id, since=log.cursor))
        except deps.transient as e:
            reads.failed(e)
        else:
            reads.succeeded()
            current = state.read(log.events)
            record.errors = [dict(event) for event in current.errors]
            decided_by = None if current.decided_by is None else dict(current.decided_by)
            if current.kind in (state.Kind.ENDED, state.Kind.FAILED):
                return Ending(kind=current.kind.value, event=decided_by, observed_at=at.isoformat())
            if current.kind is state.Kind.UNMODELLED and unmodelled is None:
                unmodelled = Ending(kind=current.kind.value, event=decided_by, observed_at=at.isoformat())
                deps.log(
                    f'an event the harness does not model; it stops deciding and waits for the deadline: {decided_by}'
                )
        if record.interrupts:
            if unmodelled is not None:
                return unmodelled
            if at - datetime.datetime.fromisoformat(record.interrupts[-1]) >= _INTERRUPT_GRACE:
                if len(record.interrupts) >= _INTERRUPTS:
                    return Ending(kind=INTERRUPT_UNANSWERED, event=None, observed_at=at.isoformat())
                _interrupt(record, created, run_dir, at, interrupts, deps)
        elif at >= deadline and _interrupt(record, created, run_dir, at, interrupts, deps) and unmodelled is not None:
            return unmodelled
        if at >= give_up_at:
            event = None if unmodelled is None else unmodelled.event
            return Ending(kind=INTERRUPT_UNANSWERED, event=event, observed_at=at.isoformat())
        deps.sleep(timing.poll_every.total_seconds())


def _interrupt(
    record: RunRecord,
    created: Created,
    run_dir: pathlib.Path,
    at: datetime.datetime,
    transient: _Transient,
    deps: Deps,
) -> bool:
    """Send an interrupt and record it; False where it failed transiently, to be sent at the next poll."""
    try:
        deps.workbench.interrupt(created.analysis_id)
    except deps.transient as e:
        transient.failed(e)
        return False
    transient.succeeded()
    record.interrupts.append(at.isoformat())
    _save(run_dir, record)
    deps.log(f'past its deadline, interrupt {len(record.interrupts)} of at most {_INTERRUPTS} sent')
    return True


def _settle(ending: Ending, settle: datetime.timedelta, deps: Deps) -> None:
    """Wait until the settle period after the end has passed, so the worker's teardown push has landed."""
    remaining = datetime.datetime.fromisoformat(ending.observed_at) + settle - deps.now()
    if remaining > datetime.timedelta(0):
        deps.sleep(remaining.total_seconds())


def _collect(record: RunRecord, analysis_id: str, session_id: str, run_dir: pathlib.Path, deps: Deps) -> None:
    """Clone the repository, then write the session, its raw event logs and its provenance.

    Raises:
        RunError: If the session lists a thread without an id that can name a file.
        provenance.ProvenanceError: If the session carries no agent snapshot, or its provenance cannot be read.
    """
    repository = run_dir / REPOSITORY
    # A clone left by a pass that stopped before recording it is not known to be whole.
    shutil.rmtree(repository, ignore_errors=True)
    try:
        deps.clone(analysis_id, repository)
    except CloneError as e:
        record.collection_error = str(e)
        deps.log(f'not collected: {e}')
        return
    session = deps.sessions.session(session_id)
    rounds.write_json(run_dir / SESSION, session)
    _write_events(run_dir / EVENTS, deps.sessions.events(session_id, since=None))
    for thread in deps.sessions.threads(session_id):
        thread_id = thread.get('id')
        if not isinstance(thread_id, str) or not thread_id or pathlib.PurePath(thread_id).name != thread_id:
            raise RunError(f'session {session_id} lists a thread whose id cannot name a file: {thread}')
        _write_events(run_dir / THREADS_DIR / f'{thread_id}.jsonl', deps.sessions.thread_events(session_id, thread_id))
    snapshot = session.get('agent')
    if not isinstance(snapshot, Mapping):
        raise provenance.ProvenanceError(f'session {session_id} carries no agent snapshot')
    record.provenance = provenance.as_json(provenance.read(snapshot, deps.sessions.agent_metadata, deps.our_skills))
    record.collection_error = None
    record.collected_at = deps.now().isoformat()
    deps.log('collected')


def _write_events(path: pathlib.Path, events: Sequence[rounds.Json]) -> None:
    """Write `events` one per line, oldest first by `processed_at`, any still queued last in listed order."""
    ordered = sorted(events, key=lambda event: (state.processed_at(event) is None, state.processed_at(event) or _EPOCH))
    rounds.publish(path, ''.join(json.dumps(event, sort_keys=True) + '\n' for event in ordered).encode('utf-8'))
