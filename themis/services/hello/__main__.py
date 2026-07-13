"""Server entrypoint: build the session resolver from the environment and serve the hello service.

``THEMIS_AUTHORIZER_BACKEND`` selects the authorizer, no silent default: ``http`` resolves each
request's session through the auth service at ``THEMIS_AUTH_URL``; ``fixture`` resolves against a
map seeded from ``THEMIS_HELLO_FIXTURE_CONTEXTS`` (JSON bearer -> binding). ``PORT`` is the Cloud Run
convention; a ``grpc.health.v1`` health service reports SERVING alongside.
"""

from __future__ import annotations

import asyncio
import os

import grpc.aio
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from themis.clients.auth import session as session_mod
from themis.rpc import hello_pb2_grpc
from themis.services.hello import servicer as servicer_mod

_FIXTURE_CONTEXTS_VAR = 'THEMIS_HELLO_FIXTURE_CONTEXTS'


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
    hello_pb2_grpc.add_HelloServicer_to_server(servicer_mod.Servicer(build_session_resolver()), server)
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
