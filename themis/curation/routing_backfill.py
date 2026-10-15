"""Remove the retired entity fields from the stored routing assessments.

`RoutingAssessment` used to carry the mode of inheritance the curator stated (field 1) and copies of
the registered disease label and MONDO term (fields 3 and 4). The registration states the question,
the entity's mode included, so the three fields are retired with their numbers and names reserved.
An assessment is a serialized proto in a `bytea`, so every routing row written before still carries
them, as fields today's stubs read as unknown, and no SQL statement can reach in to remove them.

This removes them, once, by hand, straight after the deploy that ships the surface without them.
That deploy rolls the surface first and applies the migrations after, and the registered mode this
reads arrives with `0014_curation_registration_mode`, so nothing of this can run before the deploy.
The run is `--apply`; without the flag it reports and writes nothing. The new surface saves a
routing draft without the fields, so a contradicted mode in a draft saved over between the deploy
and the run escapes the check below; running straight after the deploy keeps that window short.
Before it writes anything, it holds each stored mode against the mode the variant is registered
under. A routing whose mode differs from the registered one records a curator who answered a
different question, so the run stops on it, naming the rows, rather than erase the only record of
that. The manager then settles which question that answer is for: a registration typed with the
wrong mode is corrected in `curation.variants` by hand, and an answer to another question is removed
by hand or kept by registering that question and moving the worksheet to it, before the run is
repeated.

Run it as `themis-clu`: it inherits the migrator role, which owns the curation tables, and a table's
owner bypasses the GRANTs, while `curation.assessments` carries no `UPDATE` grant for anybody.
`tools/psql.py` documents that identity and what reaching the instance takes. Point it at the
instance with the environment the migration runner already reads:

    export THEMIS_SQL_CONNECTION_NAME=... THEMIS_SQL_DATABASE=... THEMIS_DB_USER=...
    uv run --group curation python -m themis.curation.routing_backfill
    uv run --group curation python -m themis.curation.routing_backfill --apply
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import dataclasses
import sys
from collections.abc import Iterable, Mapping, Sequence

from google.cloud.sql import connector
from google.protobuf import message, unknown_fields

from themis.common import sql
from themis.curation.models import curation_pb2
from themis.migrate import config

_ROUTING = 'routing'
_MODE = 1
_RETIRED = frozenset({_MODE, 3, 4})
_VARINT = 0

# What a stored mode number named, in the numbering of the coarse inheritance enum field 1 was typed
# with, which no descriptor in the tree declares any more. Each maps to the registered modes it agrees
# with, as `curation.variants` spells them. Its one X-linked member says nothing about dominance, so
# it agrees with either kind. Zero is a routing that stated no mode, which contradicts no
# registration. Y-linked, mitochondrial and undetermined name no mode a registration can hold, so
# they agree with none.
_AGREES_WITH: Mapping[int, frozenset[str]] = {
    1: frozenset({'AUTOSOMAL_DOMINANT'}),
    2: frozenset({'AUTOSOMAL_RECESSIVE'}),
    3: frozenset({'X_LINKED_DOMINANT', 'X_LINKED_RECESSIVE'}),
    4: frozenset(),
    5: frozenset(),
    6: frozenset({'SEMIDOMINANT'}),
    7: frozenset(),
}
_STATED_NAMES: Mapping[int, str] = {
    0: 'none stated',
    1: 'AUTOSOMAL_DOMINANT',
    2: 'AUTOSOMAL_RECESSIVE',
    3: 'X_LINKED',
    4: 'Y_LINKED',
    5: 'MITOCHONDRIAL',
    6: 'SEMIDOMINANT',
    7: 'UNDETERMINED',
}


class BackfillError(Exception):
    """A stored row the rewrite cannot account for."""


@dataclasses.dataclass(frozen=True)
class _Tier:
    """One assessment tier: its table, its row key, and how a row reaches its variant."""

    table: str
    key: tuple[str, str]
    to_variant: str


TIERS = (
    _Tier(
        'curation.drafts',
        ('worksheet_id', 'workflow_id'),
        'JOIN curation.worksheets w ON w.id = t.worksheet_id',
    ),
    _Tier(
        'curation.assessments',
        ('submission_id', 'workflow_id'),
        'JOIN curation.submissions s ON s.id = t.submission_id JOIN curation.worksheets w ON w.id = s.worksheet_id',
    ),
)


def stated_mode(payload: bytes) -> int:
    """The mode number a stored routing row carries in its retired field 1; 0 where it carries none.

    Raises:
        BackfillError: If the payload is not a routing assessment, or carries a field the rewrite
            does not account for.
    """
    routing = _parse(payload).routing
    stated = 0
    for field in unknown_fields.UnknownFieldSet(routing):
        if field.field_number not in _RETIRED:
            raise BackfillError(f'the routing carries field {field.field_number}, which no revision of it declared')
        if field.field_number == _MODE:
            if field.wire_type != _VARINT:
                raise BackfillError(f'the routing carries field {_MODE} with wire type {field.wire_type}')
            stated = int(field.data)
    return stated


def strip(payload: bytes, registered: str) -> bytes:
    """The routing row without its retired fields, once its mode is checked against the registration.

    Args:
        payload: The stored `bytea`.
        registered: The variant's `mode_of_inheritance` column.

    Returns:
        The payload unchanged where it carries no retired field, else re-serialized without them.

    Raises:
        BackfillError: If the stored mode contradicts `registered`, or the row is not one this
            rewrite can read.
    """
    stated = stated_mode(payload)
    if stated not in _AGREES_WITH and stated != 0:
        raise BackfillError(f'the routing states mode {stated}, which the retired enum never named')
    if stated != 0 and registered not in _AGREES_WITH[stated]:
        raise BackfillError(f'the routing states {_STATED_NAMES[stated]}; the variant is registered {registered}')
    assessment = _parse(payload)
    if not unknown_fields.UnknownFieldSet(assessment.routing):
        return payload
    assessment.routing.DiscardUnknownFields()
    return assessment.SerializeToString()


def _parse(payload: bytes) -> curation_pb2.Assessment:
    assessment = curation_pb2.Assessment()
    try:
        assessment.ParseFromString(payload)
    except message.DecodeError as error:
        raise BackfillError(f'the row does not parse as an Assessment: {error}') from error
    kind = assessment.WhichOneof('kind')
    if kind != _ROUTING:
        raise BackfillError(f'a {_ROUTING!r} row carries {kind!r}')
    if unknown_fields.UnknownFieldSet(assessment):
        raise BackfillError('the assessment carries a field no revision of it declared')
    return assessment


@dataclasses.dataclass(frozen=True)
class Row:
    """One stored routing row, the mode its variant is registered under, and what the rewrite makes of it."""

    tier: _Tier
    key: tuple[str, str]
    registered: str
    before: bytes
    after: bytes

    @property
    def changed(self) -> bool:
        return self.before != self.after


def plan(conn: sql.Connection) -> tuple[list[Row], list[str]]:
    """Every stored routing row and its rewrite, and a finding per row the rewrite refuses."""
    rows: list[Row] = []
    findings: list[str] = []
    for tier in TIERS:
        for key, registered, payload in _fetch(conn, tier):
            try:
                rows.append(Row(tier, key, registered, payload, strip(payload, registered)))
            except BackfillError as error:
                findings.append(f'{tier.table} {key}: {error}')
    return rows, findings


def _fetch(conn: sql.Connection, tier: _Tier) -> list[tuple[tuple[str, str], str, bytes]]:
    with contextlib.closing(conn.cursor()) as cursor:
        cursor.execute(
            f'SELECT t.{tier.key[0]}, t.{tier.key[1]}, v.mode_of_inheritance, t.assessment'  # noqa: S608 — module constants
            f' FROM {tier.table} t {tier.to_variant} JOIN curation.variants v ON v.id = w.variant_id'
            ' WHERE t.workflow_id = %s ORDER BY 1, 2',
            (_ROUTING,),
        )
        return [((str(row[0]), str(row[1])), str(row[2]), bytes(row[3])) for row in cursor.fetchall()]


def apply(conn: sql.Connection, rows: Iterable[Row]) -> list[str]:
    """Write each changed row back, in one transaction.

    Each update matches on the stored bytes as well as the key, so a row saved since `plan` read it is
    left alone and reported, and the run rolls back whole rather than leave the tiers half-rewritten.

    The transaction sets `curation.backfill_in_progress`, which the trigger stamping
    `curation.drafts.updated_at` yields to: removing the fields is not the curator saving.

    Returns:
        A finding per row the table no longer holds as `plan` read it, in which case nothing was
        written.
    """
    unmatched: list[str] = []
    with contextlib.closing(conn.cursor()) as cursor:
        cursor.execute("SET LOCAL curation.backfill_in_progress = 'on'")
        for row in rows:
            if not row.changed:
                continue
            first, second = row.tier.key
            cursor.execute(
                f'UPDATE {row.tier.table} SET assessment = %s'  # noqa: S608 — module constants
                f' WHERE {first} = %s AND {second} = %s AND assessment = %s',
                (row.after, *row.key, row.before),
            )
            if cursor.rowcount == 0:
                unmatched.append(f'{row.tier.table} {row.key} is gone, or was saved since it was read')
    if unmatched:
        conn.rollback()
    else:
        conn.commit()
    return unmatched


def census(rows: Iterable[Row]) -> dict[tuple[str, str, str], int]:
    """Rows counted by `(table, mode stated, mode registered)`."""
    counts: collections.Counter[tuple[str, str, str]] = collections.Counter()
    for row in rows:
        counts[(row.tier.table, _STATED_NAMES[stated_mode(row.before)], row.registered)] += 1
    return dict(counts)


def _report(findings: Sequence[str], summary: str) -> None:
    """Print findings and stop, or return so the caller carries on."""
    for finding in findings:
        print(f'backfill: {finding}', file=sys.stderr)
    if findings:
        raise SystemExit(f'{len(findings)} {summary}')


def run(conn: sql.Connection, *, write: bool) -> int:
    """Plan, print the census, and with `write` apply. Stops before writing on any finding."""
    rows, findings = plan(conn)
    _report(findings, 'routing row(s) cannot be rewritten; nothing was written')
    for (table, stated, registered), count in sorted(census(rows).items()):
        print(f'{table:<22} stated {stated:<20} registered {registered:<20} {count} row(s)')
    changed = sum(1 for row in rows if row.changed)
    print(f'{len(rows)} routing row(s) read, {changed} to rewrite')
    if write:
        _report(apply(conn, rows), 'routing row(s) matched nothing to update; nothing was written')
        print(f'backfill: {changed} row(s) rewritten')
    return 0


def _connect(pool: connector.Connector) -> sql.Connection:
    sql_config = config.load_sql_config()
    return sql.iam_connect(
        pool,
        connection_name=sql_config.connection_name,
        database=sql_config.database,
        db_user=sql_config.db_user,
    )


def argument_parser() -> argparse.ArgumentParser:
    """The command line: report by default, `--apply` to write."""
    parser = argparse.ArgumentParser(description='Remove the retired entity fields from the stored routing rows.')
    parser.add_argument('--apply', action='store_true', help='write the rewritten rows back (default: report only)')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = argument_parser().parse_args(argv)
    with contextlib.closing(connector.Connector()) as pool, contextlib.closing(_connect(pool)) as conn:
        return run(conn, write=args.apply)


if __name__ == '__main__':
    sys.exit(main())
