"""The literature interface's environment contract: ``THEMIS_LITERATURE_*`` to a backend.

``THEMIS_LITERATURE_BACKEND`` selects the adapter (required — no silent default): ``fixture``
(in-memory, seeded from ``THEMIS_LITERATURE_FIXTURE``) or ``live`` (the litcache-reading backend over
``THEMIS_FULLTEXT_BUCKET``, plus the Cloud SQL crosswalk named by the three
``THEMIS_LITERATURE_CROSSWALK_*`` vars). The full-text store is one bucket that several services read,
so it travels under one name rather than an interface-scoped one; every ``THEMIS_LITERATURE_*`` value
below it is this interface's alone.

Two trios are all-or-nothing, and for the same reason: a half-configured capability fails per request
instead of at deploy, so a partial set is a ``SystemExit``. Set together, the
``THEMIS_LITERATURE_CROSSWALK_*`` vars wire external-id resolution and the
``THEMIS_LITERATURE_CONVERT_*`` vars wire the conversion lane ``MaybeIngestPapers`` enqueues onto;
unset together, each leaves its capability off and the rpc answers FAILED_PRECONDITION to a call that
would have needed it.

Malformed input is a ``SystemExit`` at startup, never a backend that serves an empty corpus: "no such
paper" from an unseeded store is indistinguishable from a paper genuinely absent from the corpus.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable

from google.api_core import exceptions as api_exceptions
from google.cloud import storage, tasks_v2
from google.cloud.sql import connector as sql_connector

from themis.common import sql
from themis.litcache import enqueue
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import litcache as litcache_backend

_BACKEND_VAR = 'THEMIS_LITERATURE_BACKEND'
_FIXTURE_VAR = 'THEMIS_LITERATURE_FIXTURE'
_BUCKET_VAR = 'THEMIS_FULLTEXT_BUCKET'
_CROSSWALK_INSTANCE_VAR = 'THEMIS_LITERATURE_CROSSWALK_INSTANCE'
_CROSSWALK_DATABASE_VAR = 'THEMIS_LITERATURE_CROSSWALK_DATABASE'
_CROSSWALK_DB_USER_VAR = 'THEMIS_LITERATURE_CROSSWALK_DB_USER'
_CROSSWALK_VARS = (_CROSSWALK_INSTANCE_VAR, _CROSSWALK_DATABASE_VAR, _CROSSWALK_DB_USER_VAR)
_CONVERT_QUEUE_VAR = 'THEMIS_LITERATURE_CONVERT_QUEUE'
_CONVERT_WORKER_URL_VAR = 'THEMIS_LITERATURE_CONVERT_WORKER_URL'
_CONVERT_INVOKER_SA_VAR = 'THEMIS_LITERATURE_CONVERT_INVOKER_SA'
_CONVERT_VARS = (_CONVERT_QUEUE_VAR, _CONVERT_WORKER_URL_VAR, _CONVERT_INVOKER_SA_VAR)


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
    """Build the litcache-reading backend over the ``THEMIS_FULLTEXT_BUCKET`` GCS bucket."""
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
    return litcache_backend.LitcacheBackend(
        bucket,
        connect=_crosswalk_connect_from_env(stack),
        enqueuer=_enqueuer_from_env(stack),
    )


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


def _enqueuer_from_env(stack: contextlib.AsyncExitStack) -> enqueue.Enqueuer | None:
    """A conversion enqueuer from the ``THEMIS_LITERATURE_CONVERT_*`` trio, or None."""
    values = {var: os.environ.get(var, '') for var in _CONVERT_VARS}
    set_vars = {var for var, value in values.items() if value}
    if not set_vars:
        return None
    if set_vars != set(_CONVERT_VARS):
        raise SystemExit(f'{sorted(_CONVERT_VARS)} must all be set or all unset; got {sorted(set_vars)}')
    target = enqueue.ConversionTarget(
        queue_path=values[_CONVERT_QUEUE_VAR],
        worker_url=values[_CONVERT_WORKER_URL_VAR],
        invoker_service_account_email=values[_CONVERT_INVOKER_SA_VAR],
    )
    # No startup probe of the queue: `cloudtasks.enqueuer` grants creates and no read, so every call
    # that would confirm the queue exists is one the runtime SA is denied. A wrong queue path surfaces
    # instead as a permanent refusal on the first paper that needs converting.
    return enqueue.Enqueuer(stack.enter_context(tasks_v2.CloudTasksClient()), target)  # closes on exit
