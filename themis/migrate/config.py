"""Migration-runner configuration, read from the environment, failing loud on a gap.

The deploy step hands the runner the Cloud SQL wiring and the GRANT logins as env:
`THEMIS_SQL_CONNECTION_NAME` / `THEMIS_SQL_DATABASE` / `THEMIS_SQL_IAM_USER` (the
migrator DB role), and `THEMIS_MIGRATE_SUBSTITUTIONS` as a JSON object of the
`${VAR}` -> login map the GRANT migrations render. A missing required value raises
rather than running a half-configured migration.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Mapping


@dataclasses.dataclass(frozen=True)
class SqlConfig:
    """The Cloud SQL connector inputs for IAM-authed access (no password).

    Attributes:
        connection_name: The `project:region:instance` string the connector dials.
        database: The application database name.
        iam_user: The migrator DB role's IAM login (the SA email minus
            `.gserviceaccount.com`, matching infra/sql.py).
    """

    connection_name: str
    database: str
    iam_user: str


def _require(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise RuntimeError(f'required environment variable {name} is unset or empty')
    return value


def load_sql_config(environ: Mapping[str, str] | None = None) -> SqlConfig:
    """Build the `SqlConfig` from the environment, failing loud on any gap."""
    source = environ if environ is not None else os.environ
    return SqlConfig(
        connection_name=_require(source, 'THEMIS_SQL_CONNECTION_NAME'),
        database=_require(source, 'THEMIS_SQL_DATABASE'),
        iam_user=_require(source, 'THEMIS_SQL_IAM_USER'),
    )


def load_substitutions(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Load the `${VAR}` GRANT-login map from `THEMIS_MIGRATE_SUBSTITUTIONS`.

    Absent means no substitutions (the empty map). When present it must be a JSON
    object of string -> string.

    Raises:
        RuntimeError: If the variable is set but is not a JSON object of strings.
    """
    source = environ if environ is not None else os.environ
    raw = source.get('THEMIS_MIGRATE_SUBSTITUTIONS')
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f'THEMIS_MIGRATE_SUBSTITUTIONS is not valid JSON: {error}') from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f'THEMIS_MIGRATE_SUBSTITUTIONS must be a JSON object, got {type(parsed).__name__}')
    substitutions: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RuntimeError('THEMIS_MIGRATE_SUBSTITUTIONS must be a JSON object of string -> string')
        substitutions[key] = value
    return substitutions
