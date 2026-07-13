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
import json
import os

import grpc.aio
from google.protobuf import json_format
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from themis.clients.auth import session as session_mod
from themis.rpc import auth_pb2, store_pb2_grpc
from themis.services.store import servicer as servicer_mod
from themis.services.store import storage as storage_mod


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
        return _fixture_session_resolver_from_env()
    raise SystemExit(f'unsupported THEMIS_AUTHORIZER_BACKEND {backend!r} (expected "http" or "fixture")')


def _fixture_session_resolver_from_env() -> session_mod.SessionResolver:
    """Build an offline session resolver from ``THEMIS_STORE_FIXTURE_CONTEXTS``.

    A JSON object mapping each plaintext bearer to its binding, e.g.
    ``{"tok": {"project_id": "p1", "analysis_id": "a1"}}``. Required — an unset var is an operator
    error; pass ``{}`` for an explicit empty set. The session resolver raises ``UnresolvedSessionError`` on
    a token it does not hold, matching the http backend.
    """
    raw = os.environ.get('THEMIS_STORE_FIXTURE_CONTEXTS')
    if raw is None:
        raise SystemExit(
            'THEMIS_STORE_FIXTURE_CONTEXTS is required for the fixture authorizer: a JSON object of '
            'bearer -> binding, or "{}" for an explicit empty set'
        )
    try:
        seeds = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f'THEMIS_STORE_FIXTURE_CONTEXTS is not valid JSON: {e}') from e
    if not isinstance(seeds, dict):
        raise SystemExit(
            f'THEMIS_STORE_FIXTURE_CONTEXTS must be a JSON object of bearer -> binding, got {type(seeds).__name__}'
        )
    contexts = {token: _parse_binding(binding) for token, binding in seeds.items()}

    async def session_resolver(session_token: str) -> auth_pb2.SessionContext:
        try:
            return contexts[session_token]
        except KeyError:
            raise session_mod.UnresolvedSessionError from None

    return session_resolver


def _parse_binding(binding: object) -> auth_pb2.SessionContext:
    """Parse and validate one fixture binding into a ``SessionContext`` (fail-loud)."""
    if not isinstance(binding, dict):
        raise SystemExit('THEMIS_STORE_FIXTURE_CONTEXTS binding must be a JSON object')
    try:
        context = json_format.ParseDict(binding, auth_pb2.SessionContext())
    except json_format.ParseError as e:
        raise SystemExit(f'THEMIS_STORE_FIXTURE_CONTEXTS binding is malformed: {e}') from e
    if not context.project_id or not context.analysis_id:
        raise SystemExit('THEMIS_STORE_FIXTURE_CONTEXTS binding must set project_id and analysis_id')
    return context


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
