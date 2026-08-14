"""The committed migrations discover and render cleanly."""

from __future__ import annotations

import json
import pathlib

import yaml

from themis.migrate import migrate

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[1] / 'migrations'
_DEPLOY_WORKFLOW = pathlib.Path(__file__).resolve().parents[3] / '.github' / 'workflows' / 'deploy.yml'


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


def test_deploy_provides_every_migration_substitution() -> None:
    """A `${VAR}` in a committed migration has an entry in every `THEMIS_MIGRATE_SUBSTITUTIONS` deploy passes."""
    workflow = yaml.safe_load(_DEPLOY_WORKFLOW.read_text('utf-8'))
    # Keys are literal in the workflow; only the values are `${{ … }}` expressions.
    substitution_maps = [
        step['env']['THEMIS_MIGRATE_SUBSTITUTIONS']
        for job in workflow['jobs'].values()
        for step in job.get('steps', [])
        if 'THEMIS_MIGRATE_SUBSTITUTIONS' in step.get('env', {})
    ]
    assert substitution_maps  # the migrate step passes the map; an empty scan means the pairing is unverified
    for raw in substitution_maps:
        provided = dict.fromkeys(json.loads(raw), 'login')
        for migration in migrate.discover(_MIGRATIONS_DIR):
            migrate.render(migration.sql, provided)  # raises on a ${VAR} the deploy does not provide
