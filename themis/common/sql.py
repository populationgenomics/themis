"""Cloud SQL access shared across themis packages (verified live at deploy, not offline).

The pg8000 DBAPI surface (`Cursor`, `Connection`) and the IAM-authed connect
(`iam_connect`) used by every Cloud SQL consumer — the migrate runner and the auth
service — factored out so they aren't redefined per package. Importing this module pulls
the connector (and, transitively, pg8000), so it is imported only by the live paths, never
by the hermetic unit tests (which run against in-memory/fixture backends).

The connection is IAM-authed: the calling service account is the DB user, no stored
password.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from google.cloud.sql import connector

# pg8000 returns heterogeneous positional row tuples (str, datetime, …); typing the
# element payload buys no safety, so it stays dynamic. (`Row` aliases `Any`; ANN401
# targets a literal `Any` in an annotation, not an alias.)
Row = Any


class Cursor(Protocol):
    """The pg8000 DBAPI cursor surface themis uses."""

    def execute(self, operation: str, args: Sequence[object] = ()) -> object: ...
    def fetchone(self) -> Row | None: ...
    def fetchall(self) -> Sequence[Row]: ...
    def close(self) -> None: ...


class Connection(Protocol):
    """The pg8000 DBAPI connection surface themis uses."""

    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def close(self) -> None: ...


def iam_connect(pool: connector.Connector, *, connection_name: str, database: str, iam_user: str) -> Connection:
    """Open one IAM-authed pg8000 connection to a Cloud SQL instance.

    Args:
        pool: The connector the connection is dialed through (its lifecycle is the
            caller's).
        connection_name: The `project:region:instance` string the connector dials.
        database: The application database name.
        iam_user: The DB role's IAM login (the SA email minus `.gserviceaccount.com`).

    Returns:
        A live pg8000 connection in its default (non-autocommit) mode.
    """
    return pool.connect(
        connection_name,
        'pg8000',
        user=iam_user,
        db=database,
        enable_iam_auth=True,
    )
