"""The sweep: ask for every paper on which a step has work, so no paper waits on a request that never came.

A writer asks for the paper it commits, but a writer can crash between its commit and its request, a
request can be lost, and a request refused at the spend limit ends with its step undone
(`docs/design/evidence-fulltext.md` § Every trigger asks the same question). The sweep reads every
paper's manifest and job log on a schedule and asks again, on the bulk queue, under a task name of its
own pass (`enqueue.task_id`):

- naming the main text, for a paper that is not READY and on which a step towards the main text has work;
- naming no target, for any other paper on which a free step has work, and for a paper whose routes have
  all settled but whose terminal marker was never written.

It decides with the worker's own step tests (`produce.outstanding`), so the two cannot disagree about
what a paper still needs. A paper it cannot read is counted and passed over, so one corrupt object does
not starve the papers listed after it. Run as `python -m themis.litcache.sweep`, by a Cloud Run job on a
schedule.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime
import logging
import os
import sys
from collections.abc import Callable, Iterator

import protovalidate
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from google.cloud import tasks_v2
from google.protobuf import message

from themis.litcache import enqueue, joblog, outcome, produce, work, writer
from themis.litcache.models import litcache_pb2, work_pb2

_LOG = logging.getLogger(__name__)

_PAPERS_PREFIX = f'{writer.PAPERS_DIR}/'
# Papers read at once: each is two small reads, so a pass is bounded by round trips, not bandwidth.
_READ_WORKERS = 32
# Cloud Tasks refusals no other request in the pass would escape: a missing grant, or no such queue.
_REFUSED_FOR_EVERY_REQUEST = (api_exceptions.PermissionDenied, api_exceptions.NotFound)


@dataclasses.dataclass
class SweepReport:
    """What one pass did.

    Attributes:
        papers: Papers read.
        asked: Requests created.
        already_asked: Requests whose name was already taken, by this pass run twice or a task in flight.
        not_asked: Requests that could not be created; the next pass asks again.
        unreadable: Papers whose manifest or job log could not be read, or whose manifest is not its
            directory's; logged, and passed over.
    """

    papers: int = 0
    asked: int = 0
    already_asked: int = 0
    not_asked: int = 0
    unreadable: int = 0


@dataclasses.dataclass(frozen=True)
class _Paper:
    manifest: litcache_pb2.Manifest
    log: work_pb2.JobLog


def sweep_pass(now: datetime.datetime) -> str:
    """The pass name for a sweep started at `now`: the minute, in UTC, as a task id carries it."""
    return now.astimezone(datetime.UTC).strftime('%Y%m%dT%H%M')


def request_for(
    manifest: litcache_pb2.Manifest, log: work_pb2.JobLog, *, now: datetime.datetime, deadline: datetime.timedelta
) -> work_pb2.WorkRequest | None:
    """What the sweep asks for `manifest`'s paper, or None when no step has work for it."""
    outstanding = produce.outstanding(manifest, log, now=now, deadline=deadline)
    if outstanding.main_text:
        return work_pb2.WorkRequest(doc_id=manifest.doc_id, targets=[work.MAIN_TEXT_TARGET])
    if outstanding.free:
        return work_pb2.WorkRequest(doc_id=manifest.doc_id)
    return None


def _committed_papers(bucket: gcs.Bucket) -> Iterator[str]:
    for blob in bucket.list_blobs(prefix=_PAPERS_PREFIX, match_glob=f'{_PAPERS_PREFIX}*/{writer.MANIFEST_NAME}'):
        parts = blob.name[len(_PAPERS_PREFIX) :].split('/')
        if len(parts) == 2 and parts[1] == writer.MANIFEST_NAME:  # exactly `{doc_id}/manifest.pb`
            yield parts[0]


