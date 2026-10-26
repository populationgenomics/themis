"""A Postgres container with the litcache crosswalk schema, for the deposit command's DB tests.

Mirrors the crosswalk tests' own fixture: the container applies the litcache migration once via
the migrate runner's discover/render/split (the deploy path runs the same migration), and a
per-test fixture truncates the crosswalk for isolation. Docker-gated through the shared
`docker_daemon` fixture (root `conftest.py`): an absent daemon skips these rather than hanging.
"""

from __future__ import annotations

import contextlib
import pathlib
from collections.abc import Iterator

import pg8000.dbapi
import pytest
import testcontainers.postgres

from themis.migrate import migrate

_MIGRATIONS = pathlib.Path(__file__).resolve().parents[3] / 'themis' / 'migrate' / 'migrations'


def _connect(container: testcontainers.postgres.PostgresContainer) -> pg8000.dbapi.Connection:
    return pg8000.dbapi.connect(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(5432)),
        user='test',
        password='test',
        database='litcache',
    )


def _apply_litcache_migration(conn: pg8000.dbapi.Connection) -> None:
    sql = next(m.sql for m in migrate.discover(_MIGRATIONS) if m.name == 'litcache_crosswalk')
    with contextlib.closing(conn.cursor()) as cur:
        for statement in migrate.split_statements(migrate.render(sql, {})):
            cur.execute(statement)
    conn.commit()


@pytest.fixture(scope='session')
def postgres_container(docker_daemon: None) -> Iterator[testcontainers.postgres.PostgresContainer]:
    del docker_daemon  # gate on a reachable Docker daemon (shared fixture)
    with testcontainers.postgres.PostgresContainer(
        'postgres:16', username='test', password='test', dbname='litcache'
    ) as container:
        with contextlib.closing(_connect(container)) as conn:
            _apply_litcache_migration(conn)
        yield container


@pytest.fixture
def conn(postgres_container: testcontainers.postgres.PostgresContainer) -> Iterator[pg8000.dbapi.Connection]:
    """A connection to the migration-applied container, crosswalk truncated for per-test isolation."""
    with contextlib.closing(_connect(postgres_container)) as connection:
        with contextlib.closing(connection.cursor()) as cur:
            cur.execute('TRUNCATE litcache.crosswalk')
        connection.commit()
        yield connection
