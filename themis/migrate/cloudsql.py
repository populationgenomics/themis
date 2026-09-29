"""The live Cloud SQL ledger and the apply and check entry points.

`CloudSqlLedger` tracks applied migrations in `schema_migrations` and applies each
migration's statements plus its ledger row in one transaction. `apply_migrations`
holds a single IAM-authed connection for the whole run and takes a session-level
advisory lock, so two concurrent deploys serialize rather than racing to apply the
same version. `check_migrations` reads the ledger in a read-only transaction and runs the
same check without writing anything. Importing this module pulls the connector and
pg8000, so outside `__main__` only the Docker-gated tests import it; the hermetic
tests exercise `migrate.InMemoryLedger` instead.
"""

from __future__ import annotations

import contextlib
import pathlib
from collections.abc import Iterator, Mapping, Sequence
from typing import override

from google.cloud.sql import connector

from themis.common import sql
from themis.migrate import config, migrate

# Arbitrary application-wide key; every run takes this one session-level advisory
# lock, so concurrent runs (overlapping deploys) serialize. 0x7468656d6973 = 'themis'.
_MIGRATION_LOCK_KEY = 0x7468656D6973

_LEDGER_TABLE = 'schema_migrations'


class LedgerUnreadableError(Exception):
    """The check could not connect to the database or read its ledger, so it found nothing either way."""


def _ledger_columns(cursor: sql.Cursor) -> set[str]:
    """The ledger table's column names; empty where the table does not exist."""
    cursor.execute(
        'SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) AND attnum > 0 AND NOT attisdropped',
        (_LEDGER_TABLE,),
    )
    return {row[0] for row in cursor.fetchall()}


def read_ledger(conn: sql.Connection) -> dict[int, migrate.AppliedMigration]:
    """Read the ledger's rows without creating or altering anything.

    A database with no ledger table has applied nothing, so it reads as empty. A ledger
    without the `sha256` column predates content hashes, so every row reads with no hash
    and is checked by name.

    Args:
        conn: A live connection; this issues only `SELECT`s on it.

    Returns:
        Each applied version, mapped to what the ledger recorded for it.
    """
    with contextlib.closing(conn.cursor()) as cursor:
        columns = _ledger_columns(cursor)
        if not columns:
            return {}
        sha256_column = 'sha256' if 'sha256' in columns else 'NULL'
        cursor.execute(f'SELECT version, name, {sha256_column} FROM {_LEDGER_TABLE}')  # noqa: S608 - fixed identifiers
        rows = cursor.fetchall()
    return {row[0]: migrate.AppliedMigration(name=row[1], sha256=row[2]) for row in rows}


class CloudSqlLedger(migrate.Ledger):
    """A `migrate.Ledger` over Cloud SQL, tracked in `schema_migrations`.

    Bound to one live connection so the caller's advisory lock spans every
    `record`. Each `record` runs the migration's statements and inserts its ledger
    row in one transaction, so a failed migration leaves no row and re-runs
    cleanly. The first read creates the table where there is none, or adds the
    `sha256` column to a ledger that predates it, and issues no DDL once both exist.
    """

    # `sha256` is nullable: rows applied before the ledger kept hashes have none, and are checked by name.
    _CREATE_LEDGER = (
        f'CREATE TABLE {_LEDGER_TABLE} ('
        'version integer PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now(), sha256 text)'
    )
    _ADD_SHA256 = f'ALTER TABLE {_LEDGER_TABLE} ADD COLUMN sha256 text'

    def __init__(self, conn: sql.Connection) -> None:
        self._conn = conn

    @override
    def applied_migrations(self) -> dict[int, migrate.AppliedMigration]:
        # DDL only where something is missing: ALTER TABLE checks table ownership before it
        # evaluates IF NOT EXISTS, and takes an ACCESS EXCLUSIVE lock.
        with contextlib.closing(self._conn.cursor()) as cursor:
            columns = _ledger_columns(cursor)
            if not columns:
                cursor.execute(self._CREATE_LEDGER)
            elif 'sha256' not in columns:
                cursor.execute(self._ADD_SHA256)
            self._conn.commit()
        return read_ledger(self._conn)

    @override
    def record(self, migration: migrate.Migration, sql: str) -> None:
        with contextlib.closing(self._conn.cursor()) as cursor:
            for statement in migrate.split_statements(sql):
                cursor.execute(statement)
            cursor.execute(
                f'INSERT INTO {_LEDGER_TABLE} (version, name, sha256) VALUES (%s, %s, %s)',  # noqa: S608 - fixed identifiers
                (migration.version, migration.name, migration.sha256),
            )
            self._conn.commit()


