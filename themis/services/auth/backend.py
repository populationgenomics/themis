"""The auth service's port: the two sources it answers from (access-control.md, self-hosted-sandbox.md §7).

The token store binds a per-session token's hash to the Analysis it was minted for and that Analysis's
Project id; the ACL says which Projects exist, what each reads, and who may reach them. The adapter is
selected by ``THEMIS_BACKEND``: ``live.LiveBackend`` reads the token store in Cloud SQL and the ACL from
Secret Manager, and ``fixture.FixtureBackend`` holds both in memory.
"""

from __future__ import annotations

import abc
import dataclasses
import hashlib

# A read of the ACL not finished within this is abandoned, and the last good copy kept.
ACL_READ_TIMEOUT_SECONDS = 30


class UnresolvedError(Exception):
    """The session token resolves to no binding — an invalid, revoked, or expired token."""


@dataclasses.dataclass(frozen=True)
class StoredSession:
    """What the token store binds a token to.

    Attributes:
        analysis_id: The Analysis the token was minted for.
        project_id: The id of the Analysis's Project, as stored when the Analysis was created; whether the
            Project still exists is the ACL's to say.
    """

    analysis_id: str
    project_id: str


class AuthBackend(abc.ABC):
    """The token store and the ACL. Async: a live adapter offloads its blocking I/O to a thread."""

    @abc.abstractmethod
    async def resolve(self, session_token: str) -> StoredSession:
        """Return the token's binding, or raise ``UnresolvedError`` if it grants nothing."""
        ...

    @abc.abstractmethod
    async def read_acl(self) -> bytes:
        """The current version of the ACL, unvalidated.

        Raises:
            Exception: Whatever the source raises when the ACL cannot be read; the caller keeps its last good copy.
        """
        ...


def hash_token(token: str) -> str:
    """SHA-256 of the token — the store never holds plaintext (§7)."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()
