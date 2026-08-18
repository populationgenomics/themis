"""The production crosswalk connection factory: IAM-authed Cloud SQL, per worker.

The `conn_factory` `ingest_beam.run_ingestion` takes in production. Import-gated (pulls
the Cloud SQL connector + pg8000), so only the live launcher imports it; the hermetic
tests use a plain pg8000 factory against a throwaway Postgres instead.

Picklable by construction: Beam ships the factory to each Dataflow worker, and a
`connector.Connector` (background refresh threads + sockets) is not picklable — so
`__getstate__` ships only the dial config (instance, database, IAM user) and the worker
rebuilds a process-lifetime `Connector` lazily on first use. Nothing live crosses the
wire; the far side reconstructs from three strings.

`_WritePaperFn` holds one connection per worker *process* (`_MintConnection` via
`beam.utils.shared.Shared`) and serializes mints on its mutex, so this factory is called
about once per process. The lock around the lazy `Connector` build guards the case where
two threads reach an unbuilt one at the same time; the `Connector` is safe to share
across threads once built.
"""

from __future__ import annotations

import threading
from typing import Any, override

from google.cloud.sql import connector

from themis.common import sql


class CrosswalkConnFactory:
    """A picklable, IAM-authed Cloud SQL connection factory for the crosswalk mint.

    Each call dials a fresh connection through a per-process `Connector` (built lazily
    in the worker). The connector, database, and IAM user identify the instance; the
    calling worker SA is the DB user (no stored password).
    """

    def __init__(self, *, connection_name: str, database: str, iam_user: str) -> None:
        self._connection_name = connection_name
        self._database = database
        self._iam_user = iam_user
        self._connector: connector.Connector | None = None
        self._lock = threading.Lock()

    @override
    def __getstate__(self) -> dict[str, str]:
        # Ship only the dial config; the Connector and lock are worker-local, rebuilt
        # on unpickle (neither is picklable).
        return {
            'connection_name': self._connection_name,
            'database': self._database,
            'iam_user': self._iam_user,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        self._connection_name = state['connection_name']
        self._database = state['database']
        self._iam_user = state['iam_user']
        self._connector = None
        self._lock = threading.Lock()

    def _pool(self) -> connector.Connector:
        with self._lock:
            if self._connector is None:
                self._connector = connector.Connector()
            return self._connector

    def __call__(self) -> sql.Connection:
        return sql.iam_connect(
            self._pool(),
            connection_name=self._connection_name,
            database=self._database,
            db_user=self._iam_user,
        )
