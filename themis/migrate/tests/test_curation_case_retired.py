"""`0017_curation_case_retired` deletes the case rows and requires a clinical context, or stops on what it cannot.

Each test seeds a curation under the schema as it stands before `0017`, where the clinical context is
nullable and a worksheet may hold case rows, then runs `0017` over it. Docker-gated, like every other
test here that needs a server.
"""

from __future__ import annotations

import contextlib
import pathlib
from collections.abc import Callable, Iterator, Sequence

import pg8000.dbapi
import pytest
import testcontainers.postgres

from themis.migrate import cloudsql, migrate

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / 'migrations'
_CASE_RETIRED = 'curation_case_retired'

# GRANT needs its grantee to exist; in the deploy these are Cloud SQL IAM users.
_DB_USERS = {
    'AUTH_DB_USER': 'themis-auth@example.iam',
    'WEB_DB_USER': 'themis-web@example.iam',
    'INGEST_DB_USER': 'themis-ingest@example.iam',
    'EVIDENCE_DB_USER': 'themis-evidence@example.iam',
}

# The bytes of a stored assessment; nothing here decodes them.
_ASSESSMENT = b'\x0a\x00'


class _Database:
    """A throwaway Postgres migrated to just before `0017`, applying it on request."""

    def __init__(self, conn: pg8000.dbapi.Connection) -> None:
        self.conn = conn
        self._ledger = cloudsql.CloudSqlLedger(conn)
        self._migrations = migrate.discover(_MIGRATIONS_DIR)
        version = self._version()
        migrate.run([m for m in self._migrations if m.version < version], self._ledger, substitutions=_DB_USERS)

    def _version(self) -> int:
        return next(m.version for m in self._migrations if m.name == _CASE_RETIRED)

    def retire_the_case(self) -> None:
        version = self._version()
        migrate.run([m for m in self._migrations if m.version <= version], self._ledger, substitutions=_DB_USERS)

    def register(self, variant_id: str, clinical_context: str | None) -> None:
        """A registration, with a worksheet of its own under the id `w-<variant_id>`."""
        with contextlib.closing(self.conn.cursor()) as cursor:
            cursor.execute(
                'INSERT INTO curation.variants (id, gene, transcript, hgvs_c, disease_label, mondo_id,'
                " mode_of_inheritance, clinical_context, created_by) VALUES (%s, 'FBN1', 'NM_000138.5', %s,"
                " 'Marfan syndrome', 'MONDO:0007947', 'AUTOSOMAL_DOMINANT', %s, 'manager@example.org')",
                (variant_id, f'c.{variant_id}', clinical_context),
            )
            cursor.execute(
                'INSERT INTO curation.worksheets (id, variant_id, curator_email, workflows_version, assigned_by)'
                " VALUES (%s, %s, 'curator@example.org', 'v', 'manager@example.org')",
                (f'w-{variant_id}', variant_id),
            )
        self.conn.commit()

    def draft(self, worksheet_id: str, workflow_ids: Sequence[str]) -> None:
        with contextlib.closing(self.conn.cursor()) as cursor:
            for workflow_id in workflow_ids:
                cursor.execute(
                    'INSERT INTO curation.drafts (worksheet_id, workflow_id, assessment) VALUES (%s, %s, %s)',
                    (worksheet_id, workflow_id, _ASSESSMENT),
                )
        self.conn.commit()

    def submit(self, submission_id: str, worksheet_id: str, workflow_ids: Sequence[str]) -> None:
        with contextlib.closing(self.conn.cursor()) as cursor:
            cursor.execute(
                'INSERT INTO curation.submissions (id, worksheet_id) VALUES (%s, %s)', (submission_id, worksheet_id)
            )
            for workflow_id in workflow_ids:
                cursor.execute(
                    'INSERT INTO curation.assessments (submission_id, workflow_id, assessment) VALUES (%s, %s, %s)',
                    (submission_id, workflow_id, _ASSESSMENT),
                )
        self.conn.commit()

    def rows(self, table: str) -> list[tuple[str, str]]:
        key = 'worksheet_id' if table == 'curation.drafts' else 'submission_id'
        with contextlib.closing(self.conn.cursor()) as cursor:
            cursor.execute(f'SELECT {key}, workflow_id FROM {table} ORDER BY 1, 2')  # noqa: S608 -- a fixed table name
            return [(row[0], row[1]) for row in cursor.fetchall()]


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


def test_the_case_rows_go_and_every_other_row_stays(database: _Database) -> None:
    database.register('a', 'Proband with aortic root dilation.')
    database.draft('w-a', ['case', 'routing', 'pop_frq'])
    database.submit('s-a', 'w-a', ['case', 'routing', 'pop_frq'])
    database.retire_the_case()
    assert database.rows('curation.drafts') == [('w-a', 'pop_frq'), ('w-a', 'routing')]
    assert database.rows('curation.assessments') == [('s-a', 'pop_frq'), ('s-a', 'routing')]


def _without_a_clinical_context(database: _Database) -> None:
    database.register('uncontexted', None)


def _submitted_only_a_case_row(database: _Database) -> None:
    database.register('a', 'Proband with ectopia lentis.')
    database.submit('s-only-case', 'w-a', ['case'])


@pytest.mark.parametrize(
    ('seed', 'named'),
    [
        (
            _without_a_clinical_context,
            r'uncontexted \(NM_000138\.5:c\.uncontexted, MONDO:0007947, AUTOSOMAL_DOMINANT\)',
        ),
        # Deleting its one row would leave a submission that committed nothing.
        (_submitted_only_a_case_row, 's-only-case'),
    ],
)
def test_a_row_the_migration_cannot_retire_stops_it_whole(
    database: _Database, seed: Callable[[_Database], None], named: str
) -> None:
    database.register('answered', 'Proband with aortic root dilation.')
    database.draft('w-answered', ['case'])
    seed(database)
    with pytest.raises(pg8000.dbapi.DatabaseError, match=named):
        database.retire_the_case()
    database.conn.rollback()
    # The check stops the migration before anything changes: the case draft stays, the column stays nullable.
    assert database.rows('curation.drafts') == [('w-answered', 'case')]
    with contextlib.closing(database.conn.cursor()) as cursor:
        cursor.execute(
            'SELECT is_nullable FROM information_schema.columns'
            " WHERE table_schema = 'curation' AND table_name = 'variants' AND column_name = 'clinical_context'"
        )
        assert list(cursor.fetchall()) == [['YES']]
