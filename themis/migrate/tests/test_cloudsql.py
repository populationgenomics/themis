"""Integration tests for the live Cloud SQL ledger against a throwaway Postgres.

What the in-memory ledger can't reach: ``CloudSqlLedger.record``'s atomicity (a migration whose
statements fail mid-way commits nothing, leaving no version row so it re-runs cleanly), the
``sha256`` column added to a ledger that predates it, a migrator that does not own the ledger
applying over it, the read-only check writing nothing to a database whatever shape its ledger is
in, and the check's exit status for a finding and for a ledger it could not read. Exercised
against a real Postgres via ``testcontainers``, so Docker-gated.
"""

from __future__ import annotations

import contextlib
import hashlib
import pathlib
from collections.abc import Callable, Iterator

import pg8000.dbapi
import pytest
import testcontainers.postgres

from themis.common import sql
from themis.migrate import __main__ as entry
from themis.migrate import cloudsql, config, migrate

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / 'migrations'

# The ledger as it stood before it kept content hashes.
_PRE_HASH_LEDGER = (
    'CREATE TABLE schema_migrations ('
    'version integer PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())'
)


@pytest.fixture
def postgres(docker_daemon: None) -> Iterator[testcontainers.postgres.PostgresContainer]:
    del docker_daemon  # gate on a reachable Docker daemon (shared fixture)
    with testcontainers.postgres.PostgresContainer('postgres:16-alpine') as container:
        yield container


def _dial(postgres: testcontainers.postgres.PostgresContainer, user: str, password: str) -> pg8000.dbapi.Connection:
    return pg8000.dbapi.connect(
        user=user,
        password=password,
        host=postgres.get_container_host_ip(),
        port=int(postgres.get_exposed_port(5432)),
        database=postgres.dbname,
    )


@pytest.fixture
def connection(postgres: testcontainers.postgres.PostgresContainer) -> Iterator[pg8000.dbapi.Connection]:
    """A superuser connection to the throwaway Postgres."""
    with contextlib.closing(_dial(postgres, postgres.username, postgres.password)) as conn:
        yield conn


# A two-statement migration whose second statement is invalid, and its clean counterpart.
_FAILING_SQL = 'CREATE TABLE probe (id integer);\nnot valid sql;'
_GOOD_SQL = 'CREATE TABLE probe (id integer);'


def _migration(version: int, name: str, sql_text: str) -> migrate.Migration:
    sha256 = hashlib.sha256(sql_text.encode('utf-8')).hexdigest()
    return migrate.Migration(version=version, name=name, sql=sql_text, sha256=sha256)


def _execute(connection: pg8000.dbapi.Connection, *statements: str) -> None:
    with contextlib.closing(connection.cursor()) as cursor:
        for statement in statements:
            cursor.execute(statement)
    connection.commit()


def _ledger_columns(connection: pg8000.dbapi.Connection) -> set[str]:
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass('schema_migrations')"
            ' AND attnum > 0 AND NOT attisdropped'
        )
        return {row[0] for row in cursor.fetchall()}


def test_record_rolls_back_a_failed_migration(connection: pg8000.dbapi.Connection) -> None:
    ledger = cloudsql.CloudSqlLedger(connection)
    assert ledger.applied_migrations() == {}

    # The second statement is invalid, so record raises before its version-row INSERT and
    # never commits — neither the version nor the first statement's table may persist.
    with pytest.raises(pg8000.dbapi.DatabaseError):
        ledger.record(_migration(1, 'failing', _FAILING_SQL), _FAILING_SQL)
    connection.rollback()  # clear the aborted transaction so the ledger can be queried

    assert ledger.applied_migrations() == {}
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute("SELECT to_regclass('probe')")
        assert cursor.fetchall()[0][0] is None


def test_record_commits_a_successful_migration_with_its_hash(connection: pg8000.dbapi.Connection) -> None:
    ledger = cloudsql.CloudSqlLedger(connection)
    ledger.applied_migrations()
    migration = _migration(1, 'ok', _GOOD_SQL)
    ledger.record(migration, _GOOD_SQL)
    assert ledger.applied_migrations() == {1: migrate.AppliedMigration(name='ok', sha256=migration.sha256)}


