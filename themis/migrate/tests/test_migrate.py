"""The migration runner: discovery, ordering, idempotency, forward-only."""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from themis.migrate import migrate

_SUBSTITUTIONS = {'AUTH_DB_USER': 'themis-auth@cpg-themis-dev.iam', 'BFF_DB_USER': 'themis-web@cpg-themis-dev.iam'}


def _migration(version: int, name: str | None = None, sql: str = 'SELECT 1;') -> migrate.Migration:
    return migrate.Migration(
        version=version,
        name=name if name is not None else f'change_{version}',
        sql=sql,
        sha256=hashlib.sha256(sql.encode('utf-8')).hexdigest(),
    )


def _names(ledger: migrate.InMemoryLedger) -> dict[int, str]:
    return {version: row.name for version, row in ledger.applied_migrations().items()}


def test_discover_empty_directory_is_empty(tmp_path: pathlib.Path) -> None:
    assert migrate.discover(tmp_path) == []


def test_discover_orders_by_version(tmp_path: pathlib.Path) -> None:
    (tmp_path / '0002_second.sql').write_text('SELECT 2;', 'utf-8')
    (tmp_path / '0001_first.sql').write_text('SELECT 1;', 'utf-8')
    assert [(m.version, m.name) for m in migrate.discover(tmp_path)] == [(1, 'first'), (2, 'second')]


def test_discover_hashes_the_unrendered_file_bytes(tmp_path: pathlib.Path) -> None:
    raw = b'GRANT SELECT ON t TO "${AUTH_DB_USER}";\n'
    (tmp_path / '0001_grants.sql').write_bytes(raw)
    [migration] = migrate.discover(tmp_path)
    assert migration.sha256 == hashlib.sha256(raw).hexdigest()


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
    assert _names(ledger) == {1: 'change_1', 2: 'change_2'}


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


def test_run_records_the_committed_files_hash_not_the_rendered_sqls() -> None:
    ledger = migrate.InMemoryLedger()
    migration = _migration(1, sql='GRANT SELECT TO "${BFF_DB_USER}";')
    migrate.run([migration], ledger, substitutions=_SUBSTITUTIONS)
    assert ledger.applied_migrations()[1].sha256 == migration.sha256


def test_run_rejects_a_forward_only_violation() -> None:
    ledger = migrate.InMemoryLedger()
    # A ledger with a gap: versions 1 and 3 already applied, then present a pending 2.
    ledger.record(_migration(1), 'SELECT 1;')
    ledger.record(_migration(3), 'SELECT 1;')
    with pytest.raises(migrate.ForwardOnlyViolationError, match='forward-only'):
        migrate.run([_migration(1), _migration(2), _migration(3)], ledger)


def test_run_rejects_an_applied_migration_whose_name_and_content_differ_from_the_committed_file() -> None:
    # Another branch's migration 2 is in the ledger; this tree has a different 2, and a pending 3.
    ledger = migrate.InMemoryLedger()
    migrate.run(
        [_migration(1, name='base'), _migration(2, name='variant_notes', sql='ALTER TABLE t ADD n text;')], ledger
    )
    committed = [_migration(1, name='base'), _migration(2, name='case_flags'), _migration(3)]
    with pytest.raises(
        migrate.LedgerMismatchError, match=r'0002_case_flags\.sql: content differs.*0002_variant_notes\.sql'
    ):
        migrate.run(committed, ledger)
    assert _names(ledger) == {1: 'base', 2: 'variant_notes'}


def test_run_rejects_an_applied_migration_whose_content_differs_under_the_same_name() -> None:
    # A branch amended its already-deployed 2 and redeploys it under the same name, with a pending 3.
    ledger = migrate.InMemoryLedger()
    migrate.run([_migration(1), _migration(2, sql='ALTER TABLE t ADD n text;')], ledger)
    committed = [_migration(1), _migration(2, sql='ALTER TABLE t ADD n integer;'), _migration(3)]
    with pytest.raises(migrate.LedgerMismatchError, match=r'0002_change_2\.sql: content differs'):
        migrate.run(committed, ledger)
    assert _names(ledger) == {1: 'change_1', 2: 'change_2'}


def test_run_accepts_a_renamed_file_whose_content_matches_the_recorded_hash() -> None:
    ledger = migrate.InMemoryLedger()
    migrate.run([_migration(1, name='old_name')], ledger)
    assert migrate.run([_migration(1, name='new_name'), _migration(2)], ledger) == [2]


def test_a_row_without_a_hash_passes_on_its_name_whatever_the_content() -> None:
    # A row written before the ledger kept hashes; the committed file's content is not compared.
    ledger = migrate.InMemoryLedger()
    ledger.add_row(1, migrate.AppliedMigration(name='change_1', sha256=None))
    assert migrate.run([_migration(1, sql='SELECT 42;'), _migration(2)], ledger) == [2]


def test_a_row_without_a_hash_is_rejected_on_a_different_name() -> None:
    ledger = migrate.InMemoryLedger()
    ledger.add_row(1, migrate.AppliedMigration(name='variant_notes', sha256=None))
    with pytest.raises(migrate.LedgerMismatchError, match=r'0001_change_1\.sql: name differs.*0001_variant_notes\.sql'):
        migrate.run([_migration(1), _migration(2)], ledger)
    assert _names(ledger) == {1: 'variant_notes'}


def test_the_ledger_check_reports_every_mismatch_in_one_error() -> None:
    applied = {
        1: migrate.AppliedMigration(name='variant_notes', sha256=None),
        2: migrate.AppliedMigration(name='change_2', sha256=_migration(2, sql='SELECT 2;').sha256),
    }
    with pytest.raises(migrate.LedgerMismatchError) as excinfo:
        migrate.check_applied([_migration(1), _migration(2)], applied)
    message = str(excinfo.value)
    assert '0001_change_1.sql: name differs' in message
    assert '0002_change_2.sql: content differs' in message
    assert 'docs/design/migrations.md' in message


def test_plan_returns_the_pending_migrations_without_applying_them() -> None:
    ledger = migrate.InMemoryLedger()
    migrate.run([_migration(1)], ledger)
    committed = [_migration(1), _migration(2), _migration(3)]
    assert [m.version for m in migrate.plan(committed, ledger.applied_migrations())] == [2, 3]
    assert _names(ledger) == {1: 'change_1'}


def test_run_accepts_an_applied_migration_the_tree_lacks() -> None:
    # Another branch's migration 3 is in the ledger; this tree stops at 2.
    ledger = migrate.InMemoryLedger()
    migrate.run([_migration(1), _migration(2)], ledger)
    ledger.record(_migration(3, name='case_flags'), 'SELECT 1;')
    assert migrate.run([_migration(1), _migration(2)], ledger) == []
    assert _names(ledger) == {1: 'change_1', 2: 'change_2', 3: 'case_flags'}
