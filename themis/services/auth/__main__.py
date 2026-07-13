"""Server entrypoint: build the backend from the environment and serve the gRPC service.

``THEMIS_BACKEND`` selects the adapter (required — no silent default): ``cloudsql`` (the
deployed backend, reading the ``session_context`` table via the Cloud SQL connector) or
``fixture`` (offline runs, seeded from ``THEMIS_FIXTURE_BINDINGS``). ``PORT`` is the Cloud
Run convention. A ``grpc.health.v1`` health service reports SERVING alongside.
"""

from __future__ import annotations

import asyncio
import json
import os

import grpc.aio
from google.protobuf import json_format
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from themis.rpc import auth_pb2, auth_pb2_grpc
from themis.services.auth import backend as backend_mod
from themis.services.auth import servicer as servicer_mod


def build_backend() -> backend_mod.SessionBackend:
    backend = os.environ.get('THEMIS_BACKEND')
    if backend is None:
        raise SystemExit('THEMIS_BACKEND is required (expected "cloudsql" or "fixture")')
    if backend == 'cloudsql':
        return _cloudsql_backend_from_env()
    if backend == 'fixture':
        return _fixture_backend_from_env()
    raise SystemExit(f'unsupported THEMIS_BACKEND {backend!r} (expected "cloudsql" or "fixture")')


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


def _cloudsql_backend_from_env() -> backend_mod.SessionBackend:
    from themis.services.auth import cloudsql  # noqa: PLC0415 — deferred so the fixture path skips the connector import

    return cloudsql.CloudSqlBackend(
        connection_name=_require('THEMIS_SQL_CONNECTION_NAME'),
        database=_require('THEMIS_SQL_DATABASE'),
        iam_user=_require('THEMIS_SQL_IAM_USER'),
    )


def _fixture_backend_from_env() -> backend_mod.FixtureBackend:
    """Build the offline backend from ``THEMIS_FIXTURE_BINDINGS``.

    A JSON object mapping each plaintext session token to its binding, e.g.
    ``{"tok-abc": {"project_id": "p1", "analysis_id": "a1"}}``; tokens are hashed on load
    (the backend holds only hashes, never plaintext). Required — an unset var is an operator
    error; pass ``{}`` for an explicit empty store (the image default until the deploy
    overrides it).
    """
    raw = os.environ.get('THEMIS_FIXTURE_BINDINGS')
    if raw is None:
        raise SystemExit(
            'THEMIS_FIXTURE_BINDINGS is required for the fixture backend: a JSON object of '
            'token -> binding, or "{}" for an explicit empty store'
        )
    try:
        seeds = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f'THEMIS_FIXTURE_BINDINGS is not valid JSON: {e}') from e
    if not isinstance(seeds, dict):
        raise SystemExit(
            f'THEMIS_FIXTURE_BINDINGS must be a JSON object of token -> binding, got {type(seeds).__name__}'
        )
    bindings = {backend_mod.hash_token(token): _parse_binding(token, binding) for token, binding in seeds.items()}
    return backend_mod.FixtureBackend(bindings)


def _parse_binding(token: str, binding: object) -> auth_pb2.SessionContext:
    """Parse and validate one fixture binding into a ``SessionContext`` (fail-loud)."""
    digest = backend_mod.hash_token(token)[:8]
    if not isinstance(binding, dict):
        raise SystemExit(f'THEMIS_FIXTURE_BINDINGS binding (token sha256:{digest}…) must be a JSON object')
    try:
        context = json_format.ParseDict(binding, auth_pb2.SessionContext())
    except json_format.ParseError as e:
        raise SystemExit(f'THEMIS_FIXTURE_BINDINGS binding (token sha256:{digest}…) is malformed: {e}') from e
    if not context.project_id or not context.analysis_id:
        raise SystemExit(
            f'THEMIS_FIXTURE_BINDINGS binding (token sha256:{digest}…) must set project_id and analysis_id'
        )
    return context


async def _serve() -> None:
    server = grpc.aio.server()
    auth_pb2_grpc.add_AuthServicer_to_server(servicer_mod.Servicer(build_backend()), server)
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
