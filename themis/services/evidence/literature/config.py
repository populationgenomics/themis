"""The literature interface's environment contract: ``THEMIS_LITERATURE_*`` to a backend.

``THEMIS_LITERATURE_BACKEND`` selects the adapter (required — no silent default): ``fixture``
(in-memory, seeded from ``THEMIS_LITERATURE_FIXTURE``) or ``live`` (the litcache-reading backend over
``THEMIS_LITERATURE_FULLTEXT_BUCKET``, plus the Cloud SQL crosswalk named by the three
``THEMIS_LITERATURE_CROSSWALK_*`` vars). Every value this interface reads is its own; no other
interface of the evidence image shares one.

The crosswalk vars are all-or-nothing: set together they wire external-id resolution, unset together
they leave it off and ``MaybeIngestPapers`` answers UNAVAILABLE. A partial set is a ``SystemExit`` —
a half-configured lookup would fail per request instead of at deploy.

Malformed input is a ``SystemExit`` at startup, never a backend that serves an empty corpus: "no such
paper" from an unseeded store is indistinguishable from a paper genuinely absent from the corpus.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable

from google.api_core import exceptions as api_exceptions
from google.cloud import storage
from google.cloud.sql import connector as sql_connector

from themis.common import sql
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import litcache as litcache_backend

_BACKEND_VAR = 'THEMIS_LITERATURE_BACKEND'
_FIXTURE_VAR = 'THEMIS_LITERATURE_FIXTURE'
_BUCKET_VAR = 'THEMIS_LITERATURE_FULLTEXT_BUCKET'
_CROSSWALK_INSTANCE_VAR = 'THEMIS_LITERATURE_CROSSWALK_INSTANCE'
_CROSSWALK_DATABASE_VAR = 'THEMIS_LITERATURE_CROSSWALK_DATABASE'
_CROSSWALK_DB_USER_VAR = 'THEMIS_LITERATURE_CROSSWALK_DB_USER'
_CROSSWALK_VARS = (_CROSSWALK_INSTANCE_VAR, _CROSSWALK_DATABASE_VAR, _CROSSWALK_DB_USER_VAR)


def backend_from_env(stack: contextlib.AsyncExitStack) -> literature_backend.LiteratureBackend:
    """The adapter named by ``THEMIS_LITERATURE_BACKEND``, or ``SystemExit``.

    Args:
        stack: Owns whatever client the adapter holds open, for as long as the server runs — it
            unwinds on a startup failure, not on a Cloud Run stop (see ``interface.register``).

    Returns:
        The selected backend, ready to serve.

    Raises:
        SystemExit: the selector is unset or unknown, or the selected adapter's own config is missing
            or malformed.
    """
    backend = os.environ.get(_BACKEND_VAR)
    if backend is None:
        raise SystemExit(f'{_BACKEND_VAR} is required (expected "fixture" or "live")')
    if backend == 'fixture':
        return literature_backend.fixture_backend_from_json(os.environ.get(_FIXTURE_VAR), var_name=_FIXTURE_VAR)
    if backend == 'live':
        return _litcache_backend_from_env(stack)
    raise SystemExit(f'unsupported {_BACKEND_VAR} {backend!r} (expected "fixture" or "live")')


def _litcache_backend_from_env(stack: contextlib.AsyncExitStack) -> litcache_backend.LitcacheBackend:
    """Build the litcache-reading backend over the ``THEMIS_LITERATURE_FULLTEXT_BUCKET`` GCS bucket."""
    bucket_name = os.environ.get(_BUCKET_VAR)
    if not bucket_name:
        raise SystemExit(f'{_BUCKET_VAR} is required for the live backend (the litcache bucket)')
    client = storage.Client()
    stack.callback(client.close)
    bucket = client.bucket(bucket_name)
    # A bucket handle is lazy: a wrong/uncreated name would 404 every read, and _download can't tell
    # "no such object" from "no such bucket", so the service would answer NOT_FOUND for every paper —
    # the "empty corpus reads as genuinely absent" fault the fixture path fails loud on at startup. List
    # once so a bad bucket fails the startup probe instead. `objects.list` is what the runtime SA's
    # objectViewer grants (not `buckets.get`, so `bucket.exists()` would 403 on a correct deploy); an
    # empty result is a valid not-yet-populated corpus. A 403 raises Forbidden, already loud.
    try:
        next(iter(bucket.list_blobs(prefix='papers/', max_results=1)), None)
    except api_exceptions.NotFound as e:
        raise SystemExit(f'{_BUCKET_VAR} {bucket_name!r} does not exist or is not readable') from e
    return litcache_backend.LitcacheBackend(bucket, connect=_crosswalk_connect_from_env(stack))


def _crosswalk_connect_from_env(stack: contextlib.AsyncExitStack) -> Callable[[], sql.Connection] | None:
    """A crosswalk connection factory from the ``THEMIS_LITERATURE_CROSSWALK_*`` trio, or None."""
    values = {var: os.environ.get(var, '') for var in _CROSSWALK_VARS}
    set_vars = {var for var, value in values.items() if value}
    if not set_vars:
        return None
    if set_vars != set(_CROSSWALK_VARS):
        raise SystemExit(f'{sorted(_CROSSWALK_VARS)} must all be set or all unset; got {sorted(set_vars)}')
    instance = values[_CROSSWALK_INSTANCE_VAR]
    database = values[_CROSSWALK_DATABASE_VAR]
    db_user = values[_CROSSWALK_DB_USER_VAR]
    dialer = sql_connector.Connector()
    stack.callback(dialer.close)

    def connect() -> sql.Connection:
        return sql.iam_connect(dialer, connection_name=instance, database=database, db_user=db_user)

    return connect