def _read(bucket: gcs.Bucket, doc_id: str) -> _Paper | str:
    """The paper's manifest and job log, or why they cannot be read."""
    try:
        manifest = litcache_pb2.Manifest.FromString(bucket.blob(writer.manifest_path(doc_id)).download_as_bytes())
        log = joblog.read(bucket, doc_id)
    except message.DecodeError as e:
        return f'does not parse: {e}'
    if manifest.doc_id != doc_id:
        return f'its manifest names doc_id {manifest.doc_id!r}'
    return _Paper(manifest, log)


def _request(
    bucket: gcs.Bucket, paper: _Paper, *, now: datetime.datetime, deadline: datetime.timedelta
) -> work_pb2.WorkRequest | None:
    request = request_for(paper.manifest, paper.log, now=now, deadline=deadline)
    if request is not None or not produce.awaits_marker(paper.manifest, paper.log):
        return request
    if outcome.read_outcome(bucket, paper.manifest.doc_id) is not None:
        return None
    # Every route settled and no marker was written: the worker writes it once asked.
    return work_pb2.WorkRequest(doc_id=paper.manifest.doc_id)


def sweep(
    bucket: gcs.Bucket,
    enqueuer: enqueue.Enqueuer,
    *,
    now: datetime.datetime,
    deadline: datetime.timedelta = work.DISPATCH_DEADLINE,
) -> SweepReport:
    """Read every committed paper, and ask for each one on which a step has work.

    Args:
        bucket: The store.
        enqueuer: The enqueuer on the bulk queue.
        now: When the pass started: its name, and the time begun attempts' ages are read against.
        deadline: The dispatch deadline, past which a begun attempt has been killed.

    Returns:
        What the pass did.

    Raises:
        google.api_core.exceptions.GoogleAPIError: If the store cannot be read, or Cloud Tasks refuses a
            request in a way every other request in the pass would meet too (a missing grant, no such queue).
            Any other request that cannot be created is counted instead.
    """
    report = SweepReport()
    name = sweep_pass(now)
    doc_ids = list(_committed_papers(bucket))
    with concurrent.futures.ThreadPoolExecutor(max_workers=_READ_WORKERS) as pool:
        for doc_id, paper in zip(doc_ids, pool.map(lambda d: _read(bucket, d), doc_ids), strict=True):
            report.papers += 1
            if isinstance(paper, str):
                _LOG.error('passing over %s: %s', doc_id, paper)
                report.unreadable += 1
                continue
            request = _request(bucket, paper, now=now, deadline=deadline)
            if request is None:
                continue
            try:
                created = enqueuer.enqueue(request, sweep_pass=name)
            except protovalidate.ValidationError as e:
                _LOG.error('passing over %s: its request breaks its constraints: %s', doc_id, e)
                report.unreadable += 1
                continue
            except _REFUSED_FOR_EVERY_REQUEST:
                raise
            except api_exceptions.GoogleAPIError as e:
                _LOG.warning('could not ask for %s: %r', doc_id, e)
                report.not_asked += 1
                continue
            if created:
                report.asked += 1
            else:
                report.already_asked += 1
    return report


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


def main(clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC)) -> int:
    """Run one pass over the store the environment names; return the exit status."""
    logging.basicConfig(level=logging.INFO)
    target = enqueue.ConversionTarget(
        queue_path=_require('THEMIS_WORK_BULK_QUEUE'),
        worker_url=_require('THEMIS_WORK_WORKER_URL'),
        invoker_service_account_email=_require('THEMIS_WORK_INVOKER_SA_EMAIL'),
    )
    bucket = gcs.Client().bucket(_require('THEMIS_FULLTEXT_BUCKET'))
    report = sweep(bucket, enqueue.Enqueuer(tasks_v2.CloudTasksClient(), target), now=clock())
    _LOG.info('sweep: %s', report)
    # A request that could not be created waits for the next pass, and an unreadable paper for a person; failing the
    # job surfaces either.
    return 1 if report.not_asked or report.unreadable else 0


if __name__ == '__main__':
    sys.exit(main())
