"""The Cloud SQL backend (verified live at deploy, not offline).

Reads the ``session_context`` table by token hash through the Cloud SQL connector with
IAM authentication (the auth SA is the DB user, no password — mirrors
infra/themis_infra/sql.py). Importing this module pulls the connector and pg8000, so it
is imported only when the ``cloudsql`` backend is selected; the unit tests exercise
``backend.FixtureBackend`` instead.
"""

from __future__ import annotations

import asyncio
import contextlib

from google.cloud.sql import connector

from themis.common import sql
from themis.rpc import auth_pb2
from themis.services.auth import backend as auth_backend


class CloudSqlBackend(auth_backend.SessionBackend):
    """A ``backend.SessionBackend`` over ``session_context`` (IAM auth, one connection per resolve).

    Holds a process-lifetime ``Connector``; each resolve opens and closes a single
    connection. Lookups are by ``hash_token`` of the bearer, never the plaintext —
    matching the store's invariant.
    """

    def __init__(self, *, connection_name: str, database: str, iam_user: str) -> None:
        self._connection_name = connection_name
        self._database = database
        self._iam_user = iam_user
        self._connector = connector.Connector()

    def _connect(self) -> sql.Connection:
        return sql.iam_connect(
            self._connector,
            connection_name=self._connection_name,
            database=self._database,
            iam_user=self._iam_user,
        )

    async def resolve(self, session_token: str) -> auth_pb2.SessionContext:
        # pg8000 is a blocking driver; offload so the query doesn't stall the event loop.
        return await asyncio.get_running_loop().run_in_executor(None, self._resolve_blocking, session_token)

    def _resolve_blocking(self, session_token: str) -> auth_pb2.SessionContext:
        token_hash = auth_backend.hash_token(session_token)
        with contextlib.closing(self._connect()) as conn, contextlib.closing(conn.cursor()) as cursor:
            cursor.execute(
                'SELECT project_id, analysis_id FROM session_context WHERE token_hash = %s',
                (token_hash,),
            )
            row = cursor.fetchone()
        if row is None:
            raise auth_backend.UnresolvedError
        return auth_pb2.SessionContext(project_id=row[0], analysis_id=row[1])

    def close(self) -> None:
        """Close the underlying connector (process shutdown)."""
        self._connector.close()
