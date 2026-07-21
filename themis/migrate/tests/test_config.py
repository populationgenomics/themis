"""Environment parsing for the migration runner: fail loud on a gap."""

from __future__ import annotations

import json

import pytest

from themis.migrate import config

_SQL_ENV = {
    'THEMIS_SQL_CONNECTION_NAME': 'cpg-themis-dev:australia-southeast1:themis-sql',
    'THEMIS_SQL_DATABASE': 'themis',
    'THEMIS_DB_USER': 'themis-migrate@cpg-themis-dev.iam',
}


def test_load_sql_config_reads_every_field() -> None:
    sql = config.load_sql_config(_SQL_ENV)
    assert sql.connection_name == _SQL_ENV['THEMIS_SQL_CONNECTION_NAME']
    assert sql.database == 'themis'
    assert sql.db_user == 'themis-migrate@cpg-themis-dev.iam'


def test_load_sql_config_raises_on_a_missing_field() -> None:
    with pytest.raises(RuntimeError, match='THEMIS_DB_USER'):
        config.load_sql_config({k: v for k, v in _SQL_ENV.items() if k != 'THEMIS_DB_USER'})


def test_load_substitutions_defaults_to_empty() -> None:
    assert config.load_substitutions({}) == {}


def test_load_substitutions_parses_a_json_object() -> None:
    substitutions = config.load_substitutions(
        {'THEMIS_MIGRATE_SUBSTITUTIONS': json.dumps({'AUTH_DB_USER': 'themis-auth'})}
    )
    assert substitutions == {'AUTH_DB_USER': 'themis-auth'}


def test_load_substitutions_rejects_invalid_json() -> None:
    with pytest.raises(RuntimeError, match='not valid JSON'):
        config.load_substitutions({'THEMIS_MIGRATE_SUBSTITUTIONS': 'not json'})


def test_load_substitutions_rejects_a_non_object() -> None:
    with pytest.raises(RuntimeError, match='JSON object'):
        config.load_substitutions({'THEMIS_MIGRATE_SUBSTITUTIONS': '["a", "b"]'})


def test_load_substitutions_rejects_non_string_values() -> None:
    with pytest.raises(RuntimeError, match='string -> string'):
        config.load_substitutions({'THEMIS_MIGRATE_SUBSTITUTIONS': json.dumps({'AUTH_DB_USER': 7})})
