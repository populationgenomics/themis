"""The ACL as a Secret Manager secret, the live backend's ACL source.

The stack creates the secret in the deployment's own project and grants the auth service's account read on it; the
site's writer adds each version. This module only reads the latest one.
"""

from __future__ import annotations

import asyncio
import re

from google.api_core import retry as api_retry
from google.cloud import secretmanager

from themis.services.auth import backend as auth_backend

_SECRET_NAME = re.compile(r'projects/[^/]+/secrets/[^/]+')


class AclSecret:
    """The latest version of the secret a ``projects/<project>/secrets/<secret>`` name names."""

    def __init__(self, name: str, client: secretmanager.SecretManagerServiceClient) -> None:
        """Name the secret; nothing is read until :meth:`read`.

        Raises:
            ValueError: ``name`` is not ``projects/<project>/secrets/<secret>``.
        """
        if not _SECRET_NAME.fullmatch(name):
            raise ValueError(f'{name!r} is not a projects/<project>/secrets/<secret> name')
        self._latest = f'{name}/versions/latest'
        self._client = client

    async def read(self) -> bytes:
        """The latest version's payload.

        Raises:
            Exception: The Secret Manager client's own error when the version cannot be read: the secret has no
                enabled version, or the caller holds no ``secretAccessor`` on it.
        """
        return await asyncio.to_thread(self._read_blocking)

    def _read_blocking(self) -> bytes:
        response = self._client.access_secret_version(
            name=self._latest,
            timeout=auth_backend.ACL_READ_TIMEOUT_SECONDS,
            retry=api_retry.Retry(timeout=auth_backend.ACL_READ_TIMEOUT_SECONDS),
        )
        return response.payload.data
