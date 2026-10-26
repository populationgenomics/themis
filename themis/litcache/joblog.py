"""A paper's job log: what the worker has tried on each step, and whether a step has work.

The manifest records what a paper has; the log records what has been tried on it, so that a step which ran and produced
nothing is not run again, and a spending step that is running, or keeps dying, is not paid for again
(`docs/design/evidence-fulltext.md` § The job log records what the worker has tried). The log lives beside the manifest
as `papers/{doc_id}/job_log.pb`, a serialized `JobLog`.

Every question about the log is about the set of entries for a step on a set of inputs, never their order: entries are
appended in whatever order workers reach the object, with times from their own clocks. `assess` answers the question;
`begin` asks it and appends a begun entry under one compare-and-swap, so two attempts cannot both begin a spending
step; `append` records an event that has already happened. Appending writes the old bytes followed by the new
entries' encoding, so nothing already written is decoded again, and fields a newer worker wrote survive.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import posixpath
from collections.abc import Collection, Iterable, Sequence

import protovalidate
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from google.protobuf import timestamp_pb2

from themis.litcache import writer
from themis.litcache.models import work_pb2

JOB_LOG_NAME = 'job_log.pb'

# Attempts on one step's inputs that may spend and commit nothing before the step settles as a permanent failure.
SPENT_ATTEMPTS_LIMIT = 3

_APPEND_ATTEMPTS = 5

# The events that close an attempt's begun entry. One a later version adds is not among them, so to an older reader
# it closes nothing: a begun entry it cannot see closed reads as running, then as dead, and an older worker errs
# towards spending less.
_CLOSING_EVENTS = frozenset({work_pb2.JOB_EVENT_SETTLED, work_pb2.JOB_EVENT_REFUSED, work_pb2.JOB_EVENT_FAILED})


class Verdict(enum.Enum):
    """What the log says of a step on its inputs, for one attempt."""

    WORK = 'work'  # nothing stops the step, and nothing is begun
    BEGUN = 'begun'  # this attempt holds the step's open begun entry
    RUNNING = 'running'  # another live attempt holds an open begun entry; leave the step to it
    SETTLED = 'settled'  # the step ended without a record; it has no work
    SPENT = 'spent'  # enough attempts have spent and committed nothing; the step is to settle as failed


class LogContentionError(Exception):
    """An append lost its generation race `_APPEND_ATTEMPTS` times — **retryable**."""


@dataclasses.dataclass(frozen=True)
class Attempt:
    """One request the worker handled, and the Cloud Tasks delivery it served.

    Attributes:
        id: A uuid the worker minted for the request.
        queue: The delivery's queue (`X-CloudTasks-QueueName`).
        task: The delivery's task name (`X-CloudTasks-TaskName`).
        retry_count: The queue's retry count for the delivery (`X-CloudTasks-TaskRetryCount`).
    """

    id: str
    queue: str
    task: str
    retry_count: int

    def entry(
        self,
        *,
        at: datetime.datetime,
        step: str,
        inputs: Iterable[str],
        event: work_pb2.JobEvent,
        outcome: work_pb2.SettledOutcome = work_pb2.SETTLED_OUTCOME_UNSPECIFIED,
        model_answered: bool = False,
        detail: str = '',
    ) -> work_pb2.JobLogEntry:
        """An entry this attempt writes about `step` on `inputs`; `append` validates it.

        Raises:
            ValueError: If `at` is naive, which a timestamp would read as UTC whatever zone it meant.
        """
        _require_aware(at)
        stamp = timestamp_pb2.Timestamp()
        stamp.FromDatetime(at)
        return work_pb2.JobLogEntry(
            at=stamp,
            attempt=self.id,
            queue=self.queue,
            task=self.task,
            retry_count=self.retry_count,
            step=step,
            inputs=sorted(inputs),
            event=event,
            outcome=outcome,
            model_answered=model_answered,
            detail=detail,
        )

    def follows_delivery_of(self, entry: work_pb2.JobLogEntry) -> bool:
        """Whether this attempt serves a later delivery of `entry`'s task, so that delivery has ended."""
        return entry.queue == self.queue and entry.task == self.task and entry.retry_count < self.retry_count


def job_log_path(doc_id: str) -> str:
    """The job log key for `doc_id`, beside its manifest."""
    return posixpath.join(writer.paper_dir(doc_id), JOB_LOG_NAME)


