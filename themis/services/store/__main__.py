"""Server entrypoint: build the backends from the environment and serve the gRPC service.

Two ports, each backend selected by a required env var (no silent default).
``THEMIS_STORAGE_BACKEND``: ``gcs`` (two buckets, from ``THEMIS_STORE_*_BUCKET``) or ``fixture``
(in-memory). ``THEMIS_AUTHORIZER_BACKEND``: ``http`` (resolve each request's session through the
auth service at ``THEMIS_AUTH_URL``) or ``fixture`` (a lookup seeded from
``THEMIS_STORE_FIXTURE_CONTEXTS``). ``PORT`` is the Cloud Run convention. A ``grpc.health.v1``
health service reports SERVING alongside.
"""

from __future__ import annotations

import asyncio
import os

import grpc.aio
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from themis.clients.auth import session as session_mod
from themis.rpc import store_pb2_grpc
from themis.services.store import servicer as servicer_mod
from themis.services.store import storage as storage_mod

_FIXTURE_CONTEXTS_VAR = 'THEMIS_STORE_FIXTURE_CONTEXTS'


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


def build_storage() -> storage_mod.Storage:
    backend = os.environ.get('THEMIS_STORAGE_BACKEND')
    if backend is None:
        raise SystemExit('THEMIS_STORAGE_BACKEND is required (expected "gcs" or "fixture")')
    if backend == 'gcs':
        from themis.services.store import gcs  # noqa: PLC0415 — deferred so the fixture path skips google-cloud-storage

        return gcs.GcsStorage(
            working_document_bucket=_require('THEMIS_STORE_WORKING_DOCUMENT_BUCKET'),
            workspace_bucket=_require('THEMIS_STORE_WORKSPACE_BUCKET'),
        )
    if backend == 'fixture':
        return storage_mod.FixtureStorage()
    raise SystemExit(f'unsupported THEMIS_STORAGE_BACKEND {backend!r} (expected "gcs" or "fixture")')


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


async def _serve() -> None:
    server = grpc.aio.server()
    store_pb2_grpc.add_StoreServicer_to_server(servicer_mod.Servicer(build_storage(), build_session_resolver()), server)
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
