"""Cloud SQL access shared across themis packages (verified live at deploy, not offline).

The pg8000 DBAPI surface (`Cursor`, `Connection`), the IAM-authed connect (`iam_connect`) and the
connector a long-lived dialler builds (`lazy_connector`) — used by every Cloud SQL consumer, the
migrate runner, the auth service and the evidence service's crosswalk among them — factored out so
they aren't redefined per package. Importing this module pulls the connector (and, transitively,
pg8000), so any module importing it carries that cost whether or not it dials.

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

    # A property, not a plain attribute: pg8000 exposes it read-only, and a mutable protocol member
    # would not match.
    @property
    def rowcount(self) -> int: ...

    def execute(self, operation: str, args: Sequence[object] = ()) -> object: ...
    def executemany(self, operation: str, param_sets: Sequence[Sequence[object]]) -> object: ...
    def fetchone(self) -> Row | None: ...
    def fetchall(self) -> Sequence[Row]: ...
    def close(self) -> None: ...


class Connection(Protocol):
    """The pg8000 DBAPI connection surface themis uses."""

    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


def lazy_connector() -> connector.Connector:
    """A Cloud SQL connector that refreshes its client certificate when a dial needs one.

    The default strategy refreshes on a background task. A Cloud Run service that allows its CPU to
    idle is throttled between requests, so that task does not run while the instance is idle and the
    certificate it holds can expire before the next dial — which is when the service is least able to
    absorb a failure. The lazy strategy refreshes inside the dial instead, trading a slower first
    connection after an idle period for one that happens at all; the connector's README recommends it
    on serverless runtimes for this reason.

    Every service builds its connector here, and a check holds them to it
    (`themis.common.tests.test_sql`) — leaving it to a judgement per call site is what let the
    evidence service dial on the default for as long as it did. Two kinds of process stay on the
    default deliberately. A migrate run, a backfill and the `tools` commands are shorter-lived than a
    certificate, so no refresh falls due inside one. The Dataflow ingestion worker
    (`themis.litcache.cloudsql`) holds one for the life of its process, but its CPU is never
    throttled, so the background task it relies on runs.
    """
    return connector.Connector(refresh_strategy=connector.RefreshStrategy.LAZY)


def iam_connect(pool: connector.Connector, *, connection_name: str, database: str, db_user: str) -> Connection:
    """Open one IAM-authed pg8000 connection to a Cloud SQL instance.

    Args:
        pool: The connector the connection is dialed through (its lifecycle is the
            caller's).
        connection_name: The `project:region:instance` string the connector dials.
        database: The application database name.
        db_user: The DB role's IAM login (the SA email minus `.gserviceaccount.com`).

    Returns:
        A live pg8000 connection in its default (non-autocommit) mode.
    """
    return pool.connect(
        connection_name,
        'pg8000',
        user=db_user,
        db=database,
        enable_iam_auth=True,
    )
