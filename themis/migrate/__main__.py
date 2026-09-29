"""Entry point: apply pending migrations against Cloud SQL, or check the ledger without writing.

Reads the connection + GRANT-login config from the environment (`config`), then
applies `themis/migrate/migrations/` idempotently. The deploy step runs it after `pulumi up`:
`uv run --group migrate python -m themis.migrate`. With `--check` it only compares the
ledger with the committed migrations and writes nothing; the deploy runs that before
`pulumi up`.

Exit status: 0 on success; `EXIT_LEDGER_FINDING` (1) when the ledger disagrees with the
committed migrations; `EXIT_LEDGER_UNREADABLE` (3) when `--check` could not connect to the
database or read its ledger. Any other failure exits 1 through an uncaught exception, or 2
through argparse's usage error, which is why an unread ledger gets a code of its own.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections.abc import Sequence

from themis.migrate import cloudsql, config, migrate

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent / 'migrations'

EXIT_LEDGER_FINDING = 1
EXIT_LEDGER_UNREADABLE = 3


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog='python -m themis.migrate', description=__doc__)
    parser.add_argument(
        '--check',
        action='store_true',
        help=(
            f'compare the ledger with the committed migrations without writing; exit {EXIT_LEDGER_FINDING}'
            f' if they disagree, {EXIT_LEDGER_UNREADABLE} if the ledger could not be read'
        ),
    )
    return parser.parse_args(argv)


def _check(sql: config.SqlConfig) -> None:
    pending = cloudsql.check_migrations(sql, _MIGRATIONS_DIR)
    print(
        'themis-migrate: the ledger matches the committed migrations;'
        f' pending: {[migration.version for migration in pending]}'
    )


def _apply(sql: config.SqlConfig) -> None:
    applied = cloudsql.apply_migrations(sql, _MIGRATIONS_DIR, config.load_substitutions())
    if applied:
        print(f'themis-migrate: applied migrations {list(applied)}')
    else:
        print('themis-migrate: schema up to date')


def main(argv: Sequence[str] | None = None) -> None:
    """Run the apply, or with `--check` the read-only ledger check.

    Raises:
        SystemExit: With the error on stderr: `EXIT_LEDGER_FINDING` if the ledger
            disagrees with the committed migrations, `EXIT_LEDGER_UNREADABLE` if the
            check could not read the ledger.
    """
    args = _parse_args(argv)
    sql = config.load_sql_config()
    try:
        if args.check:
            _check(sql)
        else:
            _apply(sql)
    except migrate.LedgerCheckError as error:
        print(f'themis-migrate: {error}', file=sys.stderr)
        sys.exit(EXIT_LEDGER_FINDING)
    except cloudsql.LedgerUnreadableError as error:
        print(f'themis-migrate: {error}', file=sys.stderr)
        sys.exit(EXIT_LEDGER_UNREADABLE)


if __name__ == '__main__':
    main()
