"""Tests for `litcache.crosswalk` against a throwaway Postgres.

The `UNIQUE`-in-one-transaction mint semantics are the thing under test, so
these run against a real Postgres (testcontainers), never a mock. Gated on a
reachable Docker daemon via the shared `docker_daemon` fixture: an absent, down,
or asleep daemon skips them rather than hanging the suite.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from collections.abc import Callable

import pg8000.dbapi

from themis.litcache import crosswalk


def _doc_ids(conn: pg8000.dbapi.Connection) -> dict[str, str]:
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('SELECT external_id, doc_id FROM litcache.crosswalk')
        return dict(cur.fetchall())


def test_fresh_mint_assigns_a_new_uuid(conn: pg8000.dbapi.Connection) -> None:
    result = crosswalk.mint(conn, ['doi:10.1/x', 'pmid:111'])

    assert result.minted is True
    assert result.linked_doc_ids == ()
    assert uuid.UUID(result.doc_id)  # a real uuid4 string
    assert _doc_ids(conn) == {'doi:10.1/x': result.doc_id, 'pmid:111': result.doc_id}


def test_single_id_collision_adopts_the_incumbent(conn: pg8000.dbapi.Connection) -> None:
    first = crosswalk.mint(conn, ['doi:10.1/x'])
    second = crosswalk.mint(conn, ['doi:10.1/x'])

    assert second.minted is False
    assert second.doc_id == first.doc_id
    assert second.linked_doc_ids == ()


def test_partial_overlap_adopts_and_records_the_new_id(conn: pg8000.dbapi.Connection) -> None:
    first = crosswalk.mint(conn, ['doi:10.1/x', 'pmid:111'])
    # A later artifact for the same paper brings a not-yet-seen id (pmcid).
    second = crosswalk.mint(conn, ['pmid:111', 'pmcid:PMC9'])

    assert second.minted is False
    assert second.doc_id == first.doc_id
    assert second.linked_doc_ids == ()
    assert _doc_ids(conn)['pmcid:PMC9'] == first.doc_id


def test_two_incumbents_signal_a_cross_paper_link(conn: pg8000.dbapi.Connection) -> None:
    a = crosswalk.mint(conn, ['doi:10.1/x'])
    b = crosswalk.mint(conn, ['pmid:111'])
    # A third artifact carries both ids: the two works are one paper.
    linked = crosswalk.mint(conn, ['doi:10.1/x', 'pmid:111'])

    assert linked.minted is False
    assert linked.linked_doc_ids == tuple(sorted([a.doc_id, b.doc_id]))
    assert linked.doc_id == min(a.doc_id, b.doc_id)  # canonical = lowest in the class


def test_concurrent_mint_of_one_id_yields_a_single_uuid(
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
) -> None:
    # Force the insert race deterministically: connection A claims the id but holds its
    # transaction open; B mints the same id concurrently and blocks on the unique index
    # until A commits, then hits the violation, retries, and adopts A's doc_id. Three
    # live connections at once, so this manages them directly rather than via a fixture.
    with (
        contextlib.closing(crosswalk_connect()) as a,
        contextlib.closing(crosswalk_connect()) as b,
        contextlib.closing(crosswalk_connect()) as verify,
    ):
        with contextlib.closing(a.cursor()) as cur:
            cur.execute('TRUNCATE litcache.crosswalk')  # isolate: this test does not use the `conn` fixture
        a.commit()

        with contextlib.closing(a.cursor()) as cur:
            cur.execute('SELECT doc_id FROM litcache.crosswalk WHERE external_id = %s', ('doi:race',))
            assert not cur.fetchall()  # no incumbent yet
            uuid_a = str(uuid.uuid4())
            cur.execute('INSERT INTO litcache.crosswalk (external_id, doc_id) VALUES (%s, %s)', ('doi:race', uuid_a))
            # deliberately not committed yet

        b_result: list[crosswalk.MintResult] = []
        b_thread = threading.Thread(target=lambda: b_result.append(crosswalk.mint(b, ['doi:race'])))
        b_thread.start()
        time.sleep(0.3)  # let B reach (and block on) its INSERT before A commits
        a.commit()
        b_thread.join(timeout=15)

        assert not b_thread.is_alive()
        assert b_result[0].doc_id == uuid_a
        assert b_result[0].minted is False

        assert _doc_ids(verify) == {'doi:race': uuid_a}  # exactly one uuid survives
