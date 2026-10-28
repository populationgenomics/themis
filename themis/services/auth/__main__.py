"""Server entrypoint: build the backend, load the ACL, and serve the gRPC service behind the gate.

``THEMIS_BACKEND`` selects the adapter (required — no silent default): ``cloudsql`` (the deployed
backend: the ``session_context`` table via the Cloud SQL connector, and the latest version of the
Secret Manager secret ``projects/<project>/secrets/<secret>`` that ``THEMIS_ACL_SECRET`` names) or
``fixture`` (offline runs, seeded from ``THEMIS_FIXTURE_BINDINGS`` and ``THEMIS_FIXTURE_ACL``; see
``fixture.py``). ``THEMIS_ACL_STALENESS_LIMIT_SECONDS``, an integer above the refresh interval plus the
read timeout, is how long the last good copy of the ACL is answered from once new versions stop loading.
The ``cloudsql`` backend is traced, at the ratio ``THEMIS_TRACE_SAMPLE_RATIO`` names
(``themis/telemetry/tracing.py``); the fixture backend never reads it and reaches no cloud. ``PORT`` is the
Cloud Run convention. A ``grpc.health.v1`` health service reports SERVING alongside.

The server is built through ``interceptor.gated_server``, so ``GetAccess`` admits only the callers its
contract names. The gate verifies callers as ``THEMIS_AUTHORIZER_BACKEND`` selects (``http`` against the
ID-token audience ``THEMIS_ID_TOKEN_AUDIENCE``, this service's own URL; ``fixture`` seeded from
``THEMIS_AUTH_FIXTURE_CALLERS``) in ``THEMIS_GCP_PROJECT``, and resolves a claim's session through
this service's own servicer rather than over the network. ``THEMIS_GCP_PROJECT`` also completes the
operator account ``GetAccess`` admits to every Project.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from themis.clients.auth import interceptor as interceptor_mod
from themis.clients.auth import rules
from themis.rpc import auth_pb2_grpc
from themis.services.auth import acl_holder, fixture
from themis.services.auth import backend as auth_backend
from themis.services.auth import servicer as servicer_mod
from themis.telemetry import tracing

# The Cloud Run service's name, the one Cloud Trace lists the spans under.
SERVICE_NAME = 'themis-auth'
STALENESS_LIMIT_VAR = 'THEMIS_ACL_STALENESS_LIMIT_SECONDS'
_ACL_SECRET_VAR = 'THEMIS_ACL_SECRET'  # noqa: S105 — the variable's name, not a secret
_FIXTURE_CALLERS_VAR = 'THEMIS_AUTH_FIXTURE_CALLERS'


def build_backend() -> auth_backend.AuthBackend:
    backend = os.environ.get('THEMIS_BACKEND')
    if backend is None:
        raise SystemExit('THEMIS_BACKEND is required (expected "cloudsql" or "fixture")')
    if backend == 'cloudsql':
        return _live_backend_from_env()
    if backend == 'fixture':
        return fixture.backend_from_env()
    raise SystemExit(f'unsupported THEMIS_BACKEND {backend!r} (expected "cloudsql" or "fixture")')


def staleness_limit_seconds() -> int:
    """``THEMIS_ACL_STALENESS_LIMIT_SECONDS``, or ``SystemExit``.

    It has to exceed the refresh interval plus the longest a read may take: below that, a copy could go
    stale before the next refresh replaced it, and the service would refuse everyone for part of every
    interval.
    """
    raw = _require(STALENESS_LIMIT_VAR)
    try:
        limit = int(raw)
    except ValueError as e:
        raise SystemExit(f'{STALENESS_LIMIT_VAR} must be an integer number of seconds, got {raw!r}') from e
    floor = acl_holder.REFRESH_INTERVAL_SECONDS + auth_backend.ACL_READ_TIMEOUT_SECONDS
    if limit <= floor:
        raise SystemExit(
            f'{STALENESS_LIMIT_VAR} must exceed the {acl_holder.REFRESH_INTERVAL_SECONDS} s refresh interval plus '
            f'the {auth_backend.ACL_READ_TIMEOUT_SECONDS} s read timeout ({floor} s), got {limit}'
        )
    return limit


def install_tracing() -> None:
    """Install the trace pipeline for the ``cloudsql`` backend; any other backend runs untraced."""
    if os.environ.get('THEMIS_BACKEND') == 'cloudsql':
        tracing.install_from_env(SERVICE_NAME)


def build_authorizer(servicer: servicer_mod.Servicer) -> interceptor_mod.Authorizer:
    return interceptor_mod.authorizer_resolving_through(
        servicer_mod.in_process_session_resolver(servicer), fixture_callers_var=_FIXTURE_CALLERS_VAR
    )


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


def _live_backend_from_env() -> auth_backend.AuthBackend:
    # Deferred so the fixture path never loads the connector or the Secret Manager client.
    from google.cloud import secretmanager  # noqa: PLC0415

    from themis.services.auth import cloudsql, live, secret  # noqa: PLC0415

    sessions = cloudsql.SessionStore(
        cloudsql.engine(
            connection_name=_require('THEMIS_SQL_CONNECTION_NAME'),
            database=_require('THEMIS_SQL_DATABASE'),
            db_user=_require('THEMIS_DB_USER'),
        )
    )
    try:
        acl = secret.AclSecret(_require(_ACL_SECRET_VAR), secretmanager.SecretManagerServiceClient())
    except ValueError as e:
        raise SystemExit(f'{_ACL_SECRET_VAR}: {e}') from e
    return live.LiveBackend(sessions, acl)


async def _serve() -> None:
    backend = build_backend()
    limit = staleness_limit_seconds()
    operator = rules.operator_email(interceptor_mod.project_from_env())
    install_tracing()
    acl = await acl_holder.AclHolder.load(
        backend.read_acl,
        read_timeout_seconds=auth_backend.ACL_READ_TIMEOUT_SECONDS,
        staleness_limit_seconds=limit,
        clock=time.monotonic,
    )
    servicer = servicer_mod.Servicer(backend, acl, operator=operator)
    server = interceptor_mod.gated_server(build_authorizer(servicer), observers=[tracing.server_interceptor()])
    auth_pb2_grpc.add_AuthServicer_to_server(servicer, server)
    # grpc_health ships no py.typed; `health.aio` is a runtime re-export pyright can't see.
    health_servicer = health.aio.HealthServicer()  # pyright: ignore[reportAttributeAccessIssue]
    await health_servicer.set('', health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    server.add_insecure_port(f'[::]:{os.environ.get("PORT", "8080")}')  # TLS terminated by Cloud Run
    await server.start()
    refresher = asyncio.create_task(acl.refresh_forever(acl_holder.REFRESH_INTERVAL_SECONDS))
    termination = asyncio.create_task(server.wait_for_termination())
    try:
        done, _ = await asyncio.wait({refresher, termination}, return_when=asyncio.FIRST_COMPLETED)
        if refresher in done:
            refresher.result()  # it only ends by raising: exit rather than serve a copy nothing refreshes
    finally:
        refresher.cancel()
        await server.stop(None)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(_serve())


if __name__ == '__main__':
    main()
