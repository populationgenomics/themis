"""The committed migrations discover and render cleanly."""

from __future__ import annotations

import pathlib

from themis.migrate import migrate

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / 'migrations'


def test_committed_migrations_are_discoverable() -> None:
    # `discover` raises on a malformed filename or a version gap; non-empty rules out a vacuous pass.
    assert migrate.discover(_MIGRATIONS_DIR)


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


def test_litcache_crosswalk_grant_renders_and_splits_cleanly() -> None:
    grant = next(m for m in migrate.discover(_MIGRATIONS_DIR) if m.name == 'litcache_crosswalk_grant')
    rendered = migrate.render(grant.sql, {'INGEST_DB_USER': 'themis-ingest@cpg-themis-dev.iam'})
    assert '${' not in rendered
    assert 'GRANT USAGE ON SCHEMA litcache TO "themis-ingest@cpg-themis-dev.iam"' in rendered
    assert 'GRANT SELECT, INSERT ON litcache.crosswalk TO "themis-ingest@cpg-themis-dev.iam"' in rendered
    assert len(migrate.split_statements(rendered)) == 2  # the two GRANTs


def test_analyses_migration_renders_and_splits_cleanly() -> None:
    analyses = next(m for m in migrate.discover(_MIGRATIONS_DIR) if m.name == 'analyses')
    rendered = migrate.render(analyses.sql, {'WEB_DB_USER': 'themis-web@cpg-themis-dev.iam'})
    assert '${' not in rendered
    assert 'GRANT SELECT, INSERT ON analyses TO "themis-web@cpg-themis-dev.iam"' in rendered
    assert 'GRANT INSERT, DELETE ON session_context TO "themis-web@cpg-themis-dev.iam"' in rendered
    # CREATE TABLE analyses + the session_context foreign key + the two GRANTs.
    assert len(migrate.split_statements(rendered)) == 4


def test_project_members_migration_renders_and_splits_cleanly() -> None:
    members = next(m for m in migrate.discover(_MIGRATIONS_DIR) if m.name == 'project_members')
    rendered = migrate.render(members.sql, {'WEB_DB_USER': 'themis-web@cpg-themis-dev.iam'})
    assert '${' not in rendered
    assert 'GRANT SELECT ON project_members TO "themis-web@cpg-themis-dev.iam"' in rendered
    # CREATE TABLE project_members + the single GRANT.
    assert len(migrate.split_statements(rendered)) == 2


def test_projects_migration_renders_and_splits_cleanly() -> None:
    projects = next(m for m in migrate.discover(_MIGRATIONS_DIR) if m.name == 'projects')
    rendered = migrate.render(projects.sql, {'WEB_DB_USER': 'themis-web@cpg-themis-dev.iam'})
    assert '${' not in rendered
    assert 'GRANT SELECT ON projects TO "themis-web@cpg-themis-dev.iam"' in rendered
    # CREATE TABLE projects + three ALTER (foreign keys) + the single GRANT.
    assert len(migrate.split_statements(rendered)) == 5
