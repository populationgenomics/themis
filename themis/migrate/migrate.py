"""The forward-only SQL migration runner (docs/design/migrations.md).

Lean by design: plain `NNNN_name.sql` files under `themis/migrate/migrations/` and this small
runner — no ORM, no Alembic. The runner discovers the files, renders their
`${VAR}` placeholders from a substitution map (the per-user GRANTs need the IAM
DB-user logins), and applies the pending ones in version order through a `Ledger`.
Forward-only: a pending migration whose version is at or below an already-applied
one is rejected. The ordering / idempotency logic here is unit-tested against
`InMemoryLedger`; the live DDL apply against Cloud SQL is `cloudsql.CloudSqlLedger`.
"""

from __future__ import annotations

import abc
import dataclasses
import pathlib
import re
from collections.abc import Mapping, Sequence

_FILENAME_RE = re.compile(r'^(\d{4})_([a-z0-9_]+)\.sql$')
_PLACEHOLDER_RE = re.compile(r'\$\{(\w+)\}')


@dataclasses.dataclass(frozen=True)
class Migration:
    """One migration file.

    Attributes:
        version: The zero-padded numeric prefix, as an int (1-based, contiguous).
        name: The descriptive slug after the prefix.
        sql: The raw file contents, placeholders unrendered.
    """

    version: int
    name: str
    sql: str


class Ledger(abc.ABC):
    """The record of which migrations have been applied, and how to apply one."""

    @abc.abstractmethod
    def applied_versions(self) -> set[int]:
        """Return the set of already-applied migration versions."""
        ...

    @abc.abstractmethod
    def record(self, migration: Migration, sql: str) -> None:
        """Apply `sql` and mark `migration.version` applied, atomically."""
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
        migrations.append(Migration(int(match.group(1)), match.group(2), path.read_text('utf-8')))
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


def run(
    migrations: Sequence[Migration],
    ledger: Ledger,
    *,
    substitutions: Mapping[str, str] | None = None,
) -> list[int]:
    """Apply the migrations not yet in `ledger`, in version order, forward-only.

    Args:
        migrations: The full ordered migration set (from `discover`).
        ledger: The record of applied versions + how to apply one.
        substitutions: The `${VAR}` values to render into each pending migration.

    Returns:
        The versions applied by this call, ascending (empty if already up to date).

    Raises:
        ValueError: If a pending migration's version is at or below an
            already-applied version (a forward-only violation), or a placeholder
            has no substitution.
    """
    values = substitutions if substitutions is not None else {}
    applied = ledger.applied_versions()
    pending = sorted((m for m in migrations if m.version not in applied), key=lambda m: m.version)
    if not pending:
        return []
    highest_applied = max(applied, default=0)
    lowest_pending = pending[0].version
    if lowest_pending <= highest_applied:
        raise ValueError(
            f'forward-only violation: migration {lowest_pending} is pending but {highest_applied} is already applied'
        )
    for migration in pending:
        ledger.record(migration, render(migration.sql, values))
    return [migration.version for migration in pending]


class InMemoryLedger(Ledger):
    """A `Ledger` test double: records versions + rendered SQL in memory."""

    def __init__(self) -> None:
        self._applied: dict[int, str] = {}

    def applied_versions(self) -> set[int]:
        return set(self._applied)

    def record(self, migration: Migration, sql: str) -> None:
        self._applied[migration.version] = sql

    def rendered_sql(self, version: int) -> str:
        """The SQL recorded for a version (test inspection)."""
        return self._applied[version]
