"""Server entrypoint: build the session resolver, the backend and the limits from the environment, and serve.

Every selector is required, with no silent default. ``THEMIS_AUTHORIZER_BACKEND`` picks the
authorizer: ``http`` resolves each request's session through the auth service at
``THEMIS_AUTH_URL``; ``fixture`` resolves against a map seeded from ``THEMIS_SHEAF_FIXTURE_CONTEXTS``
(JSON bearer -> binding). ``THEMIS_SHEAF_BACKEND`` picks the store: ``gcs`` over the bucket
``THEMIS_WORKSPACE_BUCKET`` names, or ``local`` over the directory ``THEMIS_SHEAF_LOCAL_ROOT`` names.
The three ceilings — ``THEMIS_SHEAF_MAX_PUBLISH_BYTES``, ``THEMIS_SHEAF_MAX_REFS``,
``THEMIS_SHEAF_MAX_DOCUMENT_BYTES`` — are positive integers. ``PORT`` is the Cloud Run convention;
a ``grpc.health.v1`` health service reports SERVING alongside.
"""

from __future__ import annotations

import asyncio
import os

import grpc.aio
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from themis import sheaf
from themis.clients.auth import session as session_mod
from themis.rpc import sheaf_pb2_grpc
from themis.services.sheaf import servicer as servicer_mod

_FIXTURE_CONTEXTS_VAR = 'THEMIS_SHEAF_FIXTURE_CONTEXTS'
_BACKEND_VAR = 'THEMIS_SHEAF_BACKEND'
_LIMIT_VARS = {
    'max_publish_bytes': 'THEMIS_SHEAF_MAX_PUBLISH_BYTES',
    'max_refs': 'THEMIS_SHEAF_MAX_REFS',
    'max_document_bytes': 'THEMIS_SHEAF_MAX_DOCUMENT_BYTES',
}


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


def build_session_resolver() -> session_mod.SessionResolver:
    backend = os.environ.get('THEMIS_AUTHORIZER_BACKEND')
    if backend is None:
        raise SystemExit('THEMIS_AUTHORIZER_BACKEND is required (expected "http" or "fixture")')
    if backend == 'http':
        return session_mod.session_resolver_from_env()
    if backend == 'fixture':
        return session_mod.fixture_session_resolver_from_json(
            os.environ.get(_FIXTURE_CONTEXTS_VAR), var_name=_FIXTURE_CONTEXTS_VAR
        )
    raise SystemExit(f'unsupported THEMIS_AUTHORIZER_BACKEND {backend!r} (expected "http" or "fixture")')


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

    bucket = _require('THEMIS_WORKSPACE_BUCKET')
    return gcs.GcsBackend(storage.Client().bucket(bucket))


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
    return servicer_mod.Limits(**values)


async def _serve() -> None:
    server = grpc.aio.server()
    servicer = servicer_mod.Servicer(build_session_resolver(), build_backend(), build_limits())
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
