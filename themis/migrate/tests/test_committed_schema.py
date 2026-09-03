"""The committed migrations applied to an empty database, and what the resulting schema refuses.

A constraint is a claim about what the database will not hold, and the only way to check the claim
is to hand a real Postgres a row that violates it. Docker-gated, like every other test here that
needs a server.
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

# GRANT needs its grantee to exist; in the deploy these are Cloud SQL IAM users.
_DB_USERS = {
    'AUTH_DB_USER': 'themis-auth@example.iam',
    'WEB_DB_USER': 'themis-web@example.iam',
    'INGEST_DB_USER': 'themis-ingest@example.iam',
    'EVIDENCE_DB_USER': 'themis-evidence@example.iam',
}

_VARIANT = (
    'INSERT INTO curation.variants (id, gene, transcript, hgvs_c, disease_label, created_by)'
    " VALUES (%s, 'FBN1', %s, %s, 'Marfan syndrome', 'manager@example.org')"
)

# The tables `0012_curation_vocabulary` snapshots, the copy it takes of each, and the columns it then
# drops from the source — which the copy, taken first, still carries.
_SNAPSHOTS = {
    'curation.assessments': ('curation.assessments_v1', ()),
    'curation.drafts': ('curation.drafts_v1', ()),
    'curation.variants': ('curation.variants_v1', ('inheritance',)),
}


@pytest.fixture
def migrated(docker_daemon: None) -> Iterator[pg8000.dbapi.Connection]:
    """A throwaway Postgres carrying every committed migration."""
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
            migrate.run(migrate.discover(_MIGRATIONS_DIR), cloudsql.CloudSqlLedger(conn), substitutions=_DB_USERS)
            yield conn
        finally:
            conn.close()


def test_the_committed_migrations_apply_to_an_empty_database(migrated: pg8000.dbapi.Connection) -> None:
    with contextlib.closing(migrated.cursor()) as cursor:
        cursor.execute('SELECT count(*) FROM schema_migrations')
        assert cursor.fetchall()[0][0] == len(migrate.discover(_MIGRATIONS_DIR))


@pytest.mark.parametrize(
    ('transcript', 'hgvs_c'),
    [
        ('', 'NM_000138.5:c.7003C>T'),
        ('NM_000138.5:c.7003C>T', 'c.7003C>T'),
        ('NM_000138.5', 'NM_000138.5:c.7003C>T'),
    ],
)
def test_a_variant_may_not_pack_a_whole_term_into_one_column(
    migrated: pg8000.dbapi.Connection, transcript: str, hgvs_c: str
) -> None:
    with pytest.raises(pg8000.dbapi.DatabaseError), contextlib.closing(migrated.cursor()) as cursor:
        cursor.execute(_VARIANT, ('probe', transcript, hgvs_c))
    migrated.rollback()


@pytest.mark.parametrize(('transcript', 'hgvs_c'), [('', 'c.7003C>T'), ('NM_000138.5', '')])
def test_a_variant_may_not_leave_an_identity_column_blank(
    migrated: pg8000.dbapi.Connection, transcript: str, hgvs_c: str
) -> None:
    with pytest.raises(pg8000.dbapi.DatabaseError), contextlib.closing(migrated.cursor()) as cursor:
        cursor.execute(_VARIANT, ('probe', transcript, hgvs_c))
    migrated.rollback()


def test_a_variant_split_across_the_two_columns_is_accepted(migrated: pg8000.dbapi.Connection) -> None:
    with contextlib.closing(migrated.cursor()) as cursor:
        cursor.execute(_VARIANT, ('probe', 'NM_000138.5', 'c.7003C>T'))
        cursor.execute('SELECT transcript, hgvs_c FROM curation.variants WHERE id = %s', ('probe',))
        assert list(cursor.fetchall()) == [['NM_000138.5', 'c.7003C>T']]


def test_each_snapshot_carries_the_columns_of_the_table_it_was_taken_from(
    migrated: pg8000.dbapi.Connection,
) -> None:
    """Two readers select a snapshot's columns by name.

    `themis.curation.vocabulary_backfill` verifies row against snapshot row column by column, and the
    rollback the deploy runbook offers refills a dropped column out of the snapshot that kept it.
    """
    with contextlib.closing(migrated.cursor()) as cursor:
        for source, (snapshot, dropped) in _SNAPSHOTS.items():
            columns = _columns(cursor, snapshot)
            # A copy taken after the drop matches the source column for column just as well.
            assert set(dropped) <= {name for name, _type in columns}
            assert [column for column in columns if column[0] not in dropped] == _columns(cursor, source)


def _columns(cursor: pg8000.dbapi.Cursor, qualified: str) -> list[list[object]]:
    """The (name, type) pairs of a `schema.table`, in ordinal order."""
    schema, table = qualified.split('.')
    cursor.execute(
        'SELECT column_name, data_type FROM information_schema.columns'
        ' WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position',
        (schema, table),
    )
    found = [list(row) for row in cursor.fetchall()]
    assert found, f'no such table: {qualified}'
    return found
