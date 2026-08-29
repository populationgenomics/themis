"""The literature interface's env contract: which adapter each selector value builds, fail-loud."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Self

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud.sql import connector as sql_connector

from themis.litcache import enqueue
from themis.rpc import literature_pb2
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import config
from themis.services.evidence.literature import litcache as litcache_backend

_BUCKET = 'a-bucket'
_CROSSWALK_VARS = (
    'THEMIS_LITERATURE_CROSSWALK_INSTANCE',
    'THEMIS_LITERATURE_CROSSWALK_DATABASE',
    'THEMIS_LITERATURE_CROSSWALK_DB_USER',
)
_CROSSWALK_ENV = dict(zip(_CROSSWALK_VARS, ('p:r:i', 'themis', 'themis-evidence@p.iam'), strict=True))
_CONVERT_VARS = (
    'THEMIS_LITERATURE_CONVERT_QUEUE',
    'THEMIS_LITERATURE_CONVERT_WORKER_URL',
    'THEMIS_LITERATURE_CONVERT_INVOKER_SA',
)
_CONVERT_ENV = dict(
    zip(
        _CONVERT_VARS,
        (
            'projects/p/locations/r/queues/themis-convert',
            'https://themis-convert-worker-abc-ts.a.run.app',
            'themis-convert-invoker@p.iam.gserviceaccount.com',
        ),
        strict=True,
    )
)


def _from_env() -> literature_backend.LiteratureBackend:
    """Select the backend as the entrypoint would; the fixture path reaches no client."""
    return config.backend_from_env(contextlib.AsyncExitStack())


class _FakeBucket:
    """A lazy bucket handle: listing a bucket that does not exist is what raises, as in GCS."""

    def __init__(self, name: str, existing: str | None) -> None:
        self._name = name
        self._existing = existing

    def list_blobs(self, *, prefix: str, max_results: int) -> list[str]:
        if self._name != self._existing:
            raise api_exceptions.NotFound(f'no such bucket: {self._name}')
        return [f'{prefix}doc-1/rendering.md'][:max_results]


class _FakeClient:
    """A ``storage.Client`` stand-in that records its own close."""

    def __init__(self, closed: list[bool], *, existing_bucket: str | None) -> None:
        self._closed = closed
        self._existing_bucket = existing_bucket

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(name, self._existing_bucket)

    def close(self) -> None:
        self._closed.append(True)


def _live_env(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'live')
    monkeypatch.setenv('THEMIS_FULLTEXT_BUCKET', _BUCKET)
    for var in (*_CROSSWALK_VARS, *_CONVERT_VARS):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config.storage, 'Client', lambda: client)


def test_backend_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('THEMIS_LITERATURE_BACKEND', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_LITERATURE_BACKEND'):
        _from_env()


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'bogus')
    with pytest.raises(SystemExit, match='bogus'):
        _from_env()


def test_live_backend_requires_the_fulltext_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'live')
    monkeypatch.delenv('THEMIS_FULLTEXT_BUCKET', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_FULLTEXT_BUCKET'):
        _from_env()


def test_fixture_seed_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    # The seed var, not the selector, is named — the operator has to know which value is missing.
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'fixture')
    monkeypatch.delenv('THEMIS_LITERATURE_FIXTURE', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_LITERATURE_FIXTURE'):
        _from_env()


def test_fixture_selector_builds_the_seeded_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THEMIS_LITERATURE_BACKEND', 'fixture')
    monkeypatch.setenv(
        'THEMIS_LITERATURE_FIXTURE',
        json.dumps({'doc-1': {'title': 'A paper', 'markdown': {'gcs_uri': 'gs://c/r.md', 'from_xml': True}}}),
    )
    info = asyncio.run(_from_env().describe_paper('doc-1'))
    assert info.title == 'A paper'
    assert info.default_representation == literature_pb2.REPRESENTATION_MARKDOWN


def test_live_selector_hands_its_client_to_the_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    _live_env(monkeypatch, _FakeClient(closed, existing_bucket=_BUCKET))

    async def build() -> None:
        async with contextlib.AsyncExitStack() as stack:
            assert isinstance(config.backend_from_env(stack), litcache_backend.LitcacheBackend)
            assert not closed, 'the client is held open for as long as the server runs'
        assert closed, 'the stack closed the client on unwind'

    asyncio.run(build())


def test_live_selector_fails_loud_on_an_unreadable_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    _live_env(monkeypatch, _FakeClient(closed, existing_bucket=None))

    async def build() -> None:
        async with contextlib.AsyncExitStack() as stack:
            with pytest.raises(SystemExit, match=_BUCKET):
                config.backend_from_env(stack)
        assert closed, 'a failed startup probe leaks no client'

    asyncio.run(build())


def test_live_selector_without_the_crosswalk_leaves_id_resolution_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Absent config is a legitimate deployment (the doc_id path needs no crosswalk), so it must not
    # fail startup. The RPC that needs it reports a permanent condition, distinct from an outage:
    # no number of retries wires a crosswalk.
    _live_env(monkeypatch, _FakeClient([], existing_bucket=_BUCKET))
    backend = _from_env()
    with pytest.raises(literature_backend.CrosswalkNotConfiguredError):
        asyncio.run(backend.resolve_external_ids(['doi:10.1/x']))


@pytest.mark.parametrize('omitted', _CROSSWALK_VARS)
def test_a_partial_crosswalk_config_fails_at_startup(monkeypatch: pytest.MonkeyPatch, omitted: str) -> None:
    # Half-configured would fail per request instead of at deploy, which is the shape that reaches
    # production unnoticed.
    _live_env(monkeypatch, _FakeClient([], existing_bucket=_BUCKET))
    for var, value in _CROSSWALK_ENV.items():
        if var != omitted:
            monkeypatch.setenv(var, value)
    with pytest.raises(SystemExit, match='all be set or all unset'):
        _from_env()


def test_a_complete_crosswalk_config_resolves_through_the_named_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    dialled: list[tuple[str, str, str]] = []
    closed: list[bool] = []

    class _FakeCursor:
        def execute(self, operation: str, args: object = ()) -> None:
            del operation, args

        def fetchall(self) -> list[tuple[str, str]]:
            return [('doi:10.1/x', 'doc-1')]

        def close(self) -> None:
            pass

    class _FakeConnection:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

        def close(self) -> None:
            pass

    class _FakeConnector:
        def connect(self, connection_name: str, driver: str, **kwargs: object) -> _FakeConnection:
            del driver
            dialled.append((connection_name, str(kwargs['db']), str(kwargs['user'])))
            return _FakeConnection()

        def close(self) -> None:
            closed.append(True)

    _live_env(monkeypatch, _FakeClient([], existing_bucket=_BUCKET))
    for var, value in _CROSSWALK_ENV.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setattr(sql_connector, 'Connector', _FakeConnector)

    async def build() -> dict[str, str]:
        async with contextlib.AsyncExitStack() as stack:
            backend = config.backend_from_env(stack)
            found = await backend.resolve_external_ids(['doi:10.1/x'])
            assert not closed, 'the connector is held for as long as the server runs'
        assert closed, 'the stack closed the connector on unwind'
        return found

    assert asyncio.run(build()) == {'doi:10.1/x': 'doc-1'}
    instance, database, db_user = _CROSSWALK_ENV.values()
    assert dialled == [(instance, database, db_user)]


class _FakeTasksClient:
    """A ``CloudTasksClient`` stand-in: a context manager that records its own exit and never dials."""

    def __init__(self, closed: list[bool]) -> None:
        self._closed = closed

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        del exc
        self._closed.append(True)


def test_live_selector_without_the_convert_trio_leaves_conversion_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Absent config is a legitimate deployment (every read path works without a queue), so it must not
    # fail startup. The rpc that would enqueue reports a permanent condition instead, distinct from an
    # outage: no number of retries provisions a queue.
    _live_env(monkeypatch, _FakeClient([], existing_bucket=_BUCKET))
    backend = _from_env()
    with pytest.raises(literature_backend.ConversionNotConfiguredError):
        asyncio.run(backend.request_conversions(['doc-1']))


@pytest.mark.parametrize('omitted', _CONVERT_VARS)
def test_a_partial_convert_config_fails_at_startup(monkeypatch: pytest.MonkeyPatch, omitted: str) -> None:
    # Half-configured would fail per request instead of at deploy — a queue path with no worker URL
    # builds tasks that dispatch nowhere.
    _live_env(monkeypatch, _FakeClient([], existing_bucket=_BUCKET))
    for var, value in _CONVERT_ENV.items():
        if var != omitted:
            monkeypatch.setenv(var, value)
    with pytest.raises(SystemExit, match='all be set or all unset'):
        _from_env()


def test_a_complete_convert_config_targets_the_named_queue_and_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    _live_env(monkeypatch, _FakeClient([], existing_bucket=_BUCKET))
    for var, value in _CONVERT_ENV.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setattr(config.tasks_v2, 'CloudTasksClient', lambda: _FakeTasksClient(closed))

    async def build() -> enqueue.ConversionTarget:
        async with contextlib.AsyncExitStack() as stack:
            backend = config.backend_from_env(stack)
            assert isinstance(backend, litcache_backend.LitcacheBackend)
            enqueuer = backend._enqueuer
            assert enqueuer is not None
            assert not closed, 'the client is held open for as long as the server runs'
            target = enqueuer._target
        assert closed, 'the stack closed the client on unwind'
        return target

    queue, worker_url, invoker = _CONVERT_ENV.values()
    target = asyncio.run(build())
    assert target == enqueue.ConversionTarget(
        queue_path=queue, worker_url=worker_url, invoker_service_account_email=invoker
    )
