"""Tests for removing the retired entity fields from the stored routing rows.

The payloads are built as raw wire bytes, because the fields under test are ones today's
`RoutingAssessment` no longer declares: a message built from the stubs cannot carry them.
"""

from __future__ import annotations

import contextlib
import dataclasses
import pathlib
from collections.abc import Iterator, Mapping

import pg8000.dbapi
import pytest
import testcontainers.postgres

from themis.curation import routing_backfill
from themis.curation.models import curation_pb2
from themis.migrate import cloudsql, migrate

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / 'migrate' / 'migrations'
# GRANT needs its grantee to exist; in the deploy these are Cloud SQL IAM users.
_LOGINS: Mapping[str, str] = dict.fromkeys(
    ('AUTH_DB_USER', 'WEB_DB_USER', 'INGEST_DB_USER', 'EVIDENCE_DB_USER'), 'seed@example.iam'
)

# Field 2 of Assessment is `routing`, field 3 `verdict`; both length-delimited.
_ROUTING_TAG = 0x12
_VERDICT_TAG = 0x1A
# RoutingAssessment: 1 the retired mode (varint), 2 the consequence class (varint), 3 and 4 the
# retired entity and MONDO copies (length-delimited), 5 the rationale (length-delimited).
_CONSEQUENCE_MISSENSE = bytes([0x10, 1])
_RATIONALE = bytes([0x2A, 3, *b'why'])

_X_LINKED = 3
_Y_LINKED = 4


def _text(tag: int, value: bytes) -> bytes:
    return bytes([tag, len(value), *value])


def _routing(mode: int | None, *, entity: bool = True) -> bytes:
    """A routing row as a worksheet wrote it before the three fields retired."""
    body = b''
    if mode is not None:
        body += bytes([0x08, mode])
    body += _CONSEQUENCE_MISSENSE
    if entity:
        body += _text(0x1A, b'Marfan syndrome') + _text(0x22, b'MONDO:0007947')
    body += _RATIONALE
    return _text(_ROUTING_TAG, body)


_CURRENT = _text(_ROUTING_TAG, _CONSEQUENCE_MISSENSE + _RATIONALE)


def test_the_retired_fields_go_and_the_curators_answer_stays() -> None:
    stripped = routing_backfill.strip(_routing(1), 'AUTOSOMAL_DOMINANT')
    assert stripped == _CURRENT
    routing = curation_pb2.Assessment.FromString(stripped).routing
    assert (routing.consequence_class, routing.rationale) == (1, 'why')


def test_a_row_without_the_retired_fields_is_left_as_it_is() -> None:
    assert routing_backfill.strip(_CURRENT, 'AUTOSOMAL_DOMINANT') == _CURRENT


@pytest.mark.parametrize('registered', ['X_LINKED_DOMINANT', 'X_LINKED_RECESSIVE'])
def test_the_one_x_linked_mode_agrees_with_either_kind(registered: str) -> None:
    assert routing_backfill.strip(_routing(_X_LINKED), registered) == _CURRENT


def test_a_routing_that_stated_no_mode_contradicts_no_registration() -> None:
    assert routing_backfill.strip(_routing(None), 'SEMIDOMINANT') == _CURRENT


@pytest.mark.parametrize(
    ('mode', 'registered'),
    [(2, 'AUTOSOMAL_DOMINANT'), (_X_LINKED, 'AUTOSOMAL_RECESSIVE'), (_Y_LINKED, 'X_LINKED_RECESSIVE')],
)
def test_a_stated_mode_the_registration_contradicts_is_refused(mode: int, registered: str) -> None:
    with pytest.raises(routing_backfill.BackfillError, match=f'registered {registered}'):
        routing_backfill.strip(_routing(mode), registered)


def test_a_mode_the_retired_enum_never_named_is_refused() -> None:
    with pytest.raises(routing_backfill.BackfillError, match='never named'):
        routing_backfill.strip(_routing(9), 'AUTOSOMAL_DOMINANT')


def test_a_field_no_revision_declared_is_refused_rather_than_dropped() -> None:
    with pytest.raises(routing_backfill.BackfillError, match='field 9'):
        routing_backfill.strip(_text(_ROUTING_TAG, _CONSEQUENCE_MISSENSE + bytes([0x48, 1])), 'AUTOSOMAL_DOMINANT')


def test_a_routing_row_holding_another_section_is_refused() -> None:
    with pytest.raises(routing_backfill.BackfillError, match="carries 'verdict'"):
        routing_backfill.strip(_text(_VERDICT_TAG, bytes([0x08, 1])), 'AUTOSOMAL_DOMINANT')