def assess(
    log: work_pb2.JobLog,
    *,
    step: str,
    inputs: Collection[str],
    attempt: Attempt,
    now: datetime.datetime,
    deadline: datetime.timedelta,
) -> Verdict:
    """What `log` says of `step` on `inputs`, for `attempt`.

    Only entries for `step` on exactly the set `inputs` count. An attempt's begun entry is closed by a settled,
    refused or failed entry from the same attempt; an event this reader does not know closes nothing and settles
    nothing. An open begun entry is dead once older than `deadline`, or when an earlier delivery of `attempt`'s own
    task wrote it; otherwise another attempt's is running. An attempt has spent when it died, or failed after the
    model answered.

    Args:
        log: The paper's job log.
        step: The step asked about.
        inputs: The hashes of the inputs the step would read now.
        attempt: The attempt asking.
        now: The current time, compared with each begun entry's.
        deadline: The dispatch deadline: an attempt older than this has been killed.

    Returns:
        SETTLED if the step settled; BEGUN if `attempt` holds an open begun entry; RUNNING if another live attempt
        does; SPENT if `SPENT_ATTEMPTS_LIMIT` attempts have spent; WORK otherwise.

    Raises:
        ValueError: If `now` is naive.
    """
    _require_aware(now)
    wanted = set(inputs)
    by_attempt: dict[str, list[work_pb2.JobLogEntry]] = {}
    for entry in log.entries:
        if entry.step == step and set(entry.inputs) == wanted:
            by_attempt.setdefault(entry.attempt, []).append(entry)
    entries = [entry for group in by_attempt.values() for entry in group]
    if any(entry.event == work_pb2.JOB_EVENT_SETTLED for entry in entries):
        return Verdict.SETTLED

    running = False
    spent = 0
    for attempt_id, group in by_attempt.items():
        begun = [entry for entry in group if entry.event == work_pb2.JOB_EVENT_BEGUN]
        closing = [entry for entry in group if entry.event in _CLOSING_EVENTS]
        if begun and not closing:
            if attempt_id == attempt.id:
                return Verdict.BEGUN
            if any(_dead(entry, attempt, now, deadline) for entry in begun):
                spent += 1
            else:
                running = True
        elif any(entry.event == work_pb2.JOB_EVENT_FAILED and entry.model_answered for entry in closing):
            spent += 1
    if running:
        return Verdict.RUNNING
    if spent >= SPENT_ATTEMPTS_LIMIT:
        return Verdict.SPENT
    return Verdict.WORK


def failed_attempts(log: work_pb2.JobLog, *, step: str, inputs: Collection[str]) -> int:
    """How many attempts closed `step` on exactly the set `inputs` with a failed entry, paid for or not."""
    wanted = set(inputs)
    return len(
        {
            e.attempt
            for e in log.entries
            if e.step == step and set(e.inputs) == wanted and e.event == work_pb2.JOB_EVENT_FAILED
        }
    )


def settlement(log: work_pb2.JobLog, *, step: str, inputs: Collection[str]) -> work_pb2.JobLogEntry | None:
    """The entry settling `step` on exactly the set `inputs`, if one does; a permanent failure before any other."""
    wanted = set(inputs)
    settled = [
        e for e in log.entries if e.step == step and set(e.inputs) == wanted and e.event == work_pb2.JOB_EVENT_SETTLED
    ]
    return min(settled, key=lambda e: e.outcome != work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE, default=None)


def _dead(begun: work_pb2.JobLogEntry, attempt: Attempt, now: datetime.datetime, deadline: datetime.timedelta) -> bool:
    return now - begun.at.ToDatetime(tzinfo=datetime.UTC) > deadline or attempt.follows_delivery_of(begun)


def _require_aware(at: datetime.datetime) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f'{at!r} names no time zone')


def read(bucket: gcs.Bucket, doc_id: str) -> work_pb2.JobLog:
    """`doc_id`'s job log; an empty one when nothing has been logged.

    Raises:
        google.protobuf.message.DecodeError: If the stored log does not parse.
    """
    return _read(bucket.blob(job_log_path(doc_id)))[0]


