"""A throwaway Postgres carrying the litcache crosswalk schema, as a pytest plugin.

Registered from the repo-root `conftest.py`, so the fixtures reach every test tree: the crosswalk and
ingestion tests under `tests/litcache/` mint against them, and so do the evidence service's literature
tests, whose backend mints too. A `conftest.py` is visible only below its own directory, which is why
this is a plugin rather than one.

The schema is built by the migrate runner's own discover/render/split — the deploy path runs the same
statements — so no test hand-writes DDL that could drift from the migration. Only
`0003_litcache_crosswalk` is applied: a later migration is applied by the test that is about it, so
its effect is observable rather than assumed.

Connections are pg8000, the driver the Cloud SQL connector uses in deploy.
"""

from __future__ import annotations

import contextlib
import pathlib
from collections.abc import Callable, Iterator

import pg8000.dbapi
import pytest
import testcontainers.postgres

from themis.migrate import migrate

_MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / 'themis' / 'migrate' / 'migrations'


def _connect(container: testcontainers.postgres.PostgresContainer) -> pg8000.dbapi.Connection:
    return pg8000.dbapi.connect(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(5432)),
        user='test',
        password='test',
        database='litcache',
    )


def _apply_migration(conn: pg8000.dbapi.Connection, name: str) -> None:
    sql = next(m.sql for m in migrate.discover(_MIGRATIONS) if m.name == name)
    with contextlib.closing(conn.cursor()) as cur:
        for statement in migrate.split_statements(migrate.render(sql, {})):
            cur.execute(statement)
    conn.commit()


@pytest.fixture(scope='session')
def postgres_container(docker_daemon: None) -> Iterator[testcontainers.postgres.PostgresContainer]:
    """A session-lived Postgres holding `litcache.crosswalk`."""
    del docker_daemon  # gate on a reachable Docker daemon (shared fixture)
    with testcontainers.postgres.PostgresContainer(
        'postgres:16', username='test', password='test', dbname='litcache'
    ) as container:
        with contextlib.closing(_connect(container)) as conn:
            _apply_migration(conn, 'litcache_crosswalk')
        yield container


@pytest.fixture(scope='session')
def crosswalk_connect(
    postgres_container: testcontainers.postgres.PostgresContainer,
) -> Callable[[], pg8000.dbapi.Connection]:
    """A factory that opens a fresh connection to the migration-applied container."""
    return lambda: _connect(postgres_container)


@pytest.fixture
def conn(crosswalk_connect: Callable[[], pg8000.dbapi.Connection]) -> Iterator[pg8000.dbapi.Connection]:
    """A context-managed connection with the crosswalk truncated for per-test isolation."""
    with contextlib.closing(crosswalk_connect()) as connection:  # schema applied once by postgres_container
        with contextlib.closing(connection.cursor()) as cur:
            cur.execute('TRUNCATE litcache.crosswalk')
        connection.commit()
        yield connection
