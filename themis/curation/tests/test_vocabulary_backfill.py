"""Tests for the curation vocabulary rewrite.

The payloads are built as raw protobuf wire bytes rather than through the generated `Assessment`,
because the numbering under test is one no descriptor in the tree carries any more: the retired
enums went with the contract change, so a message built from today's stubs cannot express a row
written before it. The tag bytes are the only thing the two encodings share, and they are what makes
the old bytes readable at all.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import pathlib
from collections.abc import Iterator, Mapping

import pg8000.dbapi
import pytest
import testcontainers.postgres

from themis.curation import vocabulary_backfill
from themis.curation.models import curation_pb2
from themis.migrate import cloudsql, migrate

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / 'migrate' / 'migrations'
_VOCABULARY_MIGRATION = 'curation_vocabulary'
# GRANT needs its grantee to exist; in the deploy these are Cloud SQL IAM users.
_DB_USERS = ('AUTH_DB_USER', 'WEB_DB_USER', 'INGEST_DB_USER', 'EVIDENCE_DB_USER')
_LOGINS: Mapping[str, str] = dict.fromkeys(_DB_USERS, 'seed@example.iam')

# Field 2 of Assessment is `routing`, field 3 `verdict`; both are length-delimited (wire type 2).
_ROUTING_TAG = 0x12
_VERDICT_TAG = 0x1A
# Inside a section: field 1 then field 2, both varint (wire type 0).
_FIRST = 0x08
_SECOND = 0x10


def _section(tag: int, body: bytes) -> bytes:
    """An Assessment holding one section. Every length here is one byte; the bodies are tiny."""
    assert len(body) < 128
    return bytes([tag, len(body), *body])


def _routing(inheritance: int, consequence_class: int, extra: bytes = b'') -> bytes:
    """A routing row as the retired contract wrote it, plus any trailing bytes to carry through."""
    assert inheritance < 128  # one-byte varints
    assert consequence_class < 128
    return _section(_ROUTING_TAG, bytes([_FIRST, inheritance, _SECOND, consequence_class]) + extra)


def _verdict(classification: int) -> bytes:
    assert classification < 128
    return _section(_VERDICT_TAG, bytes([_FIRST, classification]))


def test_a_routing_row_keeps_the_members_it_was_written_with() -> None:
    # 3 was SEMIDOMINANT and 4 INFRAME_INDEL; under the shared enums they are 6 and 7.
    rewritten = vocabulary_backfill.remap(_routing(3, 4), 'routing')
    assessment = curation_pb2.Assessment()
    assessment.ParseFromString(rewritten)
    assert [
        (vocabulary.field, vocabulary.name_now(number))
        for vocabulary, number in vocabulary_backfill.members(assessment, 'routing')
    ] == [('inheritance', 'SEMIDOMINANT'), ('consequence_class', 'INFRAME_INDEL')]


@pytest.mark.parametrize('number', sorted(vocabulary_backfill.CLASSIFICATION.was))
def test_a_classification_keeps_its_number(number: int) -> None:
    """The class ladder moved package without renumbering, so a verdict row already reads correctly."""
    assert vocabulary_backfill.CLASSIFICATION.renumber(number) == number


@pytest.mark.parametrize('number', sorted(n for n in vocabulary_backfill.CLASSIFICATION.was if n))
def test_a_verdict_row_comes_back_byte_for_byte(number: int) -> None:
    """The zero member is excluded because proto3 elides a default scalar, which no writer emits."""
    assert vocabulary_backfill.remap(_verdict(number), 'verdict') == _verdict(number)


@pytest.mark.parametrize(
    'vocabulary',
    [vocabulary_backfill.INHERITANCE, vocabulary_backfill.CONSEQUENCE, vocabulary_backfill.CLASSIFICATION],
    ids=lambda v: v.field,
)
def test_every_retired_member_is_a_member_of_the_shared_enum(vocabulary: vocabulary_backfill.Vocabulary) -> None:
    """The rewrite rests on the retired vocabularies being name-for-name subsets of the shared ones."""
    declared = set(vocabulary.now.values_by_name)
    assert {f'{vocabulary.prefix}{name}' for name in vocabulary.was.values()} <= declared


def test_a_number_the_retired_enum_never_named_is_refused() -> None:
    with pytest.raises(vocabulary_backfill.BackfillError, match='which the retired enum never named'):
        vocabulary_backfill.remap(_routing(9, 1), 'routing')


def test_a_row_holding_the_wrong_section_is_refused() -> None:
    with pytest.raises(vocabulary_backfill.BackfillError, match="a 'verdict' row carries 'routing'"):
        vocabulary_backfill.remap(_routing(1, 1), 'verdict')


@pytest.mark.parametrize(
    ('payload', 'workflow_id'),
    [
        (_section(0x22, bytes([0x0A, 2, ord('h'), ord('i')])), 'case'),  # Assessment field 4, CaseAssessment field 1
        (_section(0x0A, bytes([_FIRST, 1])), 'PVS1'),  # a WorkflowAssessment, under an id this module never names
    ],
    ids=['case narrative', 'workflow'],
)
def test_a_row_carrying_no_vocabulary_is_returned_untouched(payload: bytes, workflow_id: str) -> None:
    assert vocabulary_backfill.remap(payload, workflow_id) == payload


@pytest.mark.parametrize(
    ('payload', 'kind'),
    [(_routing(3, 4), 'routing'), (_verdict(2), 'verdict')],
    ids=['routing', 'verdict'],
)
def test_a_vocabulary_section_under_another_workflow_id_fails_the_precondition(payload: bytes, kind: str) -> None:
    """Passed through, such a row would keep the retired numbering and verify clean; the ids have to agree both ways."""
    with pytest.raises(
        vocabulary_backfill.BackfillError, match=f"a 'PVS1' row carries '{kind}', which is stored under"
    ):
        vocabulary_backfill.remap(payload, 'PVS1')


def test_a_field_the_message_does_not_model_survives_the_rewrite() -> None:
    """Binary proto's unknown-field set is what makes this rewrite safe on a row a newer writer wrote."""
    unknown = bytes([0x78, 42])  # field 15, varint — no RoutingAssessment field claims it
    rewritten = vocabulary_backfill.remap(_routing(1, 1, extra=unknown), 'routing')
    assert unknown in rewritten


