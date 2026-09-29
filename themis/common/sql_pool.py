"""A pool of warm Cloud SQL connections for a service that queries per request (verified live at deploy).

Dialing an IAM-authed connection costs a TLS handshake and a login, a quarter of a second; a query on
a warm connection costs a few milliseconds. `cloud_sql_engine` keeps connections open in a SQLAlchemy
pool tuned for Cloud Run, and dials through `sql.iam_connect`. Opening a connection is not bounded in
time: the connector dials without a timeout, and pg8000's applies only to sockets it opens itself.

Kept apart from `sql` so that only the services that pool pull in SQLAlchemy (the `sql_pool`
dependency group).
"""

from __future__ import annotations

import contextlib
import logging
import socket
from collections.abc import Callable

import pg8000.dbapi
import sqlalchemy
import sqlalchemy.engine
import sqlalchemy.event
import sqlalchemy.pool
from google.cloud.sql import connector

from themis.common import sql

_logger = logging.getLogger(__name__)

# `pooled_engine`'s settings; the values in Cloud SQL's connection guide
# (https://docs.cloud.google.com/sql/docs/postgres/manage-connections) unless stated.
#
# How long a checkout waits for a free connection before raising.
POOL_TIMEOUT_S = 30
# The server cancels a statement running longer than this, lock waits included, and answers with an
# error on a connection still in step with the client. It sits under `IO_TIMEOUT_S`, so a slow
# statement is cancelled by the server rather than abandoned by the client, which would leave its
# backend holding a connection slot until the statement ended.
STATEMENT_TIMEOUT_S = 5
# How long one read or write on a pooled connection's socket may block. Past `STATEMENT_TIMEOUT_S`,
# a silent socket belongs to a connection the network dropped without a reset, which would otherwise
# block its thread for the kernel's ~15-minute retransmission limit.
IO_TIMEOUT_S = 10


class DialError(Exception):
    """Opening a new connection failed; the database is unreachable or refused the login."""


def lazy_connector() -> connector.Connector:
    """A Cloud SQL connector that refreshes its certificate when a dial needs one.

    Cloud Run throttles the CPU between requests, which stalls the connector's default background
    refresh; the connector's README recommends the lazy strategy there.
    """
    return connector.Connector(refresh_strategy=connector.RefreshStrategy.LAZY)


def cloud_sql_engine(
    dialer: connector.Connector,
    *,
    connection_name: str,
    database: str,
    db_user: str,
    pool_size: int,
    recycle_s: float,
) -> sqlalchemy.Engine:
    """A `pooled_engine` over IAM-authed connections to one Cloud SQL database.

    Args:
        dialer: The connector connections are dialed through, normally a `lazy_connector` (its
            lifecycle is the caller's).
        connection_name: The `project:region:instance` string the connector dials.
        database: The application database name.
        db_user: The DB role's IAM login (the SA email minus `.gserviceaccount.com`).
        pool_size: As for `pooled_engine`.
        recycle_s: As for `pooled_engine`.

    Returns:
        An engine that dials nothing until its first checkout.
    """

    def dial() -> sql.Connection:
        return sql.iam_connect(dialer, connection_name=connection_name, database=database, db_user=db_user)

    return pooled_engine(dial, pool_size=pool_size, recycle_s=recycle_s)


def pooled_engine(
    dial: Callable[[], sql.Connection],
    *,
    pool_size: int,
    recycle_s: float,
    statement_timeout_s: float = STATEMENT_TIMEOUT_S,
    io_timeout_s: float = IO_TIMEOUT_S,
) -> sqlalchemy.Engine:
    """A SQLAlchemy engine pooling pg8000 connections opened by `dial`.

    At most `pool_size` connections are open; a checkout beyond that waits up to `POOL_TIMEOUT_S`.
    Each checkout of a connection that has served before pings it first, and replaces it if the ping
    finds it disconnected. Connections run in autocommit, which saves each use a BEGIN and a ROLLBACK
    round trip. A connection whose socket failed mid-statement is discarded, never returned to the
    pool. Statement parameters are left out of error messages.

    Args:
        dial: Opens one pg8000 connection over a TCP socket.
        pool_size: The most connections open at once. The caller sizes it against its concurrency and
            the instance's `max_connections`, which every service and instance shares.
        recycle_s: Age past which a connection is closed at checkout and replaced. The caller keeps it
            under Cloud Run's idle timeout for its egress path, so no checked-out connection has
            idled past it: 10 minutes to a VPC, 20 to the internet
            (https://docs.cloud.google.com/run/docs/container-contract).
        statement_timeout_s: The server-side limit on one statement.
        io_timeout_s: How long one socket read or write may block before the statement fails.

    Returns:
        An engine that dials nothing until its first checkout.

    Raises:
        DialError: From a checkout that had to open a connection and could not, with the cause
            chained.
    """

    def creator() -> sql.Connection:
        try:
            conn = dial()
        except Exception as e:
            raise DialError('opening a database connection failed') from e
        try:
            _socket_of(conn).settimeout(io_timeout_s)
            with contextlib.closing(conn.cursor()) as cursor:
                cursor.execute(f'SET statement_timeout = {int(statement_timeout_s * 1000)}')
            conn.commit()
        except BaseException:
            _close_quietly(conn)
            raise
        return conn

    engine = sqlalchemy.create_engine(
        'postgresql+pg8000://',
        creator=creator,
        poolclass=sqlalchemy.pool.QueuePool,
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout=POOL_TIMEOUT_S,
        pool_recycle=recycle_s,
        pool_pre_ping=True,
        isolation_level='AUTOCOMMIT',
        hide_parameters=True,
    )
    sqlalchemy.event.listen(engine, 'handle_error', _socket_failure_is_a_disconnect)
    return engine


def _socket_of(conn: sql.Connection) -> socket.socket:
    # pg8000 applies its `timeout` only to a socket it opens itself; the connector opens the socket and
    # hands it over, with no hook to set one, so the timeout goes on pg8000's private handle to it.
    sock = getattr(conn, '_usock', None)
    if not isinstance(sock, socket.socket):
        raise TypeError(f'expected a pg8000 connection holding its socket as _usock, got {type(conn).__name__}')
    return sock


def _close_quietly(conn: sql.Connection) -> None:
    try:
        conn.close()
    except (pg8000.dbapi.Error, OSError):
        _logger.debug('closing a connection that failed its setup also failed', exc_info=True)


def _socket_failure_is_a_disconnect(context: sqlalchemy.engine.ExceptionContext) -> None:
    # pg8000 wraps a failed write, but raises a failed read (a timeout, a reset) as a bare OSError,
    # which SQLAlchemy's disconnect check skips, so the connection would go back to the pool
    # mid-protocol. One silent socket says nothing about the others, so the pool keeps them.
    if isinstance(context.original_exception, OSError):
        context.is_disconnect = True
        context.invalidate_pool_on_disconnect = False
