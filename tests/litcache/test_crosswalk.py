"""Tests for `litcache.crosswalk` against a throwaway Postgres.

The `UNIQUE`-in-one-transaction mint semantics are the thing under test, so
these run against a real Postgres (testcontainers), never a mock. Gated on a
reachable Docker daemon via the shared `docker_daemon` fixture: an absent, down,
or asleep daemon skips them rather than hanging the suite.
"""

from __future__ import annotations

import contextlib
import pathlib
import threading
import time
import uuid
from collections.abc import Callable

import pg8000.dbapi
import pytest

from themis.litcache import crosswalk
from themis.migrate import migrate

_MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / 'themis' / 'migrate' / 'migrations'


def _doc_ids(conn: pg8000.dbapi.Connection) -> dict[str, str]:
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('SELECT external_id, doc_id FROM litcache.crosswalk')
        return dict(cur.fetchall())


def _apply_case_fold_migration(conn: pg8000.dbapi.Connection) -> None:
    sql = next(m.sql for m in migrate.discover(_MIGRATIONS) if m.name == 'litcache_crosswalk_case_fold')
    with contextlib.closing(conn.cursor()) as cur:
        for statement in migrate.split_statements(migrate.render(sql, {})):
            cur.execute(statement)
    conn.commit()


def _insert_raw(conn: pg8000.dbapi.Connection, external_id: str, doc_id: str) -> None:
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('INSERT INTO litcache.crosswalk (external_id, doc_id) VALUES (%s, %s)', (external_id, doc_id))
    conn.commit()


def test_mint_treats_case_variants_as_one_claim(conn: pg8000.dbapi.Connection) -> None:
    # Two spellings must not take two doc_ids, or the same paper exists twice in the corpus.
    first = crosswalk.mint(conn, ['doi:10.1/AbC'])
    second = crosswalk.mint(conn, ['doi:10.1/abc'])
    assert second.doc_id == first.doc_id
    assert second.minted is False
    assert _doc_ids(conn) == {'doi:10.1/abc': first.doc_id}  # one row, folded


def test_normalise_key_folds_the_case_insensitive_schemes() -> None:
    assert crosswalk.normalise_key('doi:10.1/AbC') == 'doi:10.1/abc'
    assert crosswalk.normalise_key('pmcid:pmc99') == 'pmcid:PMC99'


def test_normalise_key_leaves_the_other_schemes_alone() -> None:
    # pmid/binhash carry no case; pii and the preprint schemes have no specified rule.
    assert crosswalk.normalise_key('pmid:12345') == 'pmid:12345'
    assert crosswalk.normalise_key('pii:S0140AbC') == 'pii:S0140AbC'
    assert crosswalk.normalise_key('not-a-key') == 'not-a-key'


@pytest.mark.parametrize(
    'external_id',
    ['doi:10.1/AbC', 'doi:10.1/İstanbul', 'pmcid:pmc99'],
)
def test_the_migration_folds_exactly_as_normalise_key_does(conn: pg8000.dbapi.Connection, external_id: str) -> None:
    # A spelling the two folds disagree on is a row nothing can reach; U+0130 is where they would.
    doc_id = str(uuid.uuid4())
    _insert_raw(conn, external_id, doc_id)

    _apply_case_fold_migration(conn)

    assert _doc_ids(conn) == {crosswalk.normalise_key(external_id): doc_id}


@pytest.mark.parametrize(
    'spellings',
    [
        ('doi:10.1/AbC', 'doi:10.1/abc'),  # one already canonical
        ('doi:10.1/AbC', 'doi:10.1/ABC'),  # neither is
        ('pmcid:pmc9', 'pmcid:Pmc9'),
    ],
)
def test_the_migration_collapses_spellings_of_one_paper(
    conn: pg8000.dbapi.Connection, spellings: tuple[str, str]
) -> None:
    # Two spellings of one paper share a doc_id, so folding one onto the other hits the primary key.
    doc_id = str(uuid.uuid4())
    for spelling in spellings:
        _insert_raw(conn, spelling, doc_id)

    _apply_case_fold_migration(conn)

    assert _doc_ids(conn) == {crosswalk.normalise_key(spellings[0]): doc_id}


def test_the_migration_aborts_when_two_spellings_name_different_papers(
    conn: pg8000.dbapi.Connection,
) -> None:
    # One identifier claimed by two doc_ids: dropping either mapping loses a paper, so this aborts.
    _insert_raw(conn, 'doi:10.1/AbC', str(uuid.uuid4()))
    _insert_raw(conn, 'doi:10.1/abc', str(uuid.uuid4()))

    with pytest.raises(pg8000.dbapi.DatabaseError):
        _apply_case_fold_migration(conn)


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
