"""`0014_curation_registration_mode` backfills the registered mode, or stops on a row it cannot place.

Each test seeds registrations under the schema `0011_curation` created, where the mode was still a
column, lets `0012_curation_vocabulary` snapshot and drop it, and then runs `0014` over what the
snapshot kept. Docker-gated, like every other test here that needs a server.
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
_REGISTRATION_MODE = 'curation_registration_mode'
_VOCABULARY = 'curation_vocabulary'

# GRANT needs its grantee to exist; in the deploy these are Cloud SQL IAM users.
_DB_USERS = {
    'AUTH_DB_USER': 'themis-auth@example.iam',
    'WEB_DB_USER': 'themis-web@example.iam',
    'INGEST_DB_USER': 'themis-ingest@example.iam',
    'EVIDENCE_DB_USER': 'themis-evidence@example.iam',
}

# (id, gene, mondo_id, mode as `0011_curation` spells it)
type _Registration = tuple[str, str, str, str]


class _Database:
    """A throwaway Postgres migrated to just before a named migration, applying the rest on request."""

    def __init__(self, conn: pg8000.dbapi.Connection) -> None:
        self.conn = conn
        self._ledger = cloudsql.CloudSqlLedger(conn)
        self._migrations = migrate.discover(_MIGRATIONS_DIR)

    def migrate_before(self, name: str) -> None:
        version = next(m.version for m in self._migrations if m.name == name)
        migrate.run([m for m in self._migrations if m.version < version], self._ledger, substitutions=_DB_USERS)

    def migrate_through(self, name: str) -> None:
        version = next(m.version for m in self._migrations if m.name == name)
        migrate.run([m for m in self._migrations if m.version <= version], self._ledger, substitutions=_DB_USERS)

    def register_with_mode(self, registrations: Sequence[_Registration]) -> None:
        """Rows as the `0011_curation` schema took them, the mode a column of its own."""
        with contextlib.closing(self.conn.cursor()) as cursor:
            for registration in registrations:
                cursor.execute(
                    'INSERT INTO curation.variants (id, gene, transcript, hgvs_c, disease_label, mondo_id, inheritance,'
                    " created_by) VALUES (%s, %s, 'NM_000000.1', %s, 'a disease', %s, %s, 'manager@example.org')",
                    (registration[0], registration[1], f'c.{registration[0]}', registration[2], registration[3]),
                )
        self.conn.commit()

    def modes(self) -> dict[str, str]:
        with contextlib.closing(self.conn.cursor()) as cursor:
            cursor.execute('SELECT id, mode_of_inheritance FROM curation.variants')
            return {row[0]: row[1] for row in cursor.fetchall()}


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


def test_every_registered_mode_is_carried_over(database: _Database) -> None:
    database.migrate_before(_VOCABULARY)
    database.register_with_mode(
        [
            ('dominant', 'FBN1', 'MONDO:0007947', 'AUTOSOMAL_DOMINANT'),
            ('recessive', 'PAH', 'MONDO:0009861', 'AUTOSOMAL_RECESSIVE'),
            ('semidominant', 'LDLR', 'MONDO:0007750', 'SEMIDOMINANT'),
            ('haemophilia', 'F8', 'MONDO:0010602', 'X_LINKED'),
            ('retinoschisis', 'RS1', 'MONDO:0010725', 'X_LINKED'),
            ('rett', 'MECP2', 'MONDO:0010726', 'X_LINKED'),
        ]
    )
    database.migrate_through(_REGISTRATION_MODE)
    assert database.modes() == {
        'dominant': 'AUTOSOMAL_DOMINANT',
        'recessive': 'AUTOSOMAL_RECESSIVE',
        'semidominant': 'SEMIDOMINANT',
        'haemophilia': 'X_LINKED_RECESSIVE',
        'retinoschisis': 'X_LINKED_RECESSIVE',
        'rett': 'X_LINKED_DOMINANT',
    }


def _registered_after_the_snapshot(database: _Database) -> None:
    database.migrate_through(_VOCABULARY)
    with contextlib.closing(database.conn.cursor()) as cursor:
        cursor.execute(
            'INSERT INTO curation.variants (id, gene, transcript, hgvs_c, disease_label, mondo_id, created_by)'
            " VALUES ('unsnapshotted', 'FBN1', 'NM_000138.5', 'c.7003C>T', 'Marfan syndrome', 'MONDO:0007947',"
            " 'manager@example.org')"
        )
    database.conn.commit()


def _x_linked_in_an_unmapped_gene(database: _Database) -> None:
    database.migrate_before(_VOCABULARY)
    database.register_with_mode([('dystrophy', 'DMD', 'MONDO:0010679', 'X_LINKED')])


def _without_a_mondo_term(database: _Database) -> None:
    database.migrate_before(_VOCABULARY)
    database.register_with_mode([('untermed', 'FBN1', '', 'AUTOSOMAL_DOMINANT')])


def _registered_twice(database: _Database) -> None:
    database.migrate_before(_VOCABULARY)
    with contextlib.closing(database.conn.cursor()) as cursor:
        for registration_id in ('first', 'second'):
            cursor.execute(
                'INSERT INTO curation.variants (id, gene, transcript, hgvs_c, disease_label, mondo_id, inheritance,'
                " created_by) VALUES (%s, 'FBN1', 'NM_000138.5', 'c.7003C>T', 'Marfan syndrome', 'MONDO:0007947',"
                " 'AUTOSOMAL_DOMINANT', 'manager@example.org')",
                (registration_id,),
            )
    database.conn.commit()


@pytest.mark.parametrize(
    ('seed', 'named'),
    [
        # Nothing records the mode a registration made after the snapshot was taken under.
        (_registered_after_the_snapshot, 'unsnapshotted'),
        # The retired X-linked member says nothing about dominance, and only three genes are mapped.
        (_x_linked_in_an_unmapped_gene, 'dystrophy'),
        (_without_a_mondo_term, 'untermed'),
        # Nothing before this migration kept a variant from being registered twice against one entity.
        (_registered_twice, r'first, second \(NM_000138\.5 c\.7003C>T, MONDO:0007947, AUTOSOMAL_DOMINANT\)'),
    ],
)
def test_a_registration_the_migration_cannot_place_stops_it_whole(
    database: _Database, seed: Callable[[_Database], None], named: str
) -> None:
    seed(database)
    with pytest.raises(pg8000.dbapi.DatabaseError, match=named):
        database.migrate_through(_REGISTRATION_MODE)
    database.conn.rollback()
    with contextlib.closing(database.conn.cursor()) as cursor:
        cursor.execute(
            'SELECT count(*) FROM information_schema.columns'
            " WHERE table_schema = 'curation' AND table_name = 'variants' AND column_name = 'mode_of_inheritance'"
        )
        assert cursor.fetchall()[0][0] == 0  # nothing of the migration survives its failure