def append(bucket: gcs.Bucket, doc_id: str, entries: Sequence[work_pb2.JobLogEntry]) -> None:
    """Append `entries`, events that have already happened, to `doc_id`'s job log.

    A lost race rereads the log and appends the same entries, whatever it finds: nothing in the log can undo an event.

    Raises:
        ValueError: If `entries` is empty, or an entry breaks its constraints.
        LogContentionError: If every append attempt lost its generation race (retryable).
    """
    if not entries:
        raise ValueError(f'nothing to append to the job log of {doc_id}')
    encoded = _encode(entries)
    for _ in range(_APPEND_ATTEMPTS):
        blob = bucket.blob(job_log_path(doc_id))
        _, held, generation = _read(blob)
        if _write(blob, held + encoded, generation):
            return
    raise LogContentionError(f'appending to the job log of {doc_id} did not converge in {_APPEND_ATTEMPTS} attempts')


def begin(
    bucket: gcs.Bucket,
    doc_id: str,
    *,
    step: str,
    inputs: Collection[str],
    attempt: Attempt,
    now: datetime.datetime,
    deadline: datetime.timedelta,
) -> Verdict:
    """Begin `step` on `inputs` for `attempt`, if the log leaves it work: a check and an append under one CAS.

    Reads the log, and when `assess` finds WORK, appends `attempt`'s begun entry on condition that the log has not
    changed since. A lost race rereads and assesses again, so of two attempts beginning the same step on the same
    inputs, the second reads the first's entry. A retry after this attempt's own append landed finds its entry, and is
    BEGUN.

    Args:
        bucket: The cache bucket.
        doc_id: The paper.
        step: The spending step to begin.
        inputs: The hashes of the inputs the step will read.
        attempt: The attempt beginning it.
        now: The time, written on the begun entry and compared with others'.
        deadline: The dispatch deadline (`assess`).

    Returns:
        BEGUN when `attempt` holds the begun entry; otherwise the verdict that stopped it.

    Raises:
        ValueError: If the begun entry breaks its constraints, or `attempt` has already closed `step` on `inputs`: an
            attempt begins a step at most once, since a second begun entry beside a closing one would read as closed.
        LogContentionError: If every attempt lost its generation race (retryable).
    """
    encoded = _encode([attempt.entry(at=now, step=step, inputs=inputs, event=work_pb2.JOB_EVENT_BEGUN)])
    for _ in range(_APPEND_ATTEMPTS):
        blob = bucket.blob(job_log_path(doc_id))
        log, held, generation = _read(blob)
        if any(
            e.attempt == attempt.id and e.step == step and set(e.inputs) == set(inputs) and e.event in _CLOSING_EVENTS
            for e in log.entries
        ):
            raise ValueError(f'attempt {attempt.id} has already closed {step} on these inputs for {doc_id}')
        verdict = assess(log, step=step, inputs=inputs, attempt=attempt, now=now, deadline=deadline)
        if verdict is not Verdict.WORK:
            return verdict
        if _write(blob, held + encoded, generation):
            return Verdict.BEGUN
    raise LogContentionError(f'beginning {step} for {doc_id} did not converge in {_APPEND_ATTEMPTS} attempts')


def _encode(entries: Sequence[work_pb2.JobLogEntry]) -> bytes:
    for entry in entries:
        try:
            protovalidate.validate(entry)
        except protovalidate.ValidationError as e:
            rules = '; '.join(f'{v.proto.rule_id}: {v.proto.message}' for v in e.violations)
            raise ValueError(f'job log entry for {entry.step!r} breaks its constraints: {rules}') from e
    return work_pb2.JobLog(entries=entries).SerializeToString()


def _read(blob: gcs.Blob) -> tuple[work_pb2.JobLog, bytes, int]:
    """The log, its bytes, and the generation they were read at; 0, the generation of an absent object, if absent."""
    try:
        held = blob.download_as_bytes()
    except api_exceptions.NotFound:
        return work_pb2.JobLog(), b'', 0
    if blob.generation is None:
        raise RuntimeError(f'{blob.name} was read with no generation')
    return work_pb2.JobLog.FromString(held), held, blob.generation


def _write(blob: gcs.Blob, data: bytes, generation: int) -> bool:
    """Write `data` if the log is still at `generation`; return whether it was."""
    try:
        blob.upload_from_string(data, if_generation_match=generation)
    except api_exceptions.PreconditionFailed:
        return False
    return True
