"""Tests for `themis.common.sql` — the IAM-authed Cloud SQL connect wiring.

The Protocols are enforced by pyright against the real pg8000 objects; here the runtime behaviour to
pin is that `iam_connect` dials the instance with the pg8000 driver and IAM auth, passing the config
through unchanged, and that a service builds its connector where the refresh strategy is decided.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from google.cloud.sql import connector

from themis.common import sql

_SERVICES = pathlib.Path(sql.__file__).resolve().parents[1] / 'services'


class _FakePool:
    """Records the connect call and returns a stand-in connection."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.connection = object()

    def connect(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        return self.connection


def test_iam_connect_dials_with_pg8000_and_iam_auth() -> None:
    pool = _FakePool()

    conn = sql.iam_connect(
        pool,  # type: ignore[arg-type]  # structural stand-in for connector.Connector
        connection_name='proj:region:inst',
        database='themis',
        db_user='themis-auth@proj.iam',
    )

    assert conn is pool.connection
    (args, kwargs) = pool.calls[0]
    assert args == ('proj:region:inst', 'pg8000')
    assert kwargs == {'user': 'themis-auth@proj.iam', 'db': 'themis', 'enable_iam_auth': True}


def _constructs_a_connector(path: pathlib.Path) -> bool:
    """Whether `path` calls `Connector(...)`, through the module or imported by name."""
    tree = ast.parse(path.read_text('utf-8'), filename=str(path))
    return any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == 'Connector')
            or (isinstance(node.func, ast.Name) and node.func.id == 'Connector')
        )
        for node in ast.walk(tree)
    )


def test_no_service_builds_its_own_cloud_sql_connector() -> None:
    """The refresh strategy is a property of the runtime, not of the call site that happens to dial.

    A service that builds its own gets the library's default, which refreshes on a background task a
    CPU-idling Cloud Run instance does not run — and nothing about the call site makes that visible.
    `lazy_connector` is where the determination is recorded, so it is the only place a service
    constructs one; a service dialling past it is the drift this catches, not a style preference.

    A name check over the source, so it is an early warning and not the enforcement: it sees a
    construction spelled in a service's own file, and not one reached through a helper elsewhere in
    the tree that builds a default connector. That path has no check, only the docstring above.
    """
    assert _SERVICES.is_dir(), f'{_SERVICES} is not the services tree — the walk is looking in the wrong place'
    builders = sorted(
        path.relative_to(_SERVICES.parent).as_posix()
        for path in _SERVICES.rglob('*.py')
        if _constructs_a_connector(path)
    )
    assert not builders


def test_the_shared_connector_refreshes_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the strategy itself: which one a service gets is the helper's whole purpose.

    The constructor is stood in for rather than run: a real one resolves application-default
    credentials, which the test runner has none of, and starts an event-loop thread for a dial no
    test makes.
    """
    built: list[dict[str, object]] = []
    monkeypatch.setattr(connector, 'Connector', lambda **kwargs: built.append(kwargs))

    sql.lazy_connector()

    assert built == [{'refresh_strategy': connector.RefreshStrategy.LAZY}]