def test_a_closed_at_without_a_utc_offset_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """Bound against TIMESTAMPTZ a naive instant reads as UTC, hours off an operator's wall clock."""
    with pytest.raises(SystemExit) as stop:
        vocabulary_backfill.argument_parser().parse_args(['rewrite', '--apply', '--closed-at', '2026-09-03T08:00:00'])
    assert stop.value.code == 2
    assert 'carries no UTC offset' in capsys.readouterr().err


def test_a_closed_at_with_a_utc_offset_is_the_instant_it_names() -> None:
    args = vocabulary_backfill.argument_parser().parse_args(
        ['rewrite', '--apply', '--closed-at', '2026-09-03T18:00:00+10:00']
    )
    assert args.closed_at == datetime.datetime(2026, 9, 3, 8, tzinfo=datetime.UTC)


def test_the_migration_snapshots_every_table_the_rewrite_reads(curation_db: pg8000.dbapi.Connection) -> None:
    """The rewrite reads a table name the migration has to have created; nothing else pairs the two."""
    with contextlib.closing(curation_db.cursor()) as cursor:
        for tier in vocabulary_backfill.TIERS:
            cursor.execute(f'SELECT count(*) FROM {tier.snapshot}')  # noqa: S608 — module constants
            assert cursor.fetchall()[0][0] == len(_SEEDED)


@dataclasses.dataclass(frozen=True)
class _SeededDatabase:
    """A throwaway Postgres holding rows in the retired numbering, with `0012_curation_vocabulary` still to run."""

    conn: pg8000.dbapi.Connection
    ledger: cloudsql.CloudSqlLedger

    def migrate(self) -> None:
        """Apply what the ledger has not recorded — `0012`, which snapshots what has been written so far."""
        migrate.run(migrate.discover(_MIGRATIONS_DIR), self.ledger, substitutions=_LOGINS)


@pytest.fixture
def seeded_db(docker_daemon: None) -> Iterator[_SeededDatabase]:
    """Rows written before `0012_curation_vocabulary`, which has not run yet.

    The seed goes in ahead of `0012` because the snapshots the rewrite reads are taken by `0012`
    itself: seeding after it would leave the snapshot empty and every test on it vacuous.
    """
    del docker_daemon  # gate on a reachable Docker daemon (shared fixture)
    migrations = migrate.discover(_MIGRATIONS_DIR)
    vocabulary = next(m for m in migrations if m.name == _VOCABULARY_MIGRATION)
    with testcontainers.postgres.PostgresContainer('postgres:16-alpine') as postgres:
        conn = pg8000.dbapi.connect(
            user=postgres.username,
            password=postgres.password,
            host=postgres.get_container_host_ip(),
            port=int(postgres.get_exposed_port(5432)),
            database=postgres.dbname,
        )
        try:
            with contextlib.closing(conn.cursor()) as cursor:
                cursor.execute('CREATE ROLE "seed@example.iam"')
            conn.commit()
            ledger = cloudsql.CloudSqlLedger(conn)
            migrate.run([m for m in migrations if m.version < vocabulary.version], ledger, substitutions=_LOGINS)
            _seed(conn)
            yield _SeededDatabase(conn, ledger)
        finally:
            conn.close()


