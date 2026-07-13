"""The migration runner: discovery, ordering, idempotency, forward-only."""

from __future__ import annotations

import pathlib

import pytest

from themis.migrate import migrate

_SUBSTITUTIONS = {'AUTH_DB_USER': 'themis-auth@cpg-themis-dev.iam', 'BFF_DB_USER': 'themis-web@cpg-themis-dev.iam'}


def _migration(version: int, name: str = 'change', sql: str = 'SELECT 1;') -> migrate.Migration:
    return migrate.Migration(version=version, name=name, sql=sql)


def test_discover_empty_directory_is_empty(tmp_path: pathlib.Path) -> None:
    assert migrate.discover(tmp_path) == []


def test_discover_orders_by_version(tmp_path: pathlib.Path) -> None:
    (tmp_path / '0002_second.sql').write_text('SELECT 2;', 'utf-8')
    (tmp_path / '0001_first.sql').write_text('SELECT 1;', 'utf-8')
    assert [(m.version, m.name) for m in migrate.discover(tmp_path)] == [(1, 'first'), (2, 'second')]


def test_discover_rejects_a_malformed_filename(tmp_path: pathlib.Path) -> None:
    (tmp_path / 'init.sql').write_text('SELECT 1;', 'utf-8')
    with pytest.raises(ValueError, match=r'NNNN_name\.sql'):
        migrate.discover(tmp_path)


def test_discover_rejects_a_version_gap(tmp_path: pathlib.Path) -> None:
    (tmp_path / '0001_a.sql').write_text('SELECT 1;', 'utf-8')
    (tmp_path / '0003_c.sql').write_text('SELECT 1;', 'utf-8')
    with pytest.raises(ValueError, match='contiguous'):
        migrate.discover(tmp_path)


def test_render_substitutes_every_placeholder() -> None:
    rendered = migrate.render('GRANT SELECT TO "${AUTH_DB_USER}", "${BFF_DB_USER}";', _SUBSTITUTIONS)
    assert '${' not in rendered
    assert 'themis-auth@cpg-themis-dev.iam' in rendered
    assert 'themis-web@cpg-themis-dev.iam' in rendered


def test_render_fails_on_a_missing_substitution() -> None:
    with pytest.raises(ValueError, match='UNKNOWN'):
        migrate.render('GRANT SELECT TO "${UNKNOWN}";', _SUBSTITUTIONS)


def test_split_statements_ignores_semicolons_in_comments_and_strings() -> None:
    sql = "-- a comment; still one\nINSERT INTO t VALUES ('a;b');\nSELECT 1;"
    assert migrate.split_statements(sql) == [
        "-- a comment; still one\nINSERT INTO t VALUES ('a;b')",
        'SELECT 1',
    ]


def test_split_statements_drops_a_trailing_comment_only_segment() -> None:
    sql = 'CREATE TABLE foo (id int);\n-- grant access in a later migration\n'
    assert migrate.split_statements(sql) == ['CREATE TABLE foo (id int)']


def test_run_applies_pending_in_version_order() -> None:
    ledger = migrate.InMemoryLedger()
    applied = migrate.run([_migration(2), _migration(1)], ledger)
    assert applied == [1, 2]
    assert ledger.applied_versions() == {1, 2}


def test_run_is_idempotent() -> None:
    ledger = migrate.InMemoryLedger()
    migrations = [_migration(1), _migration(2)]
    assert migrate.run(migrations, ledger) == [1, 2]
    assert migrate.run(migrations, ledger) == []


def test_run_only_applies_the_new_migration() -> None:
    ledger = migrate.InMemoryLedger()
    migrate.run([_migration(1)], ledger)
    assert migrate.run([_migration(1), _migration(2)], ledger) == [2]


def test_run_renders_substitutions_into_recorded_sql() -> None:
    ledger = migrate.InMemoryLedger()
    migrate.run([_migration(1, sql='GRANT SELECT TO "${BFF_DB_USER}";')], ledger, substitutions=_SUBSTITUTIONS)
    assert ledger.rendered_sql(1) == 'GRANT SELECT TO "themis-web@cpg-themis-dev.iam";'


def test_run_rejects_a_forward_only_violation() -> None:
    ledger = migrate.InMemoryLedger()
    # Versions 1 and 3 already applied, then present a pending 2.
    ledger.record(_migration(1), 'SELECT 1;')
    ledger.record(_migration(3), 'SELECT 1;')
    with pytest.raises(ValueError, match='forward-only'):
        migrate.run([_migration(2)], ledger)