def test_a_pre_hash_ledger_gains_the_column_and_keeps_its_rows_unhashed(connection: pg8000.dbapi.Connection) -> None:
    _execute(connection, _PRE_HASH_LEDGER, "INSERT INTO schema_migrations (version, name) VALUES (1, 'base')")
    ledger = cloudsql.CloudSqlLedger(connection)
    assert ledger.applied_migrations() == {1: migrate.AppliedMigration(name='base', sha256=None)}
    assert 'sha256' in _ledger_columns(connection)

    migration = _migration(2, 'ok', _GOOD_SQL)
    assert migrate.run([_migration(1, 'base', 'SELECT 1;'), migration], ledger) == [2]
    assert cloudsql.read_ledger(connection)[2] == migrate.AppliedMigration(name='ok', sha256=migration.sha256)


def test_a_migrator_that_does_not_own_the_ledger_applies_over_it(
    postgres: testcontainers.postgres.PostgresContainer, connection: pg8000.dbapi.Connection
) -> None:
    # A current ledger owned by another role. The migrator reaches it through a granted role, as
    # Cloud SQL's cloudsqlsuperuser does, without owning it, so any ALTER TABLE on it would fail.
    cloudsql.CloudSqlLedger(connection).applied_migrations()
    _execute(
        connection,
        'CREATE ROLE ledger_owner',
        'CREATE ROLE ledger_writer',
        "CREATE ROLE migrator LOGIN PASSWORD 'migrator'",
        'GRANT ledger_writer TO migrator',
        'GRANT CREATE ON SCHEMA public TO ledger_writer',
        'ALTER TABLE schema_migrations OWNER TO ledger_owner',
        'GRANT SELECT, INSERT ON schema_migrations TO ledger_writer',
    )
    migration = _migration(1, 'ok', _GOOD_SQL)
    with contextlib.closing(_dial(postgres, 'migrator', 'migrator')) as migrator:
        assert migrate.run([migration], cloudsql.CloudSqlLedger(migrator)) == [1]
    assert cloudsql.read_ledger(connection) == {1: migrate.AppliedMigration(name='ok', sha256=migration.sha256)}


def test_the_read_only_read_creates_no_ledger_where_there_is_none(connection: pg8000.dbapi.Connection) -> None:
    assert cloudsql.read_ledger_read_only(connection) == {}
    assert _ledger_columns(connection) == set()


def test_the_read_only_read_adds_no_column_to_a_pre_hash_ledger(connection: pg8000.dbapi.Connection) -> None:
    _execute(connection, _PRE_HASH_LEDGER, "INSERT INTO schema_migrations (version, name) VALUES (1, 'base')")
    assert cloudsql.read_ledger_read_only(connection) == {1: migrate.AppliedMigration(name='base', sha256=None)}
    assert _ledger_columns(connection) == {'version', 'name', 'applied_at'}


