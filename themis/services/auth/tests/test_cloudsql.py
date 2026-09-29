"""The Cloud SQL backend's connection reuse, against a throwaway Postgres.

The connector's IAM dial needs a real Cloud SQL instance, so these tests hand ``sql_pool.pooled_engine``
a plain pg8000 dial to a ``testcontainers`` Postgres instead; everything past the dial — the pool
settings, the query, the retry — is the production path. Docker-gated.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from concurrent import futures
from typing import Protocol

import pg8000.dbapi
import pytest
import sqlalchemy.exc
import testcontainers.postgres
from opentelemetry import trace
from opentelemetry.sdk import trace as sdk_trace
from opentelemetry.sdk.trace.export import in_memory_span_exporter

from themis.common import sql, sql_pool
from themis.services.auth import backend as auth_backend
from themis.services.auth import cloudsql

_POOLED_APP = 'auth-pool-under-test'
_SETTLE_TIMEOUT_S = 10
_SHORT_TIMEOUT_S = 0.5
_TRACER = trace.get_tracer(__name__)


@pytest.fixture(scope='module')
def postgres(docker_daemon: None) -> Iterator[testcontainers.postgres.PostgresContainer]:
    del docker_daemon  # gate on a reachable Docker daemon (shared fixture)
    with testcontainers.postgres.PostgresContainer('postgres:16-alpine') as container:
        yield container


def _dial(postgres: testcontainers.postgres.PostgresContainer, application_name: str) -> pg8000.dbapi.Connection:
    return pg8000.dbapi.connect(
        user=postgres.username,
        password=postgres.password,
        host=postgres.get_container_host_ip(),
        port=int(postgres.get_exposed_port(5432)),
        database=postgres.dbname,
        application_name=application_name,
    )


def _eventually(condition: Callable[[], bool], what: str) -> None:
    # A closed or terminated backend leaves pg_stat_activity a moment after the client lets go.
    deadline = time.monotonic() + _SETTLE_TIMEOUT_S
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError(f'timed out waiting until {what}')
        time.sleep(0.05)


class _Admin:
    """A connection outside the pool, for arranging rows and watching the server."""

    def __init__(self, conn: pg8000.dbapi.Connection) -> None:
        self._conn = conn
        self._conn.autocommit = True

    def execute(self, operation: str, args: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        with contextlib.closing(self._conn.cursor()) as cursor:
            cursor.execute(operation, args)
            return list(cursor.fetchall()) if cursor.description else []

    def pooled(self) -> list[tuple[object, ...]]:
        """(pid, state) of each connection the pool has open, as the server sees it."""
        return self.execute('SELECT pid, state FROM pg_stat_activity WHERE application_name = %s', (_POOLED_APP,))

    def settle_on(self, count: int) -> None:
        _eventually(lambda: len(self.pooled()) == count, f'the pool holds {count} connection(s)')

    def terminate_pooled(self) -> None:
        self.execute(
            'SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = %s', (_POOLED_APP,)
        )
        self.settle_on(0)


@pytest.fixture
def admin(postgres: testcontainers.postgres.PostgresContainer) -> Iterator[_Admin]:
    with contextlib.closing(_dial(postgres, 'auth-pool-test-admin')) as conn:
        db = _Admin(conn)
        # A lock the pool leaves held fails the arranging DDL instead of hanging it.
        db.execute(f"SET lock_timeout = '{_SETTLE_TIMEOUT_S}s'")
        db.execute('DROP TABLE IF EXISTS session_context')
        db.execute(
            'CREATE TABLE session_context '
            '(token_hash text PRIMARY KEY, project_id text NOT NULL, analysis_id text NOT NULL)'
        )
        db.execute(
            'INSERT INTO session_context VALUES (%s, %s, %s)', (auth_backend.hash_token('tok-1'), 'proj-1', 'ana-1')
        )
        yield db
        db.terminate_pooled()


class _CountingDial:
    """The engine's dial, counting how many connections it opened."""

    def __init__(self, postgres: testcontainers.postgres.PostgresContainer) -> None:
        self._postgres = postgres
        self._lock = threading.Lock()
        self.opened = 0

    def __call__(self) -> sql.Connection:
        with self._lock:
            self.opened += 1
        return _dial(self._postgres, _POOLED_APP)


