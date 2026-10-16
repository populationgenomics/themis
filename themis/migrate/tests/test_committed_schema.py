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
    'INSERT INTO curation.variants (id, gene, transcript, hgvs_c, disease_label, mondo_id, mode_of_inheritance,'
    " created_by) VALUES (%s, 'FBN1', %s, %s, 'Marfan syndrome', 'MONDO:0007947', 'AUTOSOMAL_DOMINANT',"
    " 'manager@example.org')"
)

_ENTITY = (
    'INSERT INTO curation.variants (id, gene, transcript, hgvs_c, disease_label, mondo_id, mode_of_inheritance,'
    " created_by) VALUES (%s, 'FBN1', 'NM_000138.5', 'c.7003C>T', 'Marfan syndrome', %s, %s, 'manager@example.org')"
)

_VARIANT_WITH_CLINICAL_CONTEXT = (
    'INSERT INTO curation.variants (id, gene, transcript, hgvs_c, disease_label, mondo_id, mode_of_inheritance,'
    " clinical_context, created_by) VALUES (%s, 'FBN1', 'NM_000138.5', 'c.7003C>T', 'Marfan syndrome',"
    " 'MONDO:0007947', 'AUTOSOMAL_DOMINANT', %s, 'manager@example.org')"
)


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


@pytest.mark.parametrize('mode', ['', 'X_LINKED', 'INHERITANCE_AUTOSOMAL_DOMINANT', 'autosomal_dominant'])
def test_a_variant_is_registered_under_one_of_the_five_modes(migrated: pg8000.dbapi.Connection, mode: str) -> None:
    with (
        pytest.raises(pg8000.dbapi.DatabaseError, match='variants_mode_of_inheritance_check'),
        contextlib.closing(migrated.cursor()) as cursor,
    ):
        cursor.execute(_ENTITY, ('probe', 'MONDO:0007947', mode))
    migrated.rollback()


def test_a_variant_is_registered_under_a_mode(migrated: pg8000.dbapi.Connection) -> None:
    with (
        pytest.raises(pg8000.dbapi.DatabaseError, match='mode_of_inheritance'),
        contextlib.closing(migrated.cursor()) as cursor,
    ):
        cursor.execute(_ENTITY, ('probe', 'MONDO:0007947', None))
    migrated.rollback()


@pytest.mark.parametrize('mondo_id', ['', 'mondo:0007947', 'MONDO:7947', 'Marfan syndrome'])
def test_a_variant_is_registered_against_a_mondo_term(migrated: pg8000.dbapi.Connection, mondo_id: str) -> None:
    with (
        pytest.raises(pg8000.dbapi.DatabaseError, match='variants_mondo_id_check'),
        contextlib.closing(migrated.cursor()) as cursor,
    ):
        cursor.execute(_ENTITY, ('probe', mondo_id, 'AUTOSOMAL_DOMINANT'))
    migrated.rollback()


def test_a_variant_left_without_a_mondo_term_is_refused(migrated: pg8000.dbapi.Connection) -> None:
    """The column has no default to fall back on: a registration states its term or is refused."""
    with pytest.raises(pg8000.dbapi.DatabaseError, match='mondo_id'), contextlib.closing(migrated.cursor()) as cursor:
        cursor.execute(
            'INSERT INTO curation.variants (id, gene, transcript, hgvs_c, disease_label, mode_of_inheritance,'
            " created_by) VALUES ('probe', 'FBN1', 'NM_000138.5', 'c.7003C>T', 'Marfan syndrome',"
            " 'AUTOSOMAL_DOMINANT', 'manager@example.org')"
        )
    migrated.rollback()


def test_a_variant_is_registered_once_per_entity(migrated: pg8000.dbapi.Connection) -> None:
    with contextlib.closing(migrated.cursor()) as cursor:
        cursor.execute(_ENTITY, ('first', 'MONDO:0007947', 'AUTOSOMAL_DOMINANT'))
    migrated.commit()
    with (
        pytest.raises(pg8000.dbapi.DatabaseError, match='variants_registration_key'),
        contextlib.closing(migrated.cursor()) as cursor,
    ):
        cursor.execute(_ENTITY, ('second', 'MONDO:0007947', 'AUTOSOMAL_DOMINANT'))
    migrated.rollback()


def test_a_second_mode_of_one_disease_is_a_registration_of_its_own(migrated: pg8000.dbapi.Connection) -> None:
    with contextlib.closing(migrated.cursor()) as cursor:
        cursor.execute(_ENTITY, ('dominant', 'MONDO:0007947', 'AUTOSOMAL_DOMINANT'))
        cursor.execute(_ENTITY, ('recessive', 'MONDO:0007947', 'AUTOSOMAL_RECESSIVE'))
        cursor.execute('SELECT id FROM curation.variants ORDER BY id')
        assert list(cursor.fetchall()) == [['dominant'], ['recessive']]


@pytest.mark.parametrize('clinical_context', ['', '   ', '\n\t ', 'x' * 10001])
def test_a_clinical_context_blank_or_over_the_bound_is_refused(
    migrated: pg8000.dbapi.Connection, clinical_context: str
) -> None:
    with (
        pytest.raises(pg8000.dbapi.DatabaseError, match='variants_clinical_context_check'),
        contextlib.closing(migrated.cursor()) as cursor,
    ):
        cursor.execute(_VARIANT_WITH_CLINICAL_CONTEXT, ('probe', clinical_context))
    migrated.rollback()


@pytest.mark.parametrize('clinical_context', [' Tall stature and ectopia lentis.\n', 'é' * 10000])
def test_a_clinical_context_within_the_bound_is_accepted(
    migrated: pg8000.dbapi.Connection, clinical_context: str
) -> None:
    """Whitespace around the prose is kept, and the bound counts characters, not bytes."""
    with contextlib.closing(migrated.cursor()) as cursor:
        cursor.execute(_VARIANT_WITH_CLINICAL_CONTEXT, ('probe', clinical_context))
        cursor.execute('SELECT clinical_context FROM curation.variants WHERE id = %s', ('probe',))
        assert list(cursor.fetchall()) == [[clinical_context]]
