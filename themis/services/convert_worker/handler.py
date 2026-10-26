"""The `/convert` conversion decision, over the bucket port so it is testable without an HTTP server.

A pushed Cloud Task delivers `POST /convert` with a `WorkRequest` (`work.proto`) as its body, and
`process_conversion` maps the outcome to the status Cloud Tasks reads as its retry signal: 200 for a
settled paper, 400 for a body the enqueuer malformed, and a propagating raise for anything a later
attempt could clear. Cloud Tasks retries every non-2xx, so 400 does not stop redelivery — it
distinguishes an enqueuer bug from a conversion failure in the log. The design is `docs/design/evidence-fulltext.md`.

Cloud Run's IAM verifies the task's OIDC token before the request arrives, so there is no in-app auth
check. `produce` is awaited directly and blocks the event loop for the conversion's duration, which is
sound only while the service sets `max_instance_request_concurrency=1` and declares no liveness probe
(`infra/themis_infra/convert.py`); both would have to change together.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

import protovalidate
from google.cloud import storage as gcs
from google.protobuf import message

from themis.litcache import outcome as outcome_mod
from themis.litcache import produce as produce_mod
from themis.litcache import work
from themis.litcache.models import work_pb2

_OK = 200
_BAD_REQUEST = 400

_logger = logging.getLogger(__name__)

# The producer seam: `produce_full_text(bucket, doc_id) -> Readiness`. Injected so the status mapping
# is testable offline (a fake returns a Readiness or raises) and an integration test can wire the real
# producer with fake fetch/convert.
Producer = Callable[[gcs.Bucket, str], Awaitable[outcome_mod.Readiness]]


def _legacy_doc_id(body: bytes) -> str | None:
    """The `doc_id` of a JSON `{"doc_id"}` body, the shape tasks enqueued before `WorkRequest` carry.

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
    return doc_id


def _requested_doc_id(body: bytes) -> str | None:
    """The `doc_id` of a `WorkRequest` body, if it is well formed and asks for what this worker runs."""
    try:
        request = work_pb2.WorkRequest.FromString(body)
        protovalidate.validate(request)
    except (message.DecodeError, protovalidate.ValidationError):
        _logger.warning('rejecting a /convert task whose body is not a valid WorkRequest')
        return None
    if list(request.targets) != [work.MAIN_TEXT_TARGET]:
        # Until the worker runs steps, it produces the main text and nothing else; any other request
        # would be answered with work it did not ask for.
        _logger.warning('rejecting a /convert task for %s naming targets %s', request.doc_id, list(request.targets))
        return None
    return request.doc_id


async def process_conversion(bucket: gcs.Bucket, body: bytes, *, content_type: str, produce: Producer) -> int:
    """Produce full text for the paper the task body names; return the HTTP status Cloud Tasks reads.

    Args:
        bucket: The litcache bucket the paper lives in.
        body: The raw task body: a `WorkRequest` naming the main-text target, or, from a task enqueued
            before the request was a proto, JSON `{"doc_id": <non-empty string>}`.
        content_type: The request's media type, which says which of the two the body is.
        produce: The producer to run, with its converter already bound.

    Returns:
        200 once the paper has settled — a rendering produced, a terminal marker written, or no such
        paper in the corpus; 400 for a body that is malformed, or a request for anything but the main
        text.

    Raises:
        Exception: Whatever `produce` raises (a fetch/convert/GCS failure) propagates so the HTTP layer
            returns 500 and Cloud Tasks retries.
    """
    if content_type == work.WORK_REQUEST_MEDIA_TYPE:
        doc_id = _requested_doc_id(body)
    elif content_type == 'application/json':
        doc_id = _legacy_doc_id(body)
    else:
        _logger.warning('rejecting a /convert task with media type %r', content_type)
        doc_id = None
    if doc_id is None:
        return _BAD_REQUEST
    try:
        readiness = await produce(bucket, doc_id)
    except produce_mod.PaperNotInCorpusError:
        # The corpus holds no such paper, and object reads are strongly consistent, so retrying spends
        # the whole budget on a task that cannot succeed. Settled, like the enqueuer's other bugs.
        # Every other NotFound — a seed blob the manifest names but the bucket lacks — is operational
        # and propagates as a 500, so the task retries rather than being dropped undiagnosed.
        _logger.error('dropping a /convert task for %s: no such paper in the corpus', doc_id)
        return _OK
    _logger.info('converted %s -> %s', doc_id, readiness.value)
    return _OK
