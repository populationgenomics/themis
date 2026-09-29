"""The forward-only SQL migration runner (docs/design/migrations.md).

Lean by design: plain `NNNN_name.sql` files under `themis/migrate/migrations/` and this small
runner — no ORM, no Alembic. The runner discovers the files, renders their
`${VAR}` placeholders from a substitution map (the per-user GRANTs need the IAM
DB-user logins), and applies the pending ones in version order through a `Ledger`.
Before applying anything, `plan` checks every version held by both the ledger and the
committed set: by content hash where the ledger recorded one, else by name. Forward-only:
a pending migration whose version is at or below an already-applied one is rejected. The
ordering / idempotency logic here is unit-tested against `InMemoryLedger`; the live DDL
apply against Cloud SQL is `cloudsql.CloudSqlLedger`.
"""

from __future__ import annotations

import abc
import dataclasses
import hashlib
import pathlib
import re
from collections.abc import Mapping, Sequence
from typing import override

_FILENAME_RE = re.compile(r'^(\d{4})_([a-z0-9_]+)\.sql$')
_PLACEHOLDER_RE = re.compile(r'\$\{(\w+)\}')
_RECOVERY_POINTER = 'see docs/design/migrations.md, "Checking the ledger against the tree", for recovery'


@dataclasses.dataclass(frozen=True)
class Migration:
    """One migration file.

    Attributes:
        version: The zero-padded numeric prefix, as an int (1-based, contiguous).
        name: The descriptive slug after the prefix.
        sql: The raw file contents, placeholders unrendered.
        sha256: The hex SHA-256 of the file's bytes, before rendering, so the value
            does not depend on the environment's substitutions.
    """

    version: int
    name: str
    sql: str
    sha256: str

    @property
    def filename(self) -> str:
        """The file this migration is committed as, `NNNN_name.sql`."""
        return format_filename(self.version, self.name)


@dataclasses.dataclass(frozen=True)
class AppliedMigration:
    """One ledger row: what a version was applied as.

    Attributes:
        name: The slug the version was recorded under.
        sha256: The recorded content hash, or None for a row written before the
            ledger kept hashes.
    """

    name: str
    sha256: str | None


class LedgerCheckError(ValueError):
    """The ledger disagrees with the committed migrations, so applying over it would leave the schema wrong."""


class LedgerMismatchError(LedgerCheckError):
    """The ledger recorded a committed version as a different migration than the committed file."""


class ForwardOnlyViolationError(LedgerCheckError):
    """A committed migration is pending at or below a version the ledger already holds."""


def format_filename(version: int, name: str) -> str:
    """The `NNNN_name.sql` filename of a migration version and slug."""
    return f'{version:04d}_{name}.sql'


class Ledger(abc.ABC):
    """The record of which migrations have been applied, and how to apply one."""

    @abc.abstractmethod
    def applied_migrations(self) -> dict[int, AppliedMigration]:
        """Return each already-applied version, mapped to what it was recorded as."""
        ...

    @abc.abstractmethod
    def record(self, migration: Migration, sql: str) -> None:
        """Apply `sql` and record `migration`'s version, name and hash, atomically."""
        ...


def discover(directory: pathlib.Path) -> list[Migration]:
    """Load and order the migration files in `directory`.

    Args:
        directory: The folder holding the `NNNN_name.sql` files.

    Returns:
        The migrations sorted ascending by version.

    Raises:
        ValueError: If a `.sql` filename is malformed, or the versions are not a
            contiguous 1-based sequence (a gap or duplicate).
    """
    migrations: list[Migration] = []
    for path in sorted(directory.glob('*.sql')):
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise ValueError(f'migration filename must be NNNN_name.sql (lowercase): {path.name}')
        raw = path.read_bytes()
        migrations.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                sql=raw.decode('utf-8'),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    migrations.sort(key=lambda migration: migration.version)
    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise ValueError(
                f'migration versions must be contiguous from 1; expected {expected}, got {migration.version}'
            )
    return migrations


def render(sql: str, substitutions: Mapping[str, str]) -> str:
    """Substitute `${VAR}` placeholders in a migration's SQL.

    Args:
        sql: The raw migration SQL.
        substitutions: The `VAR` to value map.

    Returns:
        The SQL with every placeholder replaced.

    Raises:
        ValueError: If the SQL references a `${VAR}` with no substitution.
    """

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in substitutions:
            raise ValueError(f'migration references ${{{key}}} but no substitution was provided')
        return substitutions[key]

    return _PLACEHOLDER_RE.sub(replace, sql)


def _is_comment_only(statement: str) -> bool:
    """True if a split segment carries no executable SQL — only `--` line comments.

    A trailing line comment (`CREATE ...;` then `-- note`) survives the split as a
    comment-only tail; Postgres rejects it as an empty query. Leading comments ride
    along with the statement they precede, so only fully-comment segments are dropped.
    """
    return all(not line.strip() or line.strip().startswith('--') for line in statement.splitlines())


