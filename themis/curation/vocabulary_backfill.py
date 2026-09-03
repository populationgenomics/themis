"""Rewrite the stored curation assessments onto the shared framework vocabularies.

`RoutingAssessment.inheritance` and `.consequence_class` and `VerdictAssessment.classification` used
to be enums the curation contract declared for itself; they are now `themis.evidence.models`'
`Inheritance` and `Consequence` and `themis.svcv4.models.Classification`. Every member name carried
over, but two of the three number them differently — and an assessment is a serialized proto in a
`bytea`, so a row written before the change decodes to a member the curator never chose, and no SQL
statement can reach in to correct it.

This is that correction. It runs once, by hand, inside the closed window
`docs/runbooks/curation-vocabulary-deploy.md` sets out, after migration `0012_curation_vocabulary`
has taken the `_v1` snapshots it reads from.

Reading the snapshot rather than the live row is what makes a second `--apply` safe: the snapshot is
never written, so re-running recomputes the same bytes. Reading the live row instead would put
already-shared numbering through the retired table a second time and land on a different member —
`X_LINKED` was 4 and is 3, and 3 used to be `SEMIDOMINANT`.

The same collision is why `--apply` takes `--closed-at`. A row written while the surface was still
open carries the shared numbering already, and the snapshot cannot tell one of those apart from a row
the retired contract wrote; verification cannot either, since it reads both sides through the same
table. So a snapshot row that postdates the close refuses the run rather than being renumbered twice.

The write-back runs with `curation.backfill_in_progress` set for its transaction: `curation.drafts`
stamps `updated_at` on every other UPDATE, and rewriting the bytes is not the curator saving, so the
time they last did stays as it was.

Run it as `themis-clu`: it inherits the migrator role, which owns the curation tables, and a table's
owner bypasses the GRANTs — `curation.assessments` carries no `UPDATE` grant for anybody.
`tools/psql.py` documents that identity and what reaching the instance takes. Point it at the
instance with the environment the migration runner already reads:

    export THEMIS_SQL_CONNECTION_NAME=... THEMIS_SQL_DATABASE=... THEMIS_DB_USER=...
    uv run --group curation python -m themis.curation.vocabulary_backfill rewrite
    uv run --group curation python -m themis.curation.vocabulary_backfill rewrite --apply --closed-at ...
    uv run --group curation python -m themis.curation.vocabulary_backfill verify
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import dataclasses
import datetime
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import cast

from google.cloud.sql import connector
from google.protobuf import descriptor, message

from themis.common import sql
from themis.curation.models import curation_pb2
from themis.evidence.models import evidence_pb2
from themis.migrate import config
from themis.svcv4.models import svcv4_pb2

# The two worksheet-level rows that carry a vocabulary; `apps/web/src/curation/workflows/registry.ts`
# fixes the ids. Every other row a worksheet stores is a WorkflowAssessment or the case narrative,
# whose enums the curation contract still declares, so those rows are left exactly as they are.
_ROUTING = 'routing'
_VERDICT = 'verdict'
# The `Assessment.kind` each of those rows holds, by its workflow id. Nothing in this tree binds the
# ids to the writer's, so a row is held to agreeing with its payload in both directions.
_KIND_OF = {_ROUTING: 'routing', _VERDICT: 'verdict'}


class BackfillError(Exception):
    """A stored row the rewrite cannot account for."""


@dataclasses.dataclass(frozen=True)
class _Tier:
    """One assessment tier, the snapshot `0012_curation_vocabulary` took of it, and its row key."""

    table: str
    snapshot: str
    key: tuple[str, ...]


TIERS = (
    _Tier('curation.drafts', 'curation.drafts_v1', ('worksheet_id', 'workflow_id')),
    _Tier('curation.assessments', 'curation.assessments_v1', ('submission_id', 'workflow_id')),
)

# What each stored number named, as the curation contract declared it at `contracts/curation`
# f03bc569e — the last revision carrying these enums. Their descriptors go with them, so this table
# is the only remaining reading of a number an already-written row holds. Bare names: the prefix
# differs between the retired enum and the shared one (`CONSEQUENCE_CLASS_` against `CONSEQUENCE_`),
# while everything after it is what carried over.
_WAS_INHERITANCE: Mapping[int, str] = {
    0: 'UNSPECIFIED',
    1: 'AUTOSOMAL_DOMINANT',
    2: 'AUTOSOMAL_RECESSIVE',
    3: 'SEMIDOMINANT',
    4: 'X_LINKED',
}
_WAS_CONSEQUENCE: Mapping[int, str] = {
    0: 'UNSPECIFIED',
    1: 'MISSENSE',
    2: 'NONSENSE',
    3: 'FRAMESHIFT',
    4: 'INFRAME_INDEL',
    5: 'CANONICAL_SPLICE',
    6: 'INTRONIC',
    7: 'SYNONYMOUS',
    8: 'EXON_DELETION',
    9: 'EXON_DUPLICATION',
    10: 'START_LOST',
    11: 'STOP_LOST',
}
_WAS_CLASSIFICATION: Mapping[int, str] = {
    0: 'UNSPECIFIED',
    1: 'PATHOGENIC',
    2: 'LIKELY_PATHOGENIC',
    3: 'VUS',
    4: 'LIKELY_BENIGN',
    5: 'BENIGN',
    6: 'NOT_ESTABLISHED',
}


# `Member` is bound to `int` because that is what the generated stubs make every proto enum.
@dataclasses.dataclass(frozen=True)
class Vocabulary[Member: int]:
    """One enum-typed field, under the numbering it was written with and the one it has now.

    The current side is the live descriptor rather than a second literal table: a member the shared
    enum has since dropped or renamed then fails here, instead of being written back as a number
    nothing names.
    """

    field: str
    was: Mapping[int, str]
    now: descriptor.EnumDescriptor
    prefix: str

    def name_written(self, number: int) -> str:
        """The member `number` named when the row was written."""
        try:
            return self.was[number]
        except KeyError as error:
            raise BackfillError(
                f'{self.field} holds {number}, which the retired enum never named; it named {sorted(self.was)}'
            ) from error

    def name_now(self, number: int) -> str:
        """The member `number` names under the shared enum."""
        value = self.now.values_by_number.get(number)
        if value is None:
            raise BackfillError(f'{self.field} holds {number}, which {self.now.full_name} does not name')
        return value.name.removeprefix(self.prefix)

    def renumber(self, number: int) -> Member:
        """The number the shared enum gives the member `number` used to name."""
        name = self.name_written(number)
        value = self.now.values_by_name.get(f'{self.prefix}{name}')
        if value is None:
            raise BackfillError(f'{self.now.full_name} has no member named {self.prefix}{name}')
        return cast('Member', value.number)


INHERITANCE: Vocabulary[evidence_pb2.Inheritance] = Vocabulary(
    'inheritance', _WAS_INHERITANCE, evidence_pb2.Inheritance.DESCRIPTOR, 'INHERITANCE_'
)
CONSEQUENCE: Vocabulary[evidence_pb2.Consequence] = Vocabulary(
    'consequence_class', _WAS_CONSEQUENCE, evidence_pb2.Consequence.DESCRIPTOR, 'CONSEQUENCE_'
)
CLASSIFICATION: Vocabulary[svcv4_pb2.Classification] = Vocabulary(
    'classification', _WAS_CLASSIFICATION, svcv4_pb2.Classification.DESCRIPTOR, 'CLASSIFICATION_'
)


def _routing(assessment: curation_pb2.Assessment) -> curation_pb2.RoutingAssessment:
    _require_kind(assessment, 'routing')
    return assessment.routing


def _verdict(assessment: curation_pb2.Assessment) -> curation_pb2.VerdictAssessment:
    _require_kind(assessment, 'verdict')
    return assessment.verdict


def _require_kind(assessment: curation_pb2.Assessment, expected: str) -> None:
    found = assessment.WhichOneof('kind')
    if found != expected:
        raise BackfillError(f'a {expected!r} row carries {found!r}')


def members(assessment: curation_pb2.Assessment, workflow_id: str) -> list[tuple[Vocabulary[int], int]]:
    """The vocabulary-typed values a row carries; empty for a row carrying none.

    Named field by field rather than reached by attribute name, so the type-checker sees each one and
    a field that goes stops being a silent no-op.

    Raises:
        BackfillError: If the row's `workflow_id` and its payload's section disagree either way. A
            routing or verdict payload under any other id would otherwise pass through under the
            retired numbering, and `verify`, reading both sides through the same table, could not tell.
    """
    if workflow_id == _ROUTING:
        routing = _routing(assessment)
        return [(INHERITANCE, routing.inheritance), (CONSEQUENCE, routing.consequence_class)]
    if workflow_id == _VERDICT:
        return [(CLASSIFICATION, _verdict(assessment).classification)]
    _require_no_vocabulary(assessment, workflow_id)
    return []


def _require_no_vocabulary(assessment: curation_pb2.Assessment, workflow_id: str) -> None:
    """The reverse of `_require_kind`: a section that carries a vocabulary is stored under its own id only."""
    kind = assessment.WhichOneof('kind')
    stored_under = next((wid for wid, k in _KIND_OF.items() if k == kind), None)
    if stored_under is not None:
        raise BackfillError(f'a {workflow_id!r} row carries {kind!r}, which is stored under {stored_under!r}')


def _renumber(assessment: curation_pb2.Assessment, workflow_id: str) -> None:
    """Renumber every vocabulary field in place. The write side of `members`; same three fields."""
    if workflow_id == _ROUTING:
        routing = _routing(assessment)
        routing.inheritance = INHERITANCE.renumber(routing.inheritance)
        routing.consequence_class = CONSEQUENCE.renumber(routing.consequence_class)
    elif workflow_id == _VERDICT:
        verdict = _verdict(assessment)
        verdict.classification = CLASSIFICATION.renumber(verdict.classification)


def parse(payload: bytes, workflow_id: str) -> curation_pb2.Assessment:
    """Parse a stored `bytea`, naming the row in the failure."""
    assessment = curation_pb2.Assessment()
    try:
        assessment.ParseFromString(payload)
    except message.DecodeError as error:
        raise BackfillError(f'a {workflow_id!r} row does not parse as an Assessment: {error}') from error
    return assessment


def remap(payload: bytes, workflow_id: str) -> bytes:
    """Renumber a stored assessment's vocabulary fields onto the shared enums.

    A row carrying no vocabulary comes back as it went in, having been parsed only to establish that
    it is an assessment at all.

    Args:
        payload: The `bytea` as written under the retired numbering.
        workflow_id: The row's `workflow_id` column, which says which section it holds.

    Returns:
        The re-serialized assessment. A field the message does not model is carried through
        untouched, as binary proto's unknown-field set makes it.

    Raises:
        BackfillError: If the payload is not an assessment, does not hold the section its
            `workflow_id` claims, or holds a number the retired enum never named.
    """
    assessment = parse(payload, workflow_id)
    if not members(assessment, workflow_id):
        return payload
    _renumber(assessment, workflow_id)
    return assessment.SerializeToString()


@dataclasses.dataclass(frozen=True)
class Row:
    """One snapshot row and what the rewrite makes of it."""

    tier: _Tier
    key: tuple[str, ...]
    workflow_id: str
    before: bytes
    after: bytes

    @property
    def changed(self) -> bool:
        return self.before != self.after


def _fetch(conn: sql.Connection, table: str, key: Sequence[str]) -> dict[tuple[str, ...], tuple[str, bytes]]:
    """Every row of a tier table or its snapshot, keyed, as `(workflow_id, assessment)`."""
    columns = ', '.join([*key, 'assessment'])
    with contextlib.closing(conn.cursor()) as cursor:
        cursor.execute(f'SELECT {columns} FROM {table}')  # noqa: S608 — table and columns are module constants
        rows = list(cursor.fetchall())
    workflow_column = key.index('workflow_id')
    return {tuple(str(value) for value in row[: len(key)]): (str(row[workflow_column]), bytes(row[-1])) for row in rows}


def plan(conn: sql.Connection) -> list[Row]:
    """What the rewrite would write, computed from the snapshots rather than the live rows."""
    return [
        Row(tier, key, workflow_id, payload, remap(payload, workflow_id))
        for tier in TIERS
        for key, (workflow_id, payload) in sorted(_fetch(conn, tier.snapshot, tier.key).items())
    ]


def apply(conn: sql.Connection, rows: Iterable[Row]) -> list[str]:
    """Write each changed row back to its live table, in one transaction.

    Each update matches on the assessment as well as the key, and accepts either the snapshot's bytes
    or the ones this would write. That is what keeps a re-run a no-op and, more to the point, what
    stops a row written since the snapshot from being silently overwritten by an older reading of
    itself. A run that finds one rolls back whole rather than leaving the tiers half-rewritten.

    The transaction carries `curation.backfill_in_progress`, which the trigger stamping
    `curation.drafts.updated_at` yields to; without it every rewritten draft would date from this run.

    Returns:
        A finding per snapshot row the live table no longer holds as the snapshot has it, in which
        case nothing was written. With the surface closed there should be none.
    """
    unmatched: list[str] = []
    with contextlib.closing(conn.cursor()) as cursor:
        # Transaction-scoped: gone on commit or rollback alike.
        cursor.execute("SET LOCAL curation.backfill_in_progress = 'on'")
        for row in rows:
            if not row.changed:
                continue
            where = ' AND '.join(f'{column} = %s' for column in row.tier.key)
            # The table and the key columns are module constants; only the values are bound.
            statement = f'UPDATE {row.tier.table} SET assessment = %s WHERE {where} AND assessment IN (%s, %s)'  # noqa: S608
            cursor.execute(statement, (row.after, *row.key, row.before, row.after))
            if cursor.rowcount == 0:
                unmatched.append(f'{row.tier.table} {row.key} is gone, or was written since the snapshot')
    if unmatched:
        conn.rollback()
    else:
        conn.commit()
    return unmatched


def histogram(rows: Iterable[Row]) -> dict[tuple[str, str, str, int, int], int]:
    """Count the rows each member accounts for, keyed by `(table, field, member, was, now)`."""
    counts: collections.Counter[tuple[str, str, str, int, int]] = collections.Counter()
    for row in rows:
        written = members(parse(row.before, row.workflow_id), row.workflow_id)
        current = members(parse(row.after, row.workflow_id), row.workflow_id)
        for (vocabulary, was), (_, now) in zip(written, current, strict=True):
            counts[(row.tier.table, vocabulary.field, vocabulary.name_written(was), was, now)] += 1
    return dict(counts)


def verify(conn: sql.Connection) -> list[str]:
    """Check the live tables against their snapshots; an empty result is a clean rewrite.

    Every live row has to be, byte for byte, what the rewrite makes of its snapshot — which for a row
    carrying no vocabulary means the snapshot unchanged. Comparing the bytes rather than the decoded
    member names is what also covers the curator's prose, the entity, and any field a newer writer
    added that this message does not model.

    What it establishes is that the rewrite ran exactly and completely: no row added, none lost, none
    half-written, none touched that should not have been. It cannot establish that the retired
    numbering was read correctly, because it reads it through the same table the rewrite wrote with;
    what holds that is the unit tests and the census `rewrite` prints.
    """
    findings: list[str] = []
    for tier in TIERS:
        live = _fetch(conn, tier.table, tier.key)
        stored = _fetch(conn, tier.snapshot, tier.key)
        if len(live) != len(stored):
            findings.append(f'{tier.table} holds {len(live)} row(s), its snapshot {len(stored)}')
        findings += [f'{tier.table} no longer holds the row {key}' for key in sorted(stored.keys() - live.keys())]
        findings += [
            f'{tier.table} holds the row {key}, which its snapshot does not'
            for key in sorted(live.keys() - stored.keys())
        ]
        for key in sorted(stored.keys() & live.keys()):
            workflow_id, before = stored[key]
            findings += _compare(f'{tier.table} {key}', workflow_id, before, live[key][1])
    return findings


def _compare(where: str, workflow_id: str, before: bytes, after: bytes) -> list[str]:
    """What one live row disagrees with the rewrite's reading of its snapshot.

    A row the rewrite cannot read at all is a finding too, not an exception: `verify` is a census, and
    aborting on the first bad row would hide every one behind it.
    """
    try:
        expected = remap(before, workflow_id)
    except BackfillError as error:
        return [f'{where} cannot be read: {error}']
    if expected == after:
        return []
    return [f'{where} is not what its snapshot renumbers to ({_describe(after, workflow_id)})']


def _describe(payload: bytes, workflow_id: str) -> str:
    """What a live row currently names, for a finding a reader can act on."""
    try:
        return str(
            [
                (vocabulary.field, vocabulary.name_now(number))
                for vocabulary, number in members(parse(payload, workflow_id), workflow_id)
            ]
            or 'no vocabulary'
        )
    except BackfillError as error:
        return f'unreadable: {error}'


def written_since(conn: sql.Connection, cutoff: datetime.datetime) -> list[str]:
    """Snapshot rows written at or after `cutoff` — the moment the surface was closed.

    Such a row was written by the revision that reads the shared numbering, so putting it through the
    retired table would land on a different member, and `verify` could not tell: it would read both
    sides the same wrong way. Nothing else in the procedure can see this, which is why `--apply`
    refuses rather than reports.

    A draft's write time is the snapshot's `updated_at` as its writer left it: `0012`'s trigger stamps
    every later write, but after the snapshot it takes, so that snapshot carries what the deployed
    writer set. An assessment's is its submission's `submitted_at`.
    """
    drafts, assessments = TIERS
    with contextlib.closing(conn.cursor()) as cursor:
        cursor.execute(
            f'SELECT worksheet_id, workflow_id FROM {drafts.snapshot} WHERE updated_at >= %s',  # noqa: S608
            (cutoff,),
        )
        found = [
            f'{drafts.table} {(str(row[0]), str(row[1]))} was written at or after {cutoff.isoformat()}'
            for row in cursor.fetchall()
        ]
        # An assessment carries no time of its own; the submission that committed it does.
        cursor.execute(
            f'SELECT a.submission_id, a.workflow_id FROM {assessments.snapshot} a'  # noqa: S608
            ' JOIN curation.submissions s ON s.id = a.submission_id WHERE s.submitted_at >= %s',
            (cutoff,),
        )
        found += [
            f'{assessments.table} {(str(row[0]), str(row[1]))} was submitted at or after {cutoff.isoformat()}'
            for row in cursor.fetchall()
        ]
    return found


def _render(rows: Sequence[Row]) -> None:
    """Print the census of sections the snapshot holds, then the before/after member histogram."""
    sections: collections.Counter[str] = collections.Counter(row.workflow_id for row in rows)
    for workflow_id in (_ROUTING, _VERDICT):
        print(f'{sections[workflow_id]} {workflow_id} row(s)')
    print(f'{sum(sections.values()) - sections[_ROUTING] - sections[_VERDICT]} row(s) carrying no vocabulary')
    counts = histogram(rows)
    for table in sorted({key[0] for key in counts}):
        print(table)
        for key, count in sorted(item for item in counts.items() if item[0][0] == table):
            _, field, member, was, now = key
            moved = f'{was} -> {now}' if was != now else f'{was} (unchanged)'
            print(f'  {field:<18} {member:<44} {moved:<16} {count} row(s)')
    print(f'{len(rows)} snapshot row(s) read, {sum(1 for row in rows if row.changed)} to rewrite')


def _connect(pool: connector.Connector) -> sql.Connection:
    sql_config = config.load_sql_config()
    return sql.iam_connect(
        pool,
        connection_name=sql_config.connection_name,
        database=sql_config.database,
        db_user=sql_config.db_user,
    )


def _verify(conn: sql.Connection) -> int:
    _report(verify(conn), 'row(s) are not what the snapshot renumbers to')
    print('backfill: every live row is what its snapshot renumbers to')
    return 0


def _rewrite(conn: sql.Connection, closed_at: datetime.datetime | None) -> int:
    rows = plan(conn)
    _render(rows)
    if closed_at is None:
        return 0
    _report(written_since(conn, closed_at), f'snapshot row(s) postdate {closed_at.isoformat()}; nothing was written')
    _report(apply(conn, rows), 'snapshot row(s) matched nothing to update; nothing was written')
    print(f'backfill: {sum(1 for row in rows if row.changed)} row(s) rewritten')
    return 0


def _report(findings: Sequence[str], summary: str) -> None:
    """Print findings and stop, or return so the caller carries on."""
    for finding in findings:
        print(f'::error::backfill: {finding}')
    if findings:
        raise SystemExit(f'{len(findings)} {summary}')


def _instant(text: str) -> datetime.datetime:
    """`--closed-at`: an ISO 8601 instant carrying its UTC offset.

    Bound against `TIMESTAMPTZ`, a naive value is read in the session's zone, UTC — so an operator's
    local wall-clock time lands hours late and every draft saved in the window passes the guard.
    """
    try:
        instant = datetime.datetime.fromisoformat(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f'{text!r} is not an ISO 8601 instant') from error
    if instant.tzinfo is None:
        raise argparse.ArgumentTypeError(f'{text!r} carries no UTC offset; write it as 2026-09-03T08:00:00+00:00 does')
    return instant


def argument_parser() -> argparse.ArgumentParser:
    """The command line: `rewrite [--dry-run | --apply --closed-at INSTANT]` and `verify`."""
    parser = argparse.ArgumentParser(description='Renumber the stored curation assessments onto the shared enums.')
    commands = parser.add_subparsers(dest='command', required=True)
    rewrite = commands.add_parser('rewrite', help='renumber the stored assessments onto the shared enums')
    mode = rewrite.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='report what would change and write nothing (default)')
    mode.add_argument('--apply', action='store_true', help='write the renumbered assessments back')
    rewrite.add_argument(
        '--closed-at',
        type=_instant,
        help='ISO 8601 instant, with its UTC offset, at which the curation surface was closed; required with '
        '--apply, which refuses any snapshot row written at or after it',
    )
    commands.add_parser('verify', help='check every live row against its snapshot')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = argument_parser()
    args = parser.parse_args(argv)
    if args.command == 'rewrite' and args.apply and args.closed_at is None:
        parser.error('--apply needs --closed-at: a row written while the surface was open cannot be renumbered')

    with contextlib.closing(connector.Connector()) as pool, contextlib.closing(_connect(pool)) as conn:
        if args.command == 'verify':
            return _verify(conn)
        return _rewrite(conn, args.closed_at if args.apply else None)


if __name__ == '__main__':
    sys.exit(main())
