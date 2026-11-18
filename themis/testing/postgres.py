"""The throwaway Postgres every database test starts: Cloud SQL's major version, with pgAudit.

Migration 0020 creates the pgAudit extension, as Cloud SQL runs it, and the extension loads only on a
server that preloads its library. So a test that applies the committed migrations needs a server
built from `postgres-image/Dockerfile` beside this module and started with that preload. The
`postgres_image` fixture builds the image once per test run, serialized across workers; `container`
starts a server from it.

Registered from the repo-root `conftest.py` as a plugin, like `litcache_db`, so the fixture reaches
every test tree.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import pathlib
from collections.abc import Callable, Iterator

import pytest
import testcontainers.core.image
import testcontainers.postgres

_CONTEXT = pathlib.Path(__file__).resolve().parent / 'postgres-image'
_TAG = 'themis-test-postgres:16'
_COMMAND = 'postgres -c shared_preload_libraries=pgaudit'


def build_image_once(
    build: Callable[[], object],
    *,
    lock_dir: pathlib.Path,
    testrun_uid: str | None,
    worker: str | None,
) -> None:
    """Run `build` directly off xdist, or once across xdist workers sharing `testrun_uid`."""
    if testrun_uid is None or worker is None:
        build()
        return
    sentinel = lock_dir / f'postgres-image-{testrun_uid}.built'
    with (lock_dir / f'postgres-image-{testrun_uid}.lock').open('a', encoding='utf-8') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if not sentinel.exists():
            build()
            staged = lock_dir / f'postgres-image-{testrun_uid}.{worker}.{os.getpid()}.tmp'
            staged.write_text(_TAG, encoding='utf-8')
            staged.replace(sentinel)


@pytest.fixture(scope='session')
def postgres_image(docker_daemon: None, tmp_path_factory: pytest.TempPathFactory) -> str:
    """Build the test Postgres image once per test run, serialized across xdist workers.

    Returns:
        The image's tag, for `container`.
    """
    del docker_daemon  # depended on to gate on Docker; value unused
    # Under pytest-xdist each worker has its own session scope and its basetemp sits one
    # directory below the shared run root (`.../pytest-<n>/popen-gw<k>`). Locking in the
    # parent stops concurrent `docker build -t themis-test-postgres:16` calls from racing on
    # the tag on a cold daemon, while scoping the sentinel to `testrun_uid` ensures a later
    # pytest invocation still rebuilds after Dockerfile edits.
    build_image_once(
        lambda: testcontainers.core.image.DockerImage(path=_CONTEXT, tag=_TAG, clean_up=False).build(),
        lock_dir=tmp_path_factory.getbasetemp().parent,
        testrun_uid=os.environ.get('PYTEST_XDIST_TESTRUNUID'),
        worker=os.environ.get('PYTEST_XDIST_WORKER'),
    )
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