@contextlib.contextmanager
def connect(sql_config: config.SqlConfig) -> Iterator[sql.Connection]:
    """Open one IAM-authed connection as the migrator, closed on exit.

    Args:
        sql_config: The Cloud SQL connection inputs.

    Yields:
        A live pg8000 connection in its default (non-autocommit) mode.
    """
    with (
        contextlib.closing(connector.Connector()) as pool,
        contextlib.closing(
            sql.iam_connect(
                pool,
                connection_name=sql_config.connection_name,
                database=sql_config.database,
                db_user=sql_config.db_user,
            )
        ) as conn,
    ):
        yield conn


def apply_migrations(
    sql_config: config.SqlConfig,
    migrations_dir: pathlib.Path,
    substitutions: Mapping[str, str],
) -> Sequence[int]:
    """Apply pending migrations against Cloud SQL under a session advisory lock.

    Holds one IAM-authed connection for the whole run; the session-level advisory
    lock (released when the connection closes) serializes concurrent runs.

    Args:
        sql_config: The Cloud SQL connection inputs.
        migrations_dir: The folder holding the `NNNN_name.sql` files.
        substitutions: The `${VAR}` values (the IAM DB-user logins for the GRANTs).

    Returns:
        The versions applied by this call, ascending.

    Raises:
        migrate.LedgerCheckError: Before anything is applied, if the ledger
            disagrees with the committed migrations.
    """
    migrations = migrate.discover(migrations_dir)
    with connect(sql_config) as conn:
        with contextlib.closing(conn.cursor()) as cursor:
            cursor.execute('SELECT pg_advisory_lock(%s)', (_MIGRATION_LOCK_KEY,))
            conn.commit()
        return migrate.run(migrations, CloudSqlLedger(conn), substitutions=substitutions)


def read_ledger_read_only(conn: sql.Connection) -> dict[int, migrate.AppliedMigration]:
    """Read the ledger behind `conn` inside a read-only transaction.

    The transaction is set read-only before the ledger is read, so Postgres itself
    rejects any write, and it is rolled back on the way out.

    Args:
        conn: A live connection with no transaction open.

    Returns:
        Each applied version, mapped to what the ledger recorded for it (`read_ledger`).
    """
    try:
        with contextlib.closing(conn.cursor()) as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
        return read_ledger(conn)
    finally:
        conn.rollback()


def check_migrations(sql_config: config.SqlConfig, migrations_dir: pathlib.Path) -> list[migrate.Migration]:
    """Check the Cloud SQL ledger against the committed migrations, writing nothing.

    Takes no advisory lock: the ledger can change before an apply, whose own check
    under the lock is the authoritative one.

    Args:
        sql_config: The Cloud SQL connection inputs.
        migrations_dir: The folder holding the `NNNN_name.sql` files.

    Returns:
        The migrations an apply would run now, ascending by version.

    Raises:
        migrate.LedgerCheckError: If the ledger disagrees with the committed migrations.
        LedgerUnreadableError: If connecting to the database or reading its ledger failed.
    """
    migrations = migrate.discover(migrations_dir)
    try:
        with connect(sql_config) as conn:
            applied = read_ledger_read_only(conn)
    # Whatever layer failed (credentials, the Admin API, the network, the login, a
    # privilege), the ledger went unread; the plan's findings are raised outside this.
    except Exception as error:
        raise LedgerUnreadableError(f'could not read the ledger: {type(error).__name__}: {error}') from error
    return migrate.plan(migrations, applied)
