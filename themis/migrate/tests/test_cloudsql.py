"""Integration test for the live Cloud SQL ledger against a throwaway Postgres.

``CloudSqlLedger.record``'s atomicity — a migration whose statements fail mid-way commits
nothing, leaving no version row so it re-runs cleanly — is the one piece of real DB logic the
in-memory ledger can't reach. Exercised against a real Postgres via ``testcontainers``, so
Docker-gated.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pg8000.dbapi
import pytest
import testcontainers.postgres

from themis.migrate import cloudsql, migrate


@pytest.fixture
def connection(docker_daemon: None) -> Iterator[pg8000.dbapi.Connection]:
    del docker_daemon  # gate on a reachable Docker daemon (shared fixture)
    with testcontainers.postgres.PostgresContainer('postgres:16-alpine') as postgres:
        conn = pg8000.dbapi.connect(
            user=postgres.username,
            password=postgres.password,
            host=postgres.get_container_host_ip(),
            port=int(postgres.get_exposed_port(5432)),
            database=postgres.dbname,
        )
        try:
            yield conn
        finally:
            conn.close()


# A two-statement migration whose second statement is invalid, and its clean counterpart.
_FAILING_SQL = 'CREATE TABLE probe (id integer);\nnot valid sql;'
_GOOD_SQL = 'CREATE TABLE probe (id integer);'


def test_record_rolls_back_a_failed_migration(connection: pg8000.dbapi.Connection) -> None:
    ledger = cloudsql.CloudSqlLedger(connection)
    assert ledger.applied_versions() == set()

    # The second statement is invalid, so record raises before its version-row INSERT and
    # never commits — neither the version nor the first statement's table may persist.
    with pytest.raises(pg8000.dbapi.DatabaseError):
        ledger.record(migrate.Migration(version=1, name='failing', sql=_FAILING_SQL), _FAILING_SQL)
    connection.rollback()  # clear the aborted transaction so the ledger can be queried

    assert ledger.applied_versions() == set()
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute("SELECT to_regclass('probe')")
        assert cursor.fetchall()[0][0] is None


def test_record_commits_a_successful_migration(connection: pg8000.dbapi.Connection) -> None:
    ledger = cloudsql.CloudSqlLedger(connection)
    ledger.applied_versions()
    ledger.record(migrate.Migration(version=1, name='ok', sql=_GOOD_SQL), _GOOD_SQL)
    assert ledger.applied_versions() == {1}
