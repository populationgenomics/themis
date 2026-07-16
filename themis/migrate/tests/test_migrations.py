"""The committed migrations discover and render cleanly."""

from __future__ import annotations

import pathlib

from themis.migrate import migrate

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / 'migrations'


def test_committed_migrations_are_contiguous() -> None:
    migrations = migrate.discover(_MIGRATIONS_DIR)
    assert [(m.version, m.name) for m in migrations] == [
        (1, 'session_context'),
        (2, 'grants'),
        (3, 'litcache_crosswalk'),
    ]


def test_litcache_crosswalk_migration_splits_cleanly() -> None:
    crosswalk = next(m for m in migrate.discover(_MIGRATIONS_DIR) if m.name == 'litcache_crosswalk')
    assert '${' not in crosswalk.sql  # no substitutions — schema/table/index only
    assert len(migrate.split_statements(crosswalk.sql)) == 3  # CREATE SCHEMA, TABLE, INDEX


def test_grants_migration_renders_and_splits_cleanly() -> None:
    grants = next(m for m in migrate.discover(_MIGRATIONS_DIR) if m.name == 'grants')
    rendered = migrate.render(grants.sql, {'AUTH_DB_USER': 'themis-auth@cpg-themis-dev.iam'})
    assert '${' not in rendered
    assert 'GRANT SELECT ON session_context TO "themis-auth@cpg-themis-dev.iam"' in rendered
    # The comment block attaches to the single GRANT statement.
    assert len(migrate.split_statements(rendered)) == 1