@pytest.fixture
def dial(postgres: testcontainers.postgres.PostgresContainer) -> _CountingDial:
    return _CountingDial(postgres)


class _MakeBackend(Protocol):
    def __call__(
        self, *, recycle_s: float = ..., statement_timeout_s: float = ..., io_timeout_s: float = ...
    ) -> cloudsql.CloudSqlBackend: ...


@pytest.fixture
def make_backend(admin: _Admin, dial: _CountingDial) -> Iterator[_MakeBackend]:
    """Build a backend on the production pool settings, overriding the timing ones a test must shorten."""
    del admin  # depended on so the table exists first and the pool's connections are reaped after
    built: list[cloudsql.CloudSqlBackend] = []

    def make(
        *,
        recycle_s: float = cloudsql.POOL_RECYCLE_S,
        statement_timeout_s: float = sql_pool.STATEMENT_TIMEOUT_S,
        io_timeout_s: float = sql_pool.IO_TIMEOUT_S,
    ) -> cloudsql.CloudSqlBackend:
        pooled = sql_pool.pooled_engine(
            dial,
            pool_size=cloudsql.POOL_SIZE,
            recycle_s=recycle_s,
            statement_timeout_s=statement_timeout_s,
            io_timeout_s=io_timeout_s,
        )
        built.append(cloudsql.CloudSqlBackend(pooled))
        return built[-1]

    yield make
    for backend in built:
        backend.close()


@pytest.fixture
def backend(make_backend: _MakeBackend) -> cloudsql.CloudSqlBackend:
    return make_backend()


async def _resolve_project(backend: cloudsql.CloudSqlBackend) -> str:
    return (await backend.resolve('tok-1')).project_id


def test_resolves_reuse_a_connection(backend: cloudsql.CloudSqlBackend, dial: _CountingDial) -> None:
    async def run() -> list[str]:
        return [await _resolve_project(backend) for _ in range(5)]

    assert asyncio.run(run()) == ['proj-1'] * 5
    assert dial.opened == 1


def test_concurrent_resolves_open_at_most_the_pool_size(backend: cloudsql.CloudSqlBackend, dial: _CountingDial) -> None:
    resolves = 4 * cloudsql.POOL_SIZE

    async def run() -> list[str]:
        return await asyncio.gather(*(_resolve_project(backend) for _ in range(resolves)))

    assert asyncio.run(run()) == ['proj-1'] * resolves
    assert 1 <= dial.opened <= cloudsql.POOL_SIZE


def test_a_dropped_connection_is_replaced(
    backend: cloudsql.CloudSqlBackend, admin: _Admin, dial: _CountingDial, caplog: pytest.LogCaptureFixture
) -> None:
    async def run() -> list[str]:
        first = await _resolve_project(backend)
        admin.terminate_pooled()  # the server drops the idle connection under the pool
        # The resolve after the drop succeeds, and the replacement is pooled in turn.
        return [first, await _resolve_project(backend), await _resolve_project(backend)]

    assert asyncio.run(run()) == ['proj-1'] * 3
    assert dial.opened == 2
    assert 'retrying' not in caplog.text, 'the checkout ping replaced it before the query ran'


def test_a_revoked_token_stops_resolving_through_a_warm_connection(
    backend: cloudsql.CloudSqlBackend, admin: _Admin, dial: _CountingDial
) -> None:
    async def run() -> None:
        await backend.resolve('tok-1')
        admin.execute('DELETE FROM session_context WHERE token_hash = %s', (auth_backend.hash_token('tok-1'),))
        with pytest.raises(auth_backend.UnresolvedError):
            await backend.resolve('tok-1')

    asyncio.run(run())
    assert dial.opened == 1, 'the second resolve ran on the warm connection'


def test_a_query_error_leaves_the_pool_usable(
    backend: cloudsql.CloudSqlBackend, admin: _Admin, dial: _CountingDial, caplog: pytest.LogCaptureFixture
) -> None:
    async def run() -> None:
        await backend.resolve('tok-1')
        admin.execute('ALTER TABLE session_context RENAME TO session_context_away')
        with pytest.raises(sqlalchemy.exc.DatabaseError):
            await backend.resolve('tok-1')  # loud, not an unresolved token
        admin.execute('ALTER TABLE session_context_away RENAME TO session_context')

        assert [await _resolve_project(backend) for _ in range(3)] == ['proj-1'] * 3

    asyncio.run(run())
    assert 'retrying' not in caplog.text, 'a statement error is the answer, not a lost connection'
    assert dial.opened == 1, 'the connection that raised stays usable'