@pytest.fixture
def curation_db(seeded_db: _SeededDatabase) -> pg8000.dbapi.Connection:
    """`seeded_db` once `0012_curation_vocabulary` has taken its snapshots of the seeded rows."""
    seeded_db.migrate()
    return seeded_db.conn


def _seed(conn: pg8000.dbapi.Connection) -> None:
    """One worksheet's drafts and one submission's assessments, in the retired numbering."""
    with contextlib.closing(conn.cursor()) as cursor:
        cursor.execute(
            'INSERT INTO curation.variants (id, gene, transcript, hgvs_c, disease_label, inheritance, created_by)'
            " VALUES ('v', 'FBN1', 'NM_000138.5', 'c.7003C>T', 'Marfan syndrome', 'SEMIDOMINANT', 'm@example.org')"
        )
        cursor.execute(
            'INSERT INTO curation.worksheets (id, variant_id, curator_email, workflows_version, assigned_by)'
            " VALUES ('w', 'v', 'c@example.org', '1', 'm@example.org')"
        )
        cursor.execute("INSERT INTO curation.submissions (id, worksheet_id) VALUES ('s', 'w')")
        for workflow_id, payload in _SEEDED:
            cursor.execute(
                'INSERT INTO curation.drafts (worksheet_id, workflow_id, assessment) VALUES (%s, %s, %s)',
                ('w', workflow_id, payload),
            )
            cursor.execute(
                'INSERT INTO curation.assessments (submission_id, workflow_id, assessment) VALUES (%s, %s, %s)',
                ('s', workflow_id, payload),
            )
    conn.commit()


_SEEDED = (
    ('routing', _routing(3, 4)),  # SEMIDOMINANT, INFRAME_INDEL — both renumber
    ('verdict', _verdict(2)),  # LIKELY_PATHOGENIC — keeps its number
    ('POP_FRQ', _section(0x0A, bytes([_FIRST, 1]))),  # a WorkflowAssessment; no vocabulary of ours
)


def test_verification_fails_until_the_rewrite_is_applied(curation_db: pg8000.dbapi.Connection) -> None:
    findings = vocabulary_backfill.verify(curation_db)
    assert [f for f in findings if 'routing' in f and 'renumbers to' in f] == findings
    assert len(findings) == len(vocabulary_backfill.TIERS)


def test_the_applied_rewrite_verifies_and_is_re_runnable(curation_db: pg8000.dbapi.Connection) -> None:
    rows = vocabulary_backfill.plan(curation_db)
    assert vocabulary_backfill.apply(curation_db, rows) == []
    assert vocabulary_backfill.verify(curation_db) == []
    # A second run recomputes from the snapshot, so it cannot map already-shared numbering twice.
    assert vocabulary_backfill.apply(curation_db, vocabulary_backfill.plan(curation_db)) == []
    assert vocabulary_backfill.verify(curation_db) == []


def test_a_row_written_since_the_snapshot_is_reported_rather_than_overwritten(
    curation_db: pg8000.dbapi.Connection,
) -> None:
    """Nobody should be writing while the surface is closed; an older reading must not win if they do."""
    written_since = _routing(1, 1)
    with contextlib.closing(curation_db.cursor()) as cursor:
        cursor.execute(
            'UPDATE curation.drafts SET assessment = %s WHERE worksheet_id = %s AND workflow_id = %s',
            (written_since, 'w', 'routing'),
        )
    curation_db.commit()
    assert vocabulary_backfill.apply(curation_db, vocabulary_backfill.plan(curation_db)) == [
        "curation.drafts ('w', 'routing') is gone, or was written since the snapshot"
    ]
    with contextlib.closing(curation_db.cursor()) as cursor:
        cursor.execute("SELECT assessment FROM curation.drafts WHERE worksheet_id = 'w' AND workflow_id = 'routing'")
        assert bytes(cursor.fetchall()[0][0]) == written_since


def _save_leaving_updated_at_out(conn: pg8000.dbapi.Connection, payload: bytes) -> None:
    """The routing draft saved again by an upsert that does not name `updated_at`."""
    with contextlib.closing(conn.cursor()) as cursor:
        cursor.execute(
            'INSERT INTO curation.drafts (worksheet_id, workflow_id, assessment) VALUES (%s, %s, %s)'
            ' ON CONFLICT (worksheet_id, workflow_id) DO UPDATE SET assessment = EXCLUDED.assessment',
            ('w', 'routing', payload),
        )
    conn.commit()