@dataclasses.dataclass(frozen=True)
class _Database:
    conn: pg8000.dbapi.Connection

    def register(self, variant: str, mode: str) -> None:
        """A variant with one curator, who has a draft and a submission holding `routing`."""
        with contextlib.closing(self.conn.cursor()) as cursor:
            cursor.execute(
                'INSERT INTO curation.variants (id, gene, transcript, hgvs_c, disease_label, mondo_id,'
                " mode_of_inheritance, created_by) VALUES (%s, 'FBN1', 'NM_000138.5', %s, 'Marfan syndrome',"
                " 'MONDO:0007947', %s, 'm@example.org')",
                (variant, f'c.{variant}', mode),
            )
            cursor.execute(
                'INSERT INTO curation.worksheets (id, variant_id, curator_email, workflows_version, assigned_by)'
                " VALUES (%s, %s, 'c@example.org', '1', 'm@example.org')",
                (f'w-{variant}', variant),
            )
            cursor.execute(
                'INSERT INTO curation.submissions (id, worksheet_id) VALUES (%s, %s)', (f's-{variant}', f'w-{variant}')
            )
        self.conn.commit()

    def store(self, variant: str, workflow_id: str, payload: bytes) -> None:
        with contextlib.closing(self.conn.cursor()) as cursor:
            cursor.execute(
                'INSERT INTO curation.drafts (worksheet_id, workflow_id, assessment, updated_at)'
                " VALUES (%s, %s, %s, '2026-08-01T00:00:00Z')",
                (f'w-{variant}', workflow_id, payload),
            )
            cursor.execute(
                'INSERT INTO curation.assessments (submission_id, workflow_id, assessment) VALUES (%s, %s, %s)',
                (f's-{variant}', workflow_id, payload),
            )
        self.conn.commit()

    def stored(self) -> set[tuple[str, str, bytes]]:
        with contextlib.closing(self.conn.cursor()) as cursor:
            cursor.execute(
                "SELECT 'drafts', workflow_id, assessment FROM curation.drafts"
                " UNION ALL SELECT 'assessments', workflow_id, assessment FROM curation.assessments"
            )
            return {(str(row[0]), str(row[1]), bytes(row[2])) for row in cursor.fetchall()}


@pytest.fixture
def database(docker_daemon: None) -> Iterator[_Database]:
    del docker_daemon  # gate on a reachable Docker daemon (shared fixture)
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
            migrate.run(migrate.discover(_MIGRATIONS_DIR), cloudsql.CloudSqlLedger(conn), substitutions=_LOGINS)
            yield _Database(conn)
        finally:
            conn.close()


_VERDICT = _text(_VERDICT_TAG, bytes([0x08, 1]))


def test_every_stored_routing_row_is_rewritten_once(database: _Database) -> None:
    database.register('dominant', 'AUTOSOMAL_DOMINANT')
    database.store('dominant', 'routing', _routing(1))
    database.store('dominant', 'verdict', _VERDICT)
    database.register('recessive', 'X_LINKED_RECESSIVE')
    database.store('recessive', 'routing', _routing(_X_LINKED))

    assert routing_backfill.run(database.conn, write=True) == 0

    assert database.stored() == {(tier, 'routing', _CURRENT) for tier in ('drafts', 'assessments')} | {
        (tier, 'verdict', _VERDICT) for tier in ('drafts', 'assessments')
    }
    with contextlib.closing(database.conn.cursor()) as cursor:
        cursor.execute("SELECT DISTINCT updated_at::text FROM curation.drafts WHERE workflow_id = 'routing'")
        assert [list(row) for row in cursor.fetchall()] == [['2026-08-01 00:00:00+00']]  # not a save
    rows, findings = routing_backfill.plan(database.conn)
    assert (findings, [row for row in rows if row.changed]) == ([], [])  # a second run writes nothing


def test_a_contradicted_registration_stops_the_run_with_nothing_written(database: _Database) -> None:
    database.register('agrees', 'AUTOSOMAL_DOMINANT')
    database.store('agrees', 'routing', _routing(1))
    database.register('contradicts', 'AUTOSOMAL_DOMINANT')
    database.store('contradicts', 'routing', _routing(2))
    before = database.stored()

    with pytest.raises(SystemExit, match='2 routing row'):
        routing_backfill.run(database.conn, write=True)

    assert database.stored() == before


def test_a_row_saved_since_it_was_read_rolls_the_run_back(database: _Database) -> None:
    database.register('dominant', 'AUTOSOMAL_DOMINANT')
    database.store('dominant', 'routing', _routing(1))
    rows, _ = routing_backfill.plan(database.conn)
    with contextlib.closing(database.conn.cursor()) as cursor:
        cursor.execute(
            "UPDATE curation.drafts SET assessment = %s WHERE workflow_id = 'routing'", (_routing(1, entity=False),)
        )
    database.conn.commit()

    assert routing_backfill.apply(database.conn, rows) == [
        "curation.drafts ('w-dominant', 'routing') is gone, or was saved since it was read"
    ]
    assert ('assessments', 'routing', _routing(1)) in database.stored()  # rolled back with it
