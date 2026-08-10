"""Tests for `themis.litcache.cloudsql` — the picklable Cloud SQL crosswalk factory.

Hermetic: the connector and `iam_connect` are mocked, so no network or live instance.
"""

from __future__ import annotations

import pickle
import typing

import pytest

from themis.litcache import cloudsql, ingest_beam


class _FakeConnector:
    instances: typing.ClassVar[list[_FakeConnector]] = []

    def __init__(self) -> None:
        _FakeConnector.instances.append(self)


@pytest.fixture
def mocked(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    _FakeConnector.instances = []
    captured: dict[str, object] = {}

    def fake_iam_connect(pool: object, *, connection_name: str, database: str, db_user: str) -> object:
        conn = object()
        captured.update(pool=pool, connection_name=connection_name, database=database, db_user=db_user, connection=conn)
        return conn

    monkeypatch.setattr(cloudsql.connector, 'Connector', _FakeConnector)
    monkeypatch.setattr(cloudsql.sql, 'iam_connect', fake_iam_connect)
    return captured


def test_call_dials_iam_connect_with_the_configured_instance(mocked: dict[str, object]) -> None:
    factory = cloudsql.CrosswalkConnFactory(
        connection_name='proj:region:inst', database='litcache', iam_user='ingest-sa'
    )
    factory()
    assert mocked['connection_name'] == 'proj:region:inst'
    assert mocked['database'] == 'litcache'
    assert mocked['db_user'] == 'ingest-sa'
    assert isinstance(mocked['pool'], _FakeConnector)


@pytest.mark.usefixtures('mocked')
def test_connector_is_built_once_and_reused() -> None:
    factory = cloudsql.CrosswalkConnFactory(
        connection_name='proj:region:inst', database='litcache', iam_user='ingest-sa'
    )
    factory()
    factory()
    factory()
    assert len(_FakeConnector.instances) == 1  # one per-process Connector, reused across calls


def test_pickle_roundtrip_rebuilds_worker_state(mocked: dict[str, object]) -> None:
    factory = cloudsql.CrosswalkConnFactory(
        connection_name='proj:region:inst', database='litcache', iam_user='ingest-sa'
    )
    shipped = pickle.loads(pickle.dumps(factory))  # noqa: S301 — round-tripping our own object

    # The unpickled factory (a fresh worker) dials the configured instance and builds
    # its own Connector — the original's Connector/lock did not travel.
    shipped()
    assert mocked['connection_name'] == 'proj:region:inst'
    assert len(_FakeConnector.instances) == 1


def test_factory_satisfies_the_ingestion_conn_factory(mocked: dict[str, object]) -> None:
    # The production wiring, which nothing else in the repo composes: the annotation is the
    # assertion, so pyright fails if the factory's connection type drifts from the one
    # `run_ingestion` accepts.
    factory: ingest_beam.ConnFactory = cloudsql.CrosswalkConnFactory(
        connection_name='proj:region:inst', database='litcache', iam_user='ingest-sa'
    )
    assert factory() is mocked['connection']
