"""Entry point: apply pending migrations against Cloud SQL.

Reads the connection + GRANT-login config from the environment (`config`), then
applies `themis/migrate/migrations/` idempotently. The deploy step runs it after `pulumi up`:
`uv run --group migrate python -m themis.migrate`.
"""

from __future__ import annotations

import pathlib

from themis.migrate import cloudsql, config

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent / 'migrations'


def main() -> None:
    sql = config.load_sql_config()
    substitutions = config.load_substitutions()
    applied = cloudsql.apply_migrations(sql, _MIGRATIONS_DIR, substitutions)
    if applied:
        print(f'themis-migrate: applied migrations {list(applied)}')
    else:
        print('themis-migrate: schema up to date')


if __name__ == '__main__':
    main()
