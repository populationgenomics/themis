"""The Google Cloud Storage backend (verified live at deploy, not offline).

Two buckets: immutable working-document versions and the overwrite-on-put workspace archive
(self-hosted-sandbox.md §9). Importing this pulls google-cloud-storage, so it is imported only
when the ``gcs`` storage backend is selected; tests exercise ``storage.FixtureStorage`` instead.
The working-document write is create-only (``if_generation_match=0``) over the store-assigned
successor of the latest key, so racing writers cannot both win nor reorder history.

google-cloud-storage is blocking; each ``Storage`` method offloads its work to a thread so the
``grpc.aio`` event loop is never stalled.
"""

from __future__ import annotations

import asyncio

from google.api_core import exceptions
from google.cloud import storage as gcs

from themis.services.store import storage as storage_mod

# A create-only write only loses the race to another live writer, each advancing the
# sequence; a bound converts a pathological spin into a loud failure.
_MAX_WRITE_ATTEMPTS = 8


class GcsStorage(storage_mod.Storage):
    """``storage.Storage`` over two GCS buckets (the store is the version authority)."""

    def __init__(self, *, working_document_bucket: str, workspace_bucket: str) -> None:
        self._client = gcs.Client()
        self._working_documents = self._client.bucket(working_document_bucket)
        self._workspaces = self._client.bucket(workspace_bucket)

    async def put_working_document(self, analysis_id: str, markdown: str) -> int:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._put_working_document_blocking, analysis_id, markdown
        )

    async def get_working_document(self, analysis_id: str) -> storage_mod.WorkingDocument | None:
        return await asyncio.get_running_loop().run_in_executor(None, self._get_working_document_blocking, analysis_id)

    async def put_workspace(self, analysis_id: str, archive: bytes) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._put_workspace_blocking, analysis_id, archive)

    async def get_workspace(self, analysis_id: str) -> bytes | None:
        return await asyncio.get_running_loop().run_in_executor(None, self._get_workspace_blocking, analysis_id)

    def _put_working_document_blocking(self, analysis_id: str, markdown: str) -> int:
        prefix = f'{analysis_id}/versions/'
        for _ in range(_MAX_WRITE_ATTEMPTS):
            existing = [blob.name for blob in self._client.list_blobs(self._working_documents, prefix=prefix)]
            version = storage_mod.next_version(existing)
            blob = self._working_documents.blob(storage_mod.version_key(analysis_id, version))
            try:
                blob.upload_from_string(markdown, content_type='text/markdown', if_generation_match=0)
            except exceptions.PreconditionFailed:
                continue
            return version
        raise RuntimeError(
            f'working-document version write for {analysis_id!r} lost the race {_MAX_WRITE_ATTEMPTS} times'
        )

    def _get_working_document_blocking(self, analysis_id: str) -> storage_mod.WorkingDocument | None:
        prefix = f'{analysis_id}/versions/'
        keys = sorted(blob.name for blob in self._client.list_blobs(self._working_documents, prefix=prefix))
        if not keys:
            return None
        latest = keys[-1]  # zero-padded ⇒ the last key is the highest version
        version = int(latest.rsplit('/', 1)[-1])
        return storage_mod.WorkingDocument(
            version=version, markdown=self._working_documents.blob(latest).download_as_text()
        )

    def _put_workspace_blocking(self, analysis_id: str, archive: bytes) -> None:
        self._workspaces.blob(analysis_id).upload_from_string(archive, content_type='application/octet-stream')

    def _get_workspace_blocking(self, analysis_id: str) -> bytes | None:
        blob = self._workspaces.blob(analysis_id)
        if not blob.exists():
            return None
        return blob.download_as_bytes()
