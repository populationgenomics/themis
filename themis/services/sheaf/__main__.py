"""Server entrypoint: build the authorizer, the backend and the limits from the environment, and serve.

Every selector is required, with no silent default. The authorizer the auth interceptor resolves each
call through is ``interceptor.authorizer_from_env``'s, seeded in fixture mode from
``THEMIS_SHEAF_FIXTURE_CONTEXTS`` and ``THEMIS_SHEAF_FIXTURE_CALLERS``. ``THEMIS_SHEAF_BACKEND`` picks
the store: ``gcs`` over the bucket ``THEMIS_SHEAF_BUCKET`` names, every repository's keys at its
root under the repository's name, signing pack URLs as the service account ``THEMIS_SHEAF_SIGNING_ACCOUNT``
names; or ``local`` over the directory ``THEMIS_SHEAF_LOCAL_ROOT`` names, which signs nothing.
The three ceilings — ``THEMIS_SHEAF_MAX_PUBLISH_BYTES``, ``THEMIS_SHEAF_MAX_REFS``,
``THEMIS_SHEAF_MAX_DOCUMENT_BYTES`` — and a signed pack URL's lifetime,
``THEMIS_SHEAF_PACK_URL_LIFETIME_SECONDS``, are positive integers. The ``gcs`` backend is traced, at the
ratio ``THEMIS_TRACE_SAMPLE_RATIO`` names (``themis/telemetry/tracing.py``); the ``local`` backend never
reads it and reaches no cloud. ``PORT`` is the Cloud Run convention; a ``grpc.health.v1`` health service
reports SERVING alongside.

The server is built through ``interceptor.gated_server`` and no other way, so every rpc is behind the
auth interceptor (rpc-authorization.md).
"""

from __future__ import annotations

import asyncio
import os

from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from themis import sheaf
from themis.clients.auth import interceptor as interceptor_mod
from themis.rpc import sheaf_pb2_grpc
from themis.services.sheaf import servicer as servicer_mod
from themis.telemetry import tracing

_FIXTURE_CONTEXTS_VAR = 'THEMIS_SHEAF_FIXTURE_CONTEXTS'
_FIXTURE_CALLERS_VAR = 'THEMIS_SHEAF_FIXTURE_CALLERS'
_BACKEND_VAR = 'THEMIS_SHEAF_BACKEND'
# The Cloud Run service's name, the one Cloud Trace lists the spans under.
SERVICE_NAME = 'themis-sheaf'
_LIMIT_VARS = {
    'max_publish_bytes': 'THEMIS_SHEAF_MAX_PUBLISH_BYTES',
    'max_refs': 'THEMIS_SHEAF_MAX_REFS',
    'max_document_bytes': 'THEMIS_SHEAF_MAX_DOCUMENT_BYTES',
    'pack_url_lifetime_seconds': 'THEMIS_SHEAF_PACK_URL_LIFETIME_SECONDS',
}


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


def build_authorizer() -> interceptor_mod.Authorizer:
    return interceptor_mod.authorizer_from_env(
        fixture_contexts_var=_FIXTURE_CONTEXTS_VAR, fixture_callers_var=_FIXTURE_CALLERS_VAR
    )


def build_backend() -> sheaf.Backend:
    kind = os.environ.get(_BACKEND_VAR)
    if kind is None:
        raise SystemExit(f'{_BACKEND_VAR} is required (expected "gcs" or "local")')
    if kind == 'gcs':
        return _gcs_backend_from_env()
    if kind == 'local':
        return sheaf.LocalBackend(_require('THEMIS_SHEAF_LOCAL_ROOT'))
    raise SystemExit(f'unsupported {_BACKEND_VAR} {kind!r} (expected "gcs" or "local")')


def _gcs_backend_from_env() -> sheaf.Backend:
    # Deferred so the local backend never loads the cloud client.
    from google.cloud import storage  # noqa: PLC0415

    from themis.sheaf.backends import gcs  # noqa: PLC0415

    bucket = _require('THEMIS_SHEAF_BUCKET')
    signer = gcs.IamSigner(_require('THEMIS_SHEAF_SIGNING_ACCOUNT'))
    return gcs.GcsBackend(storage.Client().bucket(bucket), signer=signer)


def install_tracing() -> None:
    """Install the trace pipeline for the ``gcs`` backend; the ``local`` backend runs untraced."""
    if os.environ.get(_BACKEND_VAR) == 'gcs':
        tracing.install_from_env(SERVICE_NAME)


def build_limits() -> servicer_mod.Limits:
    values = {}
    for field, var in _LIMIT_VARS.items():
        raw = _require(var)
        try:
            value = int(raw)
        except ValueError as exc:
            raise SystemExit(f'{var} must be a positive integer, got {raw!r}') from exc
        if value <= 0:
            raise SystemExit(f'{var} must be a positive integer, got {raw!r}')
        values[field] = value
    try:
        return servicer_mod.Limits(**values)
    except ValueError as exc:
        raise SystemExit(f'the sheaf limits are out of range: {exc}') from exc


async def _serve() -> None:
    authorizer, backend, limits = build_authorizer(), build_backend(), build_limits()
    install_tracing()
    server = interceptor_mod.gated_server(authorizer, observers=[tracing.server_interceptor()])
    servicer = servicer_mod.Servicer(backend, limits)
    sheaf_pb2_grpc.add_SheafServicer_to_server(servicer, server)
    # grpc_health ships no py.typed; `health.aio` is a runtime re-export pyright can't see.
    health_servicer = health.aio.HealthServicer()  # pyright: ignore[reportAttributeAccessIssue]
    await health_servicer.set('', health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    server.add_insecure_port(f'[::]:{os.environ.get("PORT", "8080")}')  # TLS terminated by Cloud Run
    await server.start()
    await server.wait_for_termination()


def main() -> None:
    asyncio.run(_serve())


if __name__ == '__main__':
    main()
