"""The live Cloud SQL ledger and apply entry point (verified at deploy, not offline).

`CloudSqlLedger` tracks applied versions in `schema_migrations` and applies each
migration's statements plus its version row in one transaction. `apply_migrations`
holds a single IAM-authed connection for the whole run and takes a session-level
advisory lock, so two concurrent deploys serialize rather than racing to apply the
same version. Importing this module pulls the connector and pg8000, so it is
imported only when migrating — never by the hermetic unit tests, which exercise
`migrate.InMemoryLedger`.
"""

from __future__ import annotations

import contextlib
import pathlib
from collections.abc import Mapping, Sequence

from google.cloud.sql import connector

from themis.common import sql
from themis.migrate import config, migrate

# Arbitrary application-wide key; every run takes this one session-level advisory
# lock, so concurrent runs (overlapping deploys) serialize. 0x7468656d6973 = 'themis'.
_MIGRATION_LOCK_KEY = 0x7468656D6973


class CloudSqlLedger(migrate.Ledger):
    """A `migrate.Ledger` over Cloud SQL, tracked in `schema_migrations`.

    Bound to one live connection so the caller's advisory lock spans every
    `record`. Each `record` runs the migration's statements and inserts its version
    row in one transaction, so a failed migration leaves no version and re-runs
    cleanly.
    """

    _CREATE_LEDGER = (
        'CREATE TABLE IF NOT EXISTS schema_migrations ('
        'version integer PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())'
    )

    def __init__(self, conn: sql.Connection) -> None:
        self._conn = conn

    def applied_versions(self) -> set[int]:
        with contextlib.closing(self._conn.cursor()) as cursor:
            cursor.execute(self._CREATE_LEDGER)
            self._conn.commit()
            cursor.execute('SELECT version FROM schema_migrations')
            rows = cursor.fetchall()
        return {row[0] for row in rows}

    def record(self, migration: migrate.Migration, sql: str) -> None:
        with contextlib.closing(self._conn.cursor()) as cursor:
            for statement in migrate.split_statements(sql):
                cursor.execute(statement)
            cursor.execute(
                'INSERT INTO schema_migrations (version, name) VALUES (%s, %s)',
                (migration.version, migration.name),
            )
            self._conn.commit()


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
    """
    migrations = migrate.discover(migrations_dir)
    with (
        contextlib.closing(connector.Connector()) as pool,
        contextlib.closing(
            sql.iam_connect(
                pool,
                connection_name=sql_config.connection_name,
                database=sql_config.database,
                iam_user=sql_config.iam_user,
            )
        ) as conn,
    ):
        with contextlib.closing(conn.cursor()) as cursor:
            cursor.execute('SELECT pg_advisory_lock(%s)', (_MIGRATION_LOCK_KEY,))
            conn.commit()
        return migrate.run(migrations, CloudSqlLedger(conn), substitutions=substitutions)
