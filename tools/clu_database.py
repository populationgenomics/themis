"""The environment's database, reached as a service account through the Cloud SQL connector.

The instance refuses direct connections, and its logins are IAM principals, so a laptop tool dials it
through the connector with credentials impersonating the account (`themis-clu` by default; see
`tools.clu`) and logs in as that account's role.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from google.cloud.sql import connector

from themis.common import sql
from tools import clu


def connection_name(project: str) -> str:
    """The `project:region:instance` name the connector dials for `project`'s instance."""
    return f'{project}:{clu.REGION}:{clu.SQL_INSTANCE}'


@contextlib.contextmanager
def connect(project: str, service_account: str) -> Iterator[sql.Connection]:
    """One connection to `project`'s database as `service_account`; it and its connector close on exit.

    Raises:
        google.auth.exceptions.DefaultCredentialsError: If there is no application-default login.
    """
    with contextlib.closing(connector.Connector(credentials=clu.impersonated(service_account))) as pool:
        conn = sql.iam_connect(
            pool,
            connection_name=connection_name(project),
            database=clu.SQL_DATABASE,
            db_user=clu.db_user(service_account),
        )
        with contextlib.closing(conn):
            yield conn
