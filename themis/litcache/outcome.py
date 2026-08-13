"""Full-text readiness + the terminal-outcome sidecar, derived from the litcache layout.

A paper's full text is produced lazily: an OA fetch or a PDF conversion adds a rendering to the
manifest (`writer.add_rendering`). Readiness is derived from the `papers/{doc_id}/` layout with no
separate status store (`docs/design/evidence-fulltext.md`):

- the manifest lists a rendering ⇒ **READY** — a rendering is committed into the manifest only after
  its blob is written, so `manifest.renderings` non-empty *is* the rendering-present signal (one read,
  no extra blob probe);
- a `.fetch_outcome` sidecar is present ⇒ its **terminal** reason (NO_FULL_TEXT / FAILED) — the stop
  condition, written once when the fetch/convert path gives up;
- neither ⇒ **PENDING** — production has not been attempted, or has not settled.

Both terminal states are marker-only. The manifest records what a paper *has*, not what has been tried
on it, and `produce.produce_full_text` is the one place that knows: it walks the ladder and writes
NO_FULL_TEXT when no rung served — including when the manifest gives it no rung to attempt at all. The
reader waits for that marker rather than re-deriving the ladder's entry conditions.

GATED (a rendering exists but its source is access-gated under an enforced licence) is a property of
`Source.access`, not this sidecar; it folds in when licence enforcement is wired.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import json
import posixpath

from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs

from themis.litcache import writer
from themis.litcache.models import litcache_pb2

_OUTCOME_NAME = '.fetch_outcome'
_PAPERS_PREFIX = 'papers'


class Readiness(enum.Enum):
    """Whether a paper's full text is available, being produced, or permanently unavailable."""

    READY = 'ready'
    PENDING = 'pending'
    NO_FULL_TEXT = 'no_full_text'
    FAILED = 'failed'


class OutcomeKind(enum.Enum):
    """The terminal reason recorded in the sidecar. GATED is not here — it derives from the manifest."""

    NO_FULL_TEXT = 'no_full_text'  # the OA ladder served no source; nothing to convert
    FAILED = 'failed'  # a conversion attempt failed and will not be retried


@dataclasses.dataclass(frozen=True)
class FetchOutcome:
    """The terminal outcome of the fetch/convert path for a paper, stored in the sidecar.

    Attributes:
        kind: Why full text is unavailable.
        at: When the terminal outcome was recorded.
        error: A human-readable reason (empty when none applies, e.g. a clean no-source).
    """

    kind: OutcomeKind
    at: datetime.datetime
    error: str = ''


def terminal_readiness(outcome: FetchOutcome) -> Readiness:
    """The `Readiness` a terminal sidecar marker maps to (FAILED vs NO_FULL_TEXT)."""
    return Readiness.FAILED if outcome.kind is OutcomeKind.FAILED else Readiness.NO_FULL_TEXT


def _outcome_path(doc_id: str) -> str:
    return posixpath.join(_PAPERS_PREFIX, doc_id, _OUTCOME_NAME)


def write_outcome(bucket: gcs.Bucket, doc_id: str, outcome: FetchOutcome) -> None:
    """Record a paper's terminal fetch/convert outcome in its `.fetch_outcome` sidecar.

    A fresh object, never a manifest edit — so it sidesteps the manifest RMW and cannot race a
    rendering write. Overwrites any prior marker (a later attempt's outcome supersedes an earlier one).
    """
    payload = {
        'kind': outcome.kind.value,
        'at': outcome.at.isoformat(),
        'error': outcome.error,
    }
    bucket.blob(_outcome_path(doc_id)).upload_from_string(json.dumps(payload), content_type='application/json')


def read_outcome(bucket: gcs.Bucket, doc_id: str) -> FetchOutcome | None:
    """The paper's terminal outcome, or None when no sidecar is present."""
    blob = bucket.blob(_outcome_path(doc_id))
    try:
        raw = blob.download_as_bytes()
    except api_exceptions.NotFound:
        return None
    payload = json.loads(raw)
    return FetchOutcome(
        kind=OutcomeKind(payload['kind']),
        at=datetime.datetime.fromisoformat(payload['at']),
        error=payload.get('error', ''),
    )


def read_readiness(bucket: gcs.Bucket, doc_id: str) -> Readiness | None:
    """Derive full-text readiness for `doc_id` from the litcache layout, or None for an unknown paper.

    None (no manifest) is a distinct axis from readiness — the caller maps it to "unknown paper".
    """
    manifest_blob = bucket.blob(writer.manifest_path(doc_id))  # writer owns the manifest key
    try:
        manifest_bytes = manifest_blob.download_as_bytes()
    except api_exceptions.NotFound:
        return None
    manifest = litcache_pb2.Manifest.FromString(manifest_bytes)
    if manifest.renderings:
        return Readiness.READY
    outcome = read_outcome(bucket, doc_id)
    if outcome is not None:
        return terminal_readiness(outcome)
    return Readiness.PENDING