def split_statements(sql: str) -> list[str]:
    """Split a rendered migration into individual statements on top-level `;`.

    pg8000 executes one statement per call, so a multi-statement file is split
    here. Semicolons inside single-quoted strings and `--` line comments are not
    treated as separators. Block comments and dollar-quoting are out of scope (the
    committed migrations use neither).

    Args:
        sql: The rendered migration SQL.

    Returns:
        The executable statements, in order — whitespace- and comment-only
        segments are dropped (Postgres rejects them as empty queries).
    """
    statements: list[str] = []
    current: list[str] = []
    in_string = False
    in_comment = False
    previous = ''
    for char in sql:
        if in_comment:
            current.append(char)
            if char == '\n':
                in_comment = False
        elif in_string:
            current.append(char)
            if char == "'":
                in_string = False
        elif char == '-' and previous == '-':
            current.append(char)
            in_comment = True
        elif char == "'":
            current.append(char)
            in_string = True
        elif char == ';':
            statement = ''.join(current).strip()
            if statement and not _is_comment_only(statement):
                statements.append(statement)
            current = []
        else:
            current.append(char)
        previous = char
    tail = ''.join(current).strip()
    if tail and not _is_comment_only(tail):
        statements.append(tail)
    return statements


def _mismatch(migration: Migration, applied: AppliedMigration) -> str | None:
    """Describe how a ledger row disagrees with the committed file of its version, if it does."""
    recorded = format_filename(migration.version, applied.name)
    if applied.sha256 is not None:
        if applied.sha256 == migration.sha256:
            return None
        if applied.name == migration.name:
            return f'{migration.filename}: content differs from the file the ledger recorded under that name'
        return f'{migration.filename}: content differs; the ledger recorded version {migration.version} as {recorded}'
    if applied.name == migration.name:
        return None
    return f'{migration.filename}: name differs; the ledger recorded version {migration.version} as {recorded}'


def check_applied(migrations: Sequence[Migration], applied: Mapping[int, AppliedMigration]) -> None:
    """Check each applied version the committed set also has against the committed file.

    A row that carries a content hash must match the committed file's hash; a row without
    one (written before the ledger kept hashes) must match its name. An applied version
    with no committed migration passes: it is another tree's migration, and it collides
    only once this tree gains that version.

    Args:
        migrations: The full committed migration set (from `discover`).
        applied: Each applied version, mapped to what the ledger recorded for it.

    Raises:
        LedgerMismatchError: Naming every applied version that disagrees with its
            committed file: by content where the row carries a hash, else by name.
    """
    committed = {migration.version: migration for migration in migrations}
    mismatches = [
        mismatch
        for version, row in sorted(applied.items())
        if version in committed and (mismatch := _mismatch(committed[version], row)) is not None
    ]
    if mismatches:
        lines = '\n'.join(f'  {mismatch}' for mismatch in mismatches)
        raise LedgerMismatchError(f'the ledger disagrees with the committed migrations:\n{lines}\n{_RECOVERY_POINTER}')


def plan(migrations: Sequence[Migration], applied: Mapping[int, AppliedMigration]) -> list[Migration]:
    """Check a ledger against the committed set and return what `run` would apply to it.

    Args:
        migrations: The full committed migration set (from `discover`).
        applied: Each applied version, mapped to what the ledger recorded for it.

    Returns:
        The committed migrations absent from the ledger, ascending by version.

    Raises:
        LedgerMismatchError: If the ledger recorded a committed version as a different
            migration (`check_applied`).
        ForwardOnlyViolationError: If a pending migration's version is at or below an
            already-applied version.
    """
    check_applied(migrations, applied)
    pending = sorted((m for m in migrations if m.version not in applied), key=lambda m: m.version)
    highest_applied = max(applied, default=0)
    if pending and pending[0].version <= highest_applied:
        raise ForwardOnlyViolationError(
            f'forward-only violation: migration {pending[0].version} is pending'
            f' but {highest_applied} is already applied'
        )
    return pending


def run(
    migrations: Sequence[Migration],
    ledger: Ledger,
    *,
    substitutions: Mapping[str, str] | None = None,
) -> list[int]:
    """Apply the migrations not yet in `ledger`, in version order, forward-only.

    Args:
        migrations: The full ordered migration set (from `discover`).
        ledger: The record of applied migrations + how to apply one.
        substitutions: The `${VAR}` values to render into each pending migration.

    Returns:
        The versions applied by this call, ascending (empty if already up to date).

    Raises:
        LedgerCheckError: Before anything is applied, if the ledger disagrees with
            the committed migrations (`plan`).
        ValueError: While applying, if a placeholder has no substitution.
    """
    values = substitutions if substitutions is not None else {}
    pending = plan(migrations, ledger.applied_migrations())
    for migration in pending:
        ledger.record(migration, render(migration.sql, values))
    return [migration.version for migration in pending]


class InMemoryLedger(Ledger):
    """A `Ledger` test double: records rows + rendered SQL in memory, by version."""

    def __init__(self) -> None:
        self._rows: dict[int, AppliedMigration] = {}
        self._sql: dict[int, str] = {}

    @override
    def applied_migrations(self) -> dict[int, AppliedMigration]:
        return dict(self._rows)

    @override
    def record(self, migration: Migration, sql: str) -> None:
        self._rows[migration.version] = AppliedMigration(name=migration.name, sha256=migration.sha256)
        self._sql[migration.version] = sql

    def add_row(self, version: int, row: AppliedMigration) -> None:
        """Insert a ledger row directly, without applying anything (test setup)."""
        self._rows[version] = row

    def rendered_sql(self, version: int) -> str:
        """The SQL recorded for a version (test inspection)."""
        return self._sql[version]
