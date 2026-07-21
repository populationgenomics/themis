"""Tests for `themis.common.sql` — the IAM-authed Cloud SQL connect wiring.

The Protocols are enforced by pyright against the real pg8000 objects; here the only
runtime behaviour to pin is that `iam_connect` dials the instance with the pg8000 driver
and IAM auth, passing the config through unchanged.
"""

from __future__ import annotations

from themis.common import sql


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