def test_postgres_rejects_a_write_inside_the_read_only_read(
    connection: pg8000.dbapi.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def read_ledger_and_write(conn: sql.Connection) -> dict[int, migrate.AppliedMigration]:
        with contextlib.closing(conn.cursor()) as cursor:
            cursor.execute(_GOOD_SQL)
        return {}

    monkeypatch.setattr(cloudsql, 'read_ledger', read_ledger_and_write)
    with pytest.raises(pg8000.dbapi.DatabaseError, match='read-only transaction'):
        cloudsql.read_ledger_read_only(connection)
    with contextlib.closing(connection.cursor()) as cursor:
        cursor.execute("SELECT to_regclass('probe')")
        assert cursor.fetchall()[0][0] is None


def test_the_read_only_read_carries_the_recorded_hashes(connection: pg8000.dbapi.Connection) -> None:
    migrate.run([_migration(1, 'ok', _GOOD_SQL)], cloudsql.CloudSqlLedger(connection))
    applied = cloudsql.read_ledger_read_only(connection)
    with pytest.raises(migrate.LedgerMismatchError, match=r'0001_ok\.sql: content differs'):
        migrate.plan([_migration(1, 'ok', 'CREATE TABLE probe (id bigint);')], applied)


def _dial_instead_of_cloud_sql(monkeypatch: pytest.MonkeyPatch, dial: Callable[[], sql.Connection]) -> None:
    """Point `python -m themis.migrate` at `dial` instead of Cloud SQL."""

    @contextlib.contextmanager
    def connect(sql_config: config.SqlConfig) -> Iterator[sql.Connection]:
        del sql_config  # the stand-in dials the container whatever the config names
        with contextlib.closing(dial()) as conn:
            yield conn

    monkeypatch.setattr(cloudsql, 'connect', connect)
    monkeypatch.setenv('THEMIS_SQL_CONNECTION_NAME', 'project:region:instance')
    monkeypatch.setenv('THEMIS_SQL_DATABASE', 'themis')
    monkeypatch.setenv('THEMIS_DB_USER', 'themis-deploy@project.iam')


@pytest.fixture
def entry_point_connection(
    postgres: testcontainers.postgres.PostgresContainer,
    connection: pg8000.dbapi.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> pg8000.dbapi.Connection:
    """`python -m themis.migrate` dialling the throwaway Postgres as its superuser, and a connection to inspect it."""
    _dial_instead_of_cloud_sql(monkeypatch, lambda: _dial(postgres, postgres.username, postgres.password))
    return connection


def test_the_check_entry_point_exits_with_the_finding_code_on_a_mismatch(
    entry_point_connection: pg8000.dbapi.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    first = migrate.discover(_MIGRATIONS_DIR)[0]  # version 1: the committed set is contiguous from 1
    _execute(
        entry_point_connection,
        _PRE_HASH_LEDGER,
        "INSERT INTO schema_migrations (version, name) VALUES (1, 'another_branch')",
    )
    with pytest.raises(SystemExit) as excinfo:
        entry.main(['--check'])
    assert excinfo.value.code == entry.EXIT_LEDGER_FINDING
    assert f'{first.filename}: name differs' in capsys.readouterr().err
    assert _ledger_columns(entry_point_connection) == {'version', 'name', 'applied_at'}


def test_the_check_entry_point_exits_with_the_finding_code_on_a_forward_only_violation(
    entry_point_connection: pg8000.dbapi.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    # The ledger holds committed versions 1 and 3, as committed, but not 2.
    committed = migrate.discover(_MIGRATIONS_DIR)
    ledger = cloudsql.CloudSqlLedger(entry_point_connection)
    ledger.applied_migrations()
    for migration in (committed[0], committed[2]):
        ledger.record(migration, 'SELECT 1;')
    with pytest.raises(SystemExit) as excinfo:
        entry.main(['--check'])
    assert excinfo.value.code == entry.EXIT_LEDGER_FINDING
    assert 'forward-only violation' in capsys.readouterr().err


def test_the_check_entry_point_exits_with_the_unreadable_code_when_the_login_fails(
    postgres: testcontainers.postgres.PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _dial_instead_of_cloud_sql(monkeypatch, lambda: _dial(postgres, postgres.username, 'not-the-password'))
    with pytest.raises(SystemExit) as excinfo:
        entry.main(['--check'])
    assert excinfo.value.code == entry.EXIT_LEDGER_UNREADABLE
    assert 'could not read the ledger' in capsys.readouterr().err


def test_the_check_entry_point_exits_with_the_unreadable_code_without_a_read_privilege(
    postgres: testcontainers.postgres.PostgresContainer,
    connection: pg8000.dbapi.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cloudsql.CloudSqlLedger(connection).applied_migrations()
    _execute(connection, "CREATE ROLE stranger LOGIN PASSWORD 'stranger'")
    _dial_instead_of_cloud_sql(monkeypatch, lambda: _dial(postgres, 'stranger', 'stranger'))
    with pytest.raises(SystemExit) as excinfo:
        entry.main(['--check'])
    assert excinfo.value.code == entry.EXIT_LEDGER_UNREADABLE
    assert 'permission denied' in capsys.readouterr().err


def test_the_check_entry_point_passes_an_empty_database(
    entry_point_connection: pg8000.dbapi.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    entry.main(['--check'])
    assert 'the ledger matches the committed migrations' in capsys.readouterr().out
    assert _ledger_columns(entry_point_connection) == set()