def test_a_snapshot_taken_after_the_trigger_dates_a_draft_saved_without_naming_updated_at(
    curation_db: pg8000.dbapi.Connection,
) -> None:
    """What a later migration's snapshot gets: the schema dates the write, whichever columns the writer names."""
    closed_at = _clock(curation_db)
    _save_leaving_updated_at_out(curation_db, _routing(1, 1))
    # A later vocabulary migration would snapshot the live table after such a write; take that snapshot again.
    with contextlib.closing(curation_db.cursor()) as cursor:
        cursor.execute('DROP TABLE curation.drafts_v1')
        cursor.execute('CREATE TABLE curation.drafts_v1 AS SELECT * FROM curation.drafts')
    curation_db.commit()
    assert vocabulary_backfill.written_since(curation_db, closed_at) == [
        f"curation.drafts ('w', 'routing') was written at or after {closed_at.isoformat()}"
    ]


def test_the_migrations_own_snapshot_dates_such_a_draft_as_its_writer_left_it(seeded_db: _SeededDatabase) -> None:
    """What `0012`'s own snapshot gets: the trigger is created after it, so the writer's convention is the guard.

    A draft saved in the window by an upsert that leaves `updated_at` out keeps its insert time, and the
    guard misses it. This run rests on the deployed writer naming the column, which the surface's upsert
    does.
    """
    closed_at = _clock(seeded_db.conn)
    _save_leaving_updated_at_out(seeded_db.conn, _routing(1, 1))
    seeded_db.migrate()
    with contextlib.closing(seeded_db.conn.cursor()) as cursor:
        cursor.execute("SELECT assessment FROM curation.drafts_v1 WHERE worksheet_id = 'w' AND workflow_id = 'routing'")
        assert bytes(cursor.fetchall()[0][0]) == _routing(1, 1)  # the snapshot holds the window's write...
    assert vocabulary_backfill.written_since(seeded_db.conn, closed_at) == []  # ...and cannot date it


def test_the_rewrite_leaves_when_each_draft_was_last_saved_alone(curation_db: pg8000.dbapi.Connection) -> None:
    """The rewrite changes bytes, not what the curator did; stamped, every draft would date from the rewrite."""
    before = _last_saved(curation_db)
    assert vocabulary_backfill.apply(curation_db, vocabulary_backfill.plan(curation_db)) == []
    assert _last_saved(curation_db) == before


def _clock(conn: pg8000.dbapi.Connection) -> datetime.datetime:
    """The database's own clock, so a cutoff and the stamps it is compared with come from one source."""
    with contextlib.closing(conn.cursor()) as cursor:
        cursor.execute('SELECT now()')
        (now,) = cursor.fetchall()[0]
    conn.commit()  # `now()` is the transaction's start; end it so a later write stamps no earlier
    return now


def _last_saved(conn: pg8000.dbapi.Connection) -> dict[tuple[str, str], datetime.datetime]:
    with contextlib.closing(conn.cursor()) as cursor:
        cursor.execute('SELECT worksheet_id, workflow_id, updated_at FROM curation.drafts')
        return {(str(row[0]), str(row[1])): row[2] for row in cursor.fetchall()}


def test_nothing_is_flagged_as_written_after_a_cutoff_in_the_future(curation_db: pg8000.dbapi.Connection) -> None:
    assert vocabulary_backfill.written_since(curation_db, datetime.datetime(2999, 1, 1, tzinfo=datetime.UTC)) == []


def test_verification_sees_a_change_the_member_names_do_not_show(curation_db: pg8000.dbapi.Connection) -> None:
    """Comparing decoded names alone would call an edited rationale clean."""
    assert vocabulary_backfill.apply(curation_db, vocabulary_backfill.plan(curation_db)) == []
    routing = curation_pb2.Assessment()
    routing.ParseFromString(vocabulary_backfill.remap(_routing(3, 4), 'routing'))
    routing.routing.rationale = 'edited out from under the curator'
    with contextlib.closing(curation_db.cursor()) as cursor:
        cursor.execute(
            'UPDATE curation.drafts SET assessment = %s WHERE worksheet_id = %s AND workflow_id = %s',
            (routing.SerializeToString(), 'w', 'routing'),
        )
    curation_db.commit()
    assert vocabulary_backfill.verify(curation_db) == [
        "curation.drafts ('w', 'routing') is not what its snapshot renumbers to "
        "([('inheritance', 'SEMIDOMINANT'), ('consequence_class', 'INFRAME_INDEL')])"
    ]


def test_the_rewrite_moves_only_the_rows_whose_numbers_moved(curation_db: pg8000.dbapi.Connection) -> None:
    """The seeded verdict row carries a vocabulary too; the class ladder just kept its numbers."""
    changed = {(row.tier.table, row.workflow_id) for row in vocabulary_backfill.plan(curation_db) if row.changed}
    assert changed == {(tier.table, 'routing') for tier in vocabulary_backfill.TIERS}
