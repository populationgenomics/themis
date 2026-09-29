"""The Cloud SQL backend (verified live at deploy, not offline).

Reads the ``session_context`` table by token hash through the Cloud SQL connector with
IAM authentication (the auth SA is the DB user, no password — mirrors
infra/themis_infra/sql.py). Importing this module pulls the connector, pg8000 and
SQLAlchemy, so it is imported only when the ``cloudsql`` backend is selected.

Connections are pooled (``sql_pool.cloud_sql_engine``); answers are not. Every resolve runs the
query, so a deleted row stops resolving on the next call. A resolve's query is a span, a child of the
rpc's; a dial the checkout has to make is the pool's ``cloudsql.connect`` span, a child of the query's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import override

import sqlalchemy
import sqlalchemy.exc
from opentelemetry import trace

from themis.common import sql, sql_pool
from themis.rpc import auth_pb2
from themis.services.auth import backend as auth_backend

_logger = logging.getLogger(__name__)
_TRACER = trace.get_tracer(__name__)

_QUERY = 'SELECT project_id, analysis_id FROM session_context WHERE token_hash = %s'

# Connections open per instance at most. infra/themis_infra/auth.py caps the service at 3 instances,
# so auth holds at most 12 of db-f1-micro's 25 max_connections, which every service shares; prod's
# sizing is decided separately. A resolve holds a connection ~10 ms, so 4 serve ~400 resolves/s.
# Resolves beyond what the pool and the default thread executor take at once queue for a thread.
POOL_SIZE = 4
# Under Cloud Run's 20-minute idle timeout on egress to the internet, the connector's path to the
# instance's public IP. It also bounds how long a connection outlives the IAM token it logged in with,
# which Postgres checks only at login.
POOL_RECYCLE_S = 15 * 60


def engine(*, connection_name: str, database: str, db_user: str) -> sqlalchemy.Engine:
    """The pooled engine `CloudSqlBackend` runs on, for one Cloud SQL database.

    Its connector lives as long as the process: nothing in the service handles shutdown.
    """
    return sql_pool.cloud_sql_engine(
        sql_pool.lazy_connector(),
        connection_name=connection_name,
        database=database,
        db_user=db_user,
        pool_size=POOL_SIZE,
        recycle_s=POOL_RECYCLE_S,
    )


class CloudSqlBackend(auth_backend.SessionBackend):
    """A ``backend.SessionBackend`` over ``session_context``, on a pooled engine.

    Lookups are by ``hash_token`` of the bearer, never the plaintext — matching the
    store's invariant.
    """

    def __init__(self, pooled: sqlalchemy.Engine) -> None:
        self._engine = pooled

    @override
    async def resolve(self, session_token: str) -> auth_pb2.SessionContext:
        # pg8000 is a blocking driver; offload so the query doesn't stall the event loop. `to_thread` rather
        # than `run_in_executor`: it carries the rpc's context, so the spans below are children of its span.
        row = await asyncio.to_thread(self._resolve_blocking, auth_backend.hash_token(session_token))
        if row is None:
            raise auth_backend.UnresolvedError
        return auth_pb2.SessionContext(project_id=row[0], analysis_id=row[1])

    def _resolve_blocking(self, token_hash: str) -> sql.Row | None:
        """Query once, and once more if the first attempt lost an open connection.

        The checkout's ping replaces a connection that died while idle, but not one whose ping
        timed out (pg8000 raises that as a bare ``OSError``, which the ping does not treat as a
        disconnect) or that died between ping and query. Either way the engine has discarded the
        connection, and the query is a read, so it runs again on a fresh one. A failed dial
        (``sql_pool.DialError``) and a statement error propagate at once.
        """
        try:
            return self._query(token_hash)
        except (sqlalchemy.exc.DBAPIError, OSError) as e:
            if isinstance(e, sqlalchemy.exc.DBAPIError) and not e.connection_invalidated:
                raise
            _logger.warning('a pooled connection failed; retrying on a fresh one', exc_info=True)
            return self._query(token_hash)

    def _query(self, token_hash: str) -> sql.Row | None:
        with (
            _TRACER.start_as_current_span(
                'cloudsql.query', attributes={'db.system.name': 'postgresql', 'db.query.text': _QUERY}
            ),
            self._engine.connect() as conn,
        ):
            return conn.exec_driver_sql(_QUERY, (token_hash,)).fetchone()

    def close(self) -> None:
        """Close the pooled connections."""
        self._engine.dispose()
