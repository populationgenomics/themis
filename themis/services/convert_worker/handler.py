"""The `/convert` request decision, over the bucket port so it is testable without an HTTP server.

A pushed Cloud Task delivers `POST /convert` with a `WorkRequest` (`work.proto`) as its body, and
`process_conversion` maps the outcome to the status Cloud Tasks reads as its retry signal: 200 once
every step the request reaches is done, settled or running elsewhere, 400 for a request the enqueuer
malformed, and a propagating raise for anything a later attempt could clear. Cloud Tasks retries
every non-2xx, so 400 does not stop redelivery — it distinguishes an enqueuer bug from a conversion
failure in the log. The design is `docs/design/evidence-fulltext.md`.

The request is one attempt in the paper's job log: an id minted here, and the delivery it serves,
read off Cloud Tasks' request headers.

Cloud Run's IAM verifies the task's OIDC token before the request arrives, so there is no in-app auth
check. `produce` is awaited directly and blocks the event loop for the conversion's duration, which is
sound only while the service sets `max_instance_request_concurrency=1` and declares no liveness probe
(`infra/themis_infra/convert.py`); both would have to change together.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Collection, Mapping
from typing import Protocol

import protovalidate
from google.cloud import storage as gcs
from google.protobuf import message

from themis.litcache import joblog, work
from themis.litcache import outcome as outcome_mod
from themis.litcache import produce as produce_mod
from themis.litcache.models import work_pb2

_OK = 200
_BAD_REQUEST = 400

# The headers Cloud Tasks sets on an HTTP target's request, naming the delivery.
_QUEUE_HEADER = 'X-CloudTasks-QueueName'
_TASK_HEADER = 'X-CloudTasks-TaskName'
_RETRY_COUNT_HEADER = 'X-CloudTasks-TaskRetryCount'
# A retry count as Cloud Tasks writes it, and small enough for the job log's uint32.
_RETRY_COUNT = re.compile(r'[0-9]{1,9}')

_logger = logging.getLogger(__name__)


class Producer(Protocol):
    """The producer seam, `produce.run` with its converter bound; injected so the status mapping is testable offline."""

    def __call__(
        self, bucket: gcs.Bucket, doc_id: str, /, *, targets: Collection[str], attempt: joblog.Attempt
    ) -> Awaitable[outcome_mod.Readiness]: ...


@dataclasses.dataclass(frozen=True)
class _Request:
    doc_id: str
    targets: tuple[str, ...]


def _legacy_request(body: bytes) -> _Request | None:
    """The request a JSON `{"doc_id"}` body makes, the shape tasks enqueued before `WorkRequest` carry: the main text.

    Read only for those tasks: once this worker has been deployed for longer than the queue's retry
    window, none can remain, and this goes.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _logger.warning('rejecting a /convert task with a non-JSON body')
        return None
    doc_id = payload.get('doc_id') if isinstance(payload, dict) else None
    if not isinstance(doc_id, str) or not doc_id:
        _logger.warning('rejecting a /convert task with no doc_id')
        return None
    return _Request(doc_id=doc_id, targets=(work.MAIN_TEXT_TARGET,))


def _work_request(body: bytes) -> _Request | None:
    """The request a `WorkRequest` body makes, if it is well formed and names only targets this worker runs."""
    try:
        request = work_pb2.WorkRequest.FromString(body)
        protovalidate.validate(request)
    except (message.DecodeError, protovalidate.ValidationError):
        _logger.warning('rejecting a /convert task whose body is not a valid WorkRequest')
        return None
    unknown = set(request.targets) - produce_mod.TARGETS
    if unknown:
        _logger.warning('rejecting a /convert task for %s naming unknown targets %s', request.doc_id, sorted(unknown))
        return None
    return _Request(doc_id=request.doc_id, targets=tuple(request.targets))


def _attempt(headers: Mapping[str, str]) -> joblog.Attempt | None:
    """This request as the job log names it: a fresh id, and the delivery Cloud Tasks' headers name."""
    queue, task, retry_count = (headers.get(h) for h in (_QUEUE_HEADER, _TASK_HEADER, _RETRY_COUNT_HEADER))
    if not queue or not task or retry_count is None or not _RETRY_COUNT.fullmatch(retry_count):
        _logger.warning(
            'rejecting a /convert request with no Cloud Tasks delivery (%s=%r, %s=%r, %s=%r)',
            _QUEUE_HEADER,
            queue,
            _TASK_HEADER,
            task,
            _RETRY_COUNT_HEADER,
            retry_count,
        )
        return None
    return joblog.Attempt(id=str(uuid.uuid4()), queue=queue, task=task, retry_count=int(retry_count))


async def process_conversion(
    bucket: gcs.Bucket, body: bytes, *, content_type: str, headers: Mapping[str, str], produce: Producer
) -> int:
    """Run the paper the task body names up to date; return the HTTP status Cloud Tasks reads.

    Args:
        bucket: The litcache bucket the paper lives in.
        body: The raw task body: a `WorkRequest`, or, from a task enqueued before the request was a
            proto, JSON `{"doc_id": <non-empty string>}`, which asks for the main text.
        content_type: The request's media type, which says which of the two the body is.
        headers: The request's headers, which name the Cloud Tasks delivery.
        produce: The producer to run, with its converter already bound.

    Returns:
        200 once the producer returns, or for a paper the corpus does not hold; 400 for a body that is
        malformed, a target no step answers to, or a request Cloud Tasks did not deliver.

    Raises:
        Exception: Whatever `produce` raises (a fetch/convert/GCS failure) propagates so the HTTP layer
            returns 500 and Cloud Tasks retries.
    """
    if content_type == work.WORK_REQUEST_MEDIA_TYPE:
        request = _work_request(body)
    elif content_type == 'application/json':
        request = _legacy_request(body)
    else:
        _logger.warning('rejecting a /convert task with media type %r', content_type)
        request = None
    attempt = _attempt(headers)
    if request is None or attempt is None:
        return _BAD_REQUEST
    try:
        readiness = await produce(bucket, request.doc_id, targets=request.targets, attempt=attempt)
    except produce_mod.PaperNotInCorpusError:
        # The corpus holds no such paper, and object reads are strongly consistent, so retrying spends
        # the whole budget on a task that cannot succeed. Settled, like the enqueuer's other bugs.
        # Every other NotFound — a seed blob the manifest names but the bucket lacks — is operational
        # and propagates as a 500, so the task retries rather than being dropped undiagnosed.
        _logger.error('dropping a /convert task for %s: no such paper in the corpus', request.doc_id)
        return _OK
    _logger.info('ran %s for %s -> %s', request.doc_id, list(request.targets), readiness.value)
    return _OK
