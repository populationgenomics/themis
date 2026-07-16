"""Shared GCS blob helpers for themis.

`google-cloud-storage` is the storage abstraction (mature, standard, used across themis) —
there is no themis storage port: a byte-filesystem wrapper would be reinvented at every GCS
consumer, and nothing should depend on one domain to reach GCS. This module holds only the
two operations the SDK lacks — content-addressed writes and generation-preconditioned
read-modify-write. Callers pass a `google.cloud.storage.Bucket` and use the SDK directly for
plain reads/writes/listing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from google.api_core import exceptions as api_exceptions
from google.cloud import storage


class StaleGenerationError(Exception):
    """A read-modify-write exhausted its attempts: a concurrent writer kept winning."""


def put_content_addressed(bucket: storage.Bucket, data: bytes, prefix: str, ext: str) -> str:
    """Write ``data`` under ``prefix`` at a name keyed by its content hash; return the name.

    The name is ``{prefix}/{sha256-hex}.{ext}``. Idempotent: identical bytes map to the same
    name, so a re-put when the object already exists is a no-op. The exists-then-write race is
    benign — content-addressing means any concurrent writer stores identical bytes.

    Args:
        bucket: The destination bucket.
        data: The blob bytes.
        prefix: The object-name prefix (a virtual directory), e.g. ``renderings``.
        ext: The extension, with or without a leading dot, e.g. ``jpg``.

    Returns:
        The object name the blob was written to.
    """
    name = f'{prefix}/{hashlib.sha256(data).hexdigest()}.{ext.lstrip(".")}'
    blob = bucket.blob(name)
    if not blob.exists():
        blob.upload_from_string(data)
    return name


def read_modify_write(
    bucket: storage.Bucket, name: str, mutate: Callable[[bytes], bytes], *, attempts: int = 5
) -> None:
    """Read ``name``, apply ``mutate``, and write the result iff the object hasn't changed.

    Optimistic concurrency via GCS generation preconditions (ADR 0003): read the object and
    its generation, write back with ``if_generation_match``; a concurrent write makes the read
    stale (a 412), so re-read and retry up to ``attempts`` times. `mutate` must be pure — it is
    re-invoked on each attempt.

    Args:
        bucket: The bucket holding the object.
        name: The object name to modify in place.
        mutate: Maps the current bytes to the bytes to write.
        attempts: How many times to retry a lost race before failing.

    Raises:
        StaleGenerationError: every attempt's write-back lost the race.
        google.cloud.exceptions.NotFound: ``name`` does not exist.
    """
    for _ in range(attempts):
        blob = bucket.blob(name)  # fresh handle each attempt: a retained stale generation would
        data = blob.download_as_bytes()  # pin the download to a superseded generation (404)
        try:
            blob.upload_from_string(mutate(data), if_generation_match=blob.generation)
        except api_exceptions.PreconditionFailed:
            continue  # a concurrent writer won between the read and the write; re-read and retry
        return
    raise StaleGenerationError(f'{name}: {attempts} read-modify-write attempts lost the race')