def test_an_idle_pooled_connection_holds_no_transaction(backend: cloudsql.CloudSqlBackend, admin: _Admin) -> None:
    # An open transaction's lock would queue a migration's ALTER TABLE on session_context behind it.
    asyncio.run(backend.resolve('tok-1'))
    assert [state for _, state in admin.pooled()] == ['idle']


def test_a_connection_past_the_recycle_age_is_replaced(
    make_backend: _MakeBackend, admin: _Admin, dial: _CountingDial
) -> None:
    backend = make_backend(recycle_s=_SHORT_TIMEOUT_S)

    async def run() -> None:
        await backend.resolve('tok-1')
        await asyncio.sleep(2 * _SHORT_TIMEOUT_S)
        await backend.resolve('tok-1')

    asyncio.run(run())
    assert dial.opened == 2
    admin.settle_on(1)  # the recycled connection is closed, not leaked


def test_a_silent_connection_times_out_and_is_replaced(
    make_backend: _MakeBackend,
    postgres: testcontainers.postgres.PostgresContainer,
    admin: _Admin,
    dial: _CountingDial,
) -> None:
    # A stopped backend reads like a connection the network dropped without a reset: nothing answers.
    backend = make_backend(io_timeout_s=_SHORT_TIMEOUT_S)
    asyncio.run(backend.resolve('tok-1'))
    ((stopped, _),) = admin.pooled()
    postgres.exec(['kill', '-STOP', str(stopped)])
    # In a thread of its own: a resolve that never times out would otherwise hang the test.
    with futures.ThreadPoolExecutor(max_workers=1) as executor:
        try:
            project = executor.submit(asyncio.run, _resolve_project(backend)).result(timeout=_SETTLE_TIMEOUT_S)
        finally:
            postgres.exec(['kill', '-CONT', str(stopped)])  # before the executor joins a hung thread
    assert project == 'proj-1'
    assert dial.opened == 2


def test_a_connection_that_times_out_mid_query_is_discarded(
    make_backend: _MakeBackend, admin: _Admin, dial: _CountingDial
) -> None:
    # The ping passes and the query then stalls, here behind a lock: its connection is mid-protocol.
    backend = make_backend(io_timeout_s=_SHORT_TIMEOUT_S)
    asyncio.run(backend.resolve('tok-1'))
    admin.execute('BEGIN')
    admin.execute('LOCK TABLE session_context IN ACCESS EXCLUSIVE MODE')
    # In a thread of its own: a resolve that never times out would otherwise hang the test.
    with futures.ThreadPoolExecutor(max_workers=1) as executor:
        try:
            stalled = executor.submit(asyncio.run, backend.resolve('tok-1'))
            # `exception` itself raises if the resolve never returns; the socket's timeout is its error.
            assert isinstance(stalled.exception(timeout=_SETTLE_TIMEOUT_S), TimeoutError)
        finally:
            admin.execute('COMMIT')  # before the executor joins a hung thread
    admin.settle_on(0)  # both stalled connections were closed, not returned to the pool

    assert asyncio.run(backend.resolve('tok-1')).project_id == 'proj-1'
    assert dial.opened == 3


def test_a_statement_past_the_statement_timeout_is_cancelled_by_the_server(
    make_backend: _MakeBackend, admin: _Admin, dial: _CountingDial, caplog: pytest.LogCaptureFixture
) -> None:
    # Abandoning the socket instead would leave the server's backend queued on the lock, holding a
    # connection slot, while the pool dialed a replacement.
    backend = make_backend(statement_timeout_s=_SHORT_TIMEOUT_S)
    asyncio.run(backend.resolve('tok-1'))
    admin.execute('BEGIN')
    admin.execute('LOCK TABLE session_context IN ACCESS EXCLUSIVE MODE')
    with futures.ThreadPoolExecutor(max_workers=1) as executor:
        try:
            stalled = executor.submit(asyncio.run, backend.resolve('tok-1'))
            error = stalled.exception(timeout=_SETTLE_TIMEOUT_S)  # raises itself if the resolve never returns
            assert isinstance(error, sqlalchemy.exc.DatabaseError)
            assert 'statement timeout' in str(error)
        finally:
            admin.execute('COMMIT')  # before the executor joins a hung thread

    assert asyncio.run(backend.resolve('tok-1')).project_id == 'proj-1'
    assert dial.opened == 1, 'the cancelled statement left its connection usable'
    assert 'retrying' not in caplog.text


