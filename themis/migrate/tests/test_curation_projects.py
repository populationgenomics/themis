"""`0019_curation_projects` moves every registration to Project `validation` and drops the role table.

Each test seeds a curation under the schema as it stands before `0019`, where a registration names no
Project and `curation.roles` holds who may curate, then runs `0019` over it. Docker-gated, like every
other test here that needs a server.
"""

from __future__ import annotations

import contextlib
import pathlib
from collections.abc import Iterator

import pg8000.dbapi
import pytest
import testcontainers.postgres

from themis.migrate import cloudsql, migrate

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / 'migrations'
_CURATION_PROJECTS = 'curation_projects'

# GRANT needs its grantee to exist; in the deploy these are Cloud SQL IAM users.
_DB_USERS = {
    'AUTH_DB_USER': 'themis-auth@example.iam',
    'WEB_DB_USER': 'themis-web@example.iam',
    'INGEST_DB_USER': 'themis-ingest@example.iam',
    'EVIDENCE_DB_USER': 'themis-evidence@example.iam',
}


class _Database:
    """A throwaway Postgres migrated to just before `0019`, applying it on request."""

    def __init__(self, conn: pg8000.dbapi.Connection) -> None:
        self.conn = conn
        self._ledger = cloudsql.CloudSqlLedger(conn)
        self._migrations = migrate.discover(_MIGRATIONS_DIR)
        self._version = next(m.version for m in self._migrations if m.name == _CURATION_PROJECTS)
        migrate.run([m for m in self._migrations if m.version < self._version], self._ledger, substitutions=_DB_USERS)

    def bind_to_projects(self) -> None:
        migrate.run([m for m in self._migrations if m.version <= self._version], self._ledger, substitutions=_DB_USERS)

    def register(self, variant_id: str, hgvs_c: str) -> None:
        """A registration, with a worksheet of its own under the id `w-<variant_id>`."""
        with contextlib.closing(self.conn.cursor()) as cursor:
            cursor.execute(
                'INSERT INTO curation.variants (id, gene, transcript, hgvs_c, disease_label, mondo_id,'
                " mode_of_inheritance, clinical_context, created_by) VALUES (%s, 'FBN1', 'NM_000138.5', %s,"
                " 'Marfan syndrome', 'MONDO:0007947', 'AUTOSOMAL_DOMINANT', 'Tall stature.', 'manager@example.org')",
                (variant_id, hgvs_c),
            )
            cursor.execute(
                'INSERT INTO curation.worksheets (id, variant_id, curator_email, workflows_version, assigned_by)'
                " VALUES (%s, %s, 'curator@example.org', 'v', 'manager@example.org')",
                (f'w-{variant_id}', variant_id),
            )
        self.conn.commit()

    def grant_role(self, email: str, role: str) -> None:
        with contextlib.closing(self.conn.cursor()) as cursor:
            cursor.execute(
                "INSERT INTO curation.roles (email, role, added_by) VALUES (%s, %s, 'manager@example.org')",
                (email, role),
            )
        self.conn.commit()

    def query(self, sql: str) -> list[list[object]]:
        with contextlib.closing(self.conn.cursor()) as cursor:
            cursor.execute(sql)
            return [list(row) for row in cursor.fetchall()]


@pytest.fixture
def database(docker_daemon: None) -> Iterator[_Database]:
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
            with contextlib.closing(conn.cursor()) as cursor:
                for login in _DB_USERS.values():
                    cursor.execute(f'CREATE ROLE "{login}"')
            conn.commit()
            yield _Database(conn)
        finally:
            conn.close()


def test_every_registration_moves_to_validation_while_the_role_table_holds_rows(database: _Database) -> None:
    database.register('a', 'c.1A>G')
    database.register('b', 'c.2A>G')
    database.grant_role('curator@example.org', 'curator')
    database.bind_to_projects()
    assert database.query('SELECT id, project_id FROM curation.variants ORDER BY id') == [
        ['a', 'validation'],
        ['b', 'validation'],
    ]


def test_an_empty_curation_migrates_too(database: _Database) -> None:
    database.bind_to_projects()
    assert database.query('SELECT count(*) FROM curation.variants') == [[0]]
