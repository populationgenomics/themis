"""The throwaway Postgres every database test starts: Cloud SQL's major version, with pgAudit.

Migration 0020 creates the pgAudit extension, as Cloud SQL runs it, and the extension loads only on a
server that preloads its library. So a test that applies the committed migrations needs a server
built from `postgres-image/Dockerfile` beside this module and started with that preload. The
`postgres_image` fixture builds the image once per session; `container` starts a server from it.

Registered from the repo-root `conftest.py` as a plugin, like `litcache_db`, so the fixture reaches
every test tree.
"""

from __future__ import annotations

import contextlib
import pathlib
from collections.abc import Iterator

import pytest
import testcontainers.core.image
import testcontainers.postgres

_CONTEXT = pathlib.Path(__file__).resolve().parent / 'postgres-image'
_TAG = 'themis-test-postgres:16'
_COMMAND = 'postgres -c shared_preload_libraries=pgaudit'


@pytest.fixture(scope='session')
def postgres_image(docker_daemon: None) -> str:
    """Build the test Postgres image once per session; Docker's layer cache spans sessions.

    Returns:
        The image's tag, for `container`.
    """
    del docker_daemon  # depended on to gate on Docker; value unused
    testcontainers.core.image.DockerImage(path=_CONTEXT, tag=_TAG, clean_up=False).build()
    return _TAG


@contextlib.contextmanager
def container(
    image: str, *, username: str | None = None, password: str | None = None, dbname: str | None = None
) -> Iterator[testcontainers.postgres.PostgresContainer]:
    """Start a throwaway Postgres server with pgAudit preloaded, and stop it on exit.

    Args:
        image: The tag the `postgres_image` fixture returns.
        username: The superuser to create; `PostgresContainer`'s default when None.
        password: That user's password; `PostgresContainer`'s default when None.
        dbname: The database to create; `PostgresContainer`'s default when None.

    Yields:
        The running container, from which a test reads its host, port and credentials.
    """
    with testcontainers.postgres.PostgresContainer(
        image, username=username, password=password, dbname=dbname
    ).with_command(_COMMAND) as postgres:
        yield postgres