def test_a_failed_dial_is_not_retried() -> None:
    attempts = 0

    def refuse() -> sql.Connection:
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError('nothing listens there')

    backend = cloudsql.CloudSqlBackend(
        sql_pool.pooled_engine(refuse, pool_size=cloudsql.POOL_SIZE, recycle_s=cloudsql.POOL_RECYCLE_S)
    )
    with pytest.raises(sql_pool.DialError):
        asyncio.run(backend.resolve('tok-1'))
    assert attempts == 1


def test_a_silent_socket_does_not_discard_the_other_connections(admin: _Admin, dial: _CountingDial) -> None:
    pooled = sql_pool.pooled_engine(dial, pool_size=2, recycle_s=cloudsql.POOL_RECYCLE_S, io_timeout_s=_SHORT_TIMEOUT_S)

    def count_rows() -> object:
        with pooled.connect() as conn:
            return conn.exec_driver_sql('SELECT count(*) FROM session_context').scalar()

    try:
        with pooled.connect(), pooled.connect():
            pass  # two warm connections
        admin.execute('BEGIN')
        admin.execute('LOCK TABLE session_context IN ACCESS EXCLUSIVE MODE')
        with futures.ThreadPoolExecutor(max_workers=1) as executor:
            try:
                stalled = executor.submit(count_rows)
                assert isinstance(stalled.exception(timeout=_SETTLE_TIMEOUT_S), TimeoutError)
            finally:
                admin.execute('COMMIT')  # before the executor joins a hung thread

        assert count_rows() == 1
        assert dial.opened == 2, 'the connection that stayed idle was reused, not recycled'
    finally:
        pooled.dispose()


class _ContainerConnector:
    """Stands in for the Cloud SQL connector: whatever instance `connect` names, it dials the throwaway Postgres."""

    def __init__(self, postgres: testcontainers.postgres.PostgresContainer) -> None:
        self._postgres = postgres

    def connect(self, instance_connection_string: str, driver: str, **kwargs: object) -> pg8000.dbapi.Connection:
        del instance_connection_string, driver, kwargs
        return _dial(self._postgres, _POOLED_APP)


def _parent_id(span: sdk_trace.ReadableSpan) -> int | None:
    return None if span.parent is None else span.parent.span_id


def test_a_resolve_is_a_query_span_and_a_dial_is_its_connect_child(
    admin: _Admin,
    postgres: testcontainers.postgres.PostgresContainer,
    recorded_spans: in_memory_span_exporter.InMemorySpanExporter,
) -> None:
    del admin  # depended on so the table exists first and the pool's connections are reaped after
    backend = cloudsql.CloudSqlBackend(
        sql_pool.cloud_sql_engine(
            _ContainerConnector(postgres),  # pyright: ignore[reportArgumentType]
            connection_name='proj:region:instance',
            database='themis',
            db_user='themis-auth@proj.iam',
            pool_size=cloudsql.POOL_SIZE,
            recycle_s=cloudsql.POOL_RECYCLE_S,
        )
    )
    try:
        with _TRACER.start_as_current_span('rpc') as rpc:
            assert asyncio.run(_resolve_project(backend)) == 'proj-1'
            assert asyncio.run(_resolve_project(backend)) == 'proj-1'
    finally:
        backend.close()

    spans = recorded_spans.get_finished_spans()
    queries = [s for s in spans if s.name == 'cloudsql.query']
    # Only the first checkout dials: the second resolve runs on the warm connection.
    (connect,) = [s for s in spans if s.name == 'cloudsql.connect']
    assert len(queries) == 2
    # The query runs on a worker thread; its span is still the rpc's child, not a trace of its own.
    assert [_parent_id(q) for q in queries] == [rpc.get_span_context().span_id] * 2
    assert queries[0].context is not None
    assert _parent_id(connect) == queries[0].context.span_id
    assert connect.attributes is not None
    assert connect.attributes['db.namespace'] == 'themis'
