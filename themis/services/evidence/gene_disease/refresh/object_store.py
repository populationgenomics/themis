"""The refresh job's object store: read the stored ETag, write the reference dumps.

The weekly refresh reads each upstream's previously stored ETag (a ``<object>.etag`` sidecar) to
issue a conditional GET, and writes the fresh dump plus its new ETag back. The GCS-backed store
imports google-cloud-storage lazily at construction, so importing this module — and the in-memory
store the tests inject — needs neither the library nor GCS credentials. Blocking GCS calls offload
to a worker thread.
"""

from __future__ import annotations

import abc
import asyncio
from typing import override


class ReferenceObjectStore(abc.ABC):
    """A byte-keyed object store: an absent object reads as ``None``; a write overwrites."""

    @abc.abstractmethod
    async def read(self, name: str) -> bytes | None:
        """Return the object's bytes, or ``None`` if it does not exist."""

    @abc.abstractmethod
    async def write(self, name: str, data: bytes) -> None:
        """Write ``data`` to ``name``, overwriting any existing object."""


class GcsReferenceStore(ReferenceObjectStore):
    """A :class:`ReferenceObjectStore` over a single GCS bucket.

    google-cloud-storage is imported at construction (not module load) so importing this module,
    and the fake store the tests use, needs neither the library nor GCS credentials. The store owns
    its GCS client; ``close`` releases it (``contextlib.closing`` at the call site).
    """

    def __init__(self, bucket: str) -> None:
        from google.cloud import storage as gcs  # noqa: PLC0415 — deferred so the tests skip google-cloud-storage

        self._client = gcs.Client()
        self._bucket = self._client.bucket(bucket)

    def close(self) -> None:
        """Release the client's pooled HTTP connections."""
        self._client.close()

    @override
    async def read(self, name: str) -> bytes | None:
        return await asyncio.to_thread(self._read_blocking, name)

    def _read_blocking(self, name: str) -> bytes | None:
        from google.api_core import exceptions as api_exceptions  # noqa: PLC0415 — deferred with the client library

        try:
            return self._bucket.blob(name).download_as_bytes()
        except api_exceptions.NotFound:
            return None

    @override
    async def write(self, name: str, data: bytes) -> None:
        await asyncio.to_thread(self._bucket.blob(name).upload_from_string, data)
