"""The live backend: the token store in Cloud SQL and the ACL in Secret Manager.

Importing this module pulls the Cloud SQL connector and the Secret Manager client, so it is imported only when the live
backend is selected.
"""

from __future__ import annotations

from typing import override

from themis.services.auth import backend as auth_backend
from themis.services.auth import cloudsql, secret


class LiveBackend(auth_backend.AuthBackend):
    """``backend.AuthBackend`` over the deployed sources, one module per source."""

    def __init__(self, sessions: cloudsql.SessionStore, acl: secret.AclSecret) -> None:
        self._sessions = sessions
        self._acl = acl

    @override
    async def resolve(self, session_token: str) -> auth_backend.StoredSession:
        return await self._sessions.resolve(session_token)

    @override
    async def read_acl(self) -> bytes:
        return await self._acl.read()
