"""The auth service backend: resolve a per-session token to its binding (self-hosted-sandbox.md §7).

The auth service is the sole reader of the token store. A caller (the store) forwards a
session token; the backend hashes it and returns the bound Project + Analysis, or raises
``UnresolvedError`` if the token grants nothing. The adapter is pluggable (selected by
``THEMIS_BACKEND``): the in-memory ``FixtureBackend`` runs offline (tests, a first deploy),
and ``cloudsql.CloudSqlBackend`` reads the ``session_context`` table over the Cloud SQL
connector with IAM auth (the deployed path).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Protocol

from themis.rpc import auth_pb2


class UnresolvedError(Exception):
    """The session token resolves to no binding — an invalid, revoked, or expired token."""


class SessionBackend(Protocol):
    async def resolve(self, session_token: str) -> auth_pb2.SessionContext:
        """Return the token's binding, or raise ``UnresolvedError`` if it grants nothing.

        Async: the servicer runs on ``grpc.aio``, so a real adapter (Cloud SQL) offloads
        its blocking driver rather than stalling the single event loop.
        """
        ...


def hash_token(token: str) -> str:
    """SHA-256 of the token — the store never holds plaintext (§7)."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


class FixtureBackend:
    """In-memory backend keyed by token hash, for offline use and tests.

    The invariant matches the real store: lookups are by ``hash_token`` of the bearer,
    never the plaintext.
    """

    def __init__(self, bindings_by_token_hash: Mapping[str, auth_pb2.SessionContext]) -> None:
        self._by_hash = dict(bindings_by_token_hash)

    async def resolve(self, session_token: str) -> auth_pb2.SessionContext:
        try:
            return self._by_hash[hash_token(session_token)]
        except KeyError:
            raise UnresolvedError from None
