"""The `/convert` conversion decision, over the bucket port so it is testable without an HTTP server.

A pushed Cloud Task delivers `POST /convert {"doc_id": ...}` and `process_conversion` maps the outcome
to the status Cloud Tasks reads as its retry signal: 200 for a settled paper, 400 for a body the
enqueuer malformed, and a propagating raise for anything a later attempt could clear. Cloud Tasks
retries every non-2xx, so 400 does not stop redelivery — it distinguishes an enqueuer bug from a
conversion failure in the log. The design is `docs/design/evidence-fulltext.md`.

Cloud Run's IAM verifies the task's OIDC token before the request arrives, so there is no in-app auth
check. `produce` is awaited directly and blocks the event loop for the conversion's duration, which is
sound only while the service sets `max_instance_request_concurrency=1` and declares no liveness probe
(`infra/themis_infra/convert.py`); both would have to change together.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from google.cloud import storage as gcs

from themis.litcache import outcome as outcome_mod
from themis.litcache import produce as produce_mod

_OK = 200
_BAD_REQUEST = 400

_logger = logging.getLogger(__name__)

# The producer seam: `produce_full_text(bucket, doc_id) -> Readiness`. Injected so the status mapping
# is testable offline (a fake returns a Readiness or raises) and an integration test can wire the real
# producer with fake fetch/convert.
Producer = Callable[[gcs.Bucket, str], Awaitable[outcome_mod.Readiness]]


async def process_conversion(bucket: gcs.Bucket, body: bytes, *, produce: Producer) -> int:
    """Produce full text for the `doc_id` in the task body; return the HTTP status Cloud Tasks reads.

    Args:
        bucket: The litcache bucket the paper lives in.
        body: The raw task body — JSON `{"doc_id": <non-empty string>}`.
        produce: The producer to run, with its converter already bound.

    Returns:
        200 once the paper has settled — a rendering produced, a terminal marker written, or no such
        paper in the corpus; 400 if the body is not JSON or carries no `doc_id`.

    Raises:
        Exception: Whatever `produce` raises (a fetch/convert/GCS failure) propagates so the HTTP layer
            returns 500 and Cloud Tasks retries.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # A non-JSON body, or one that is not even UTF-8, is malformed — 400, never a conversion.
        _logger.warning('rejecting a /convert task with a non-JSON body')
        return _BAD_REQUEST
    doc_id = payload.get('doc_id') if isinstance(payload, dict) else None
    if not isinstance(doc_id, str) or not doc_id:
        _logger.warning('rejecting a /convert task with no doc_id')
        return _BAD_REQUEST
    try:
        readiness = await produce(bucket, doc_id)
    except produce_mod.UnknownPaperError:
        # The corpus holds no such paper, and object reads are strongly consistent, so retrying spends
        # the whole budget on a task that cannot succeed. Settled, like the enqueuer's other bugs.
        # Every other NotFound — a seed blob the manifest names but the bucket lacks — is operational
        # and propagates as a 500, so the task retries rather than being dropped undiagnosed.
        _logger.error('dropping a /convert task for unknown doc_id %s', doc_id)
        return _OK
    _logger.info('converted %s -> %s', doc_id, readiness.value)
    return _OK
