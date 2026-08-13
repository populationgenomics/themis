"""Server entrypoint: attach every interface the image serves to one gRPC server.

``INTERFACES`` is the image's composition; each entry builds its own backend and installs its servicer,
so ``PORT`` (the Cloud Run convention) is the only env var read here. The ``grpc.health.v1`` service
reports SERVING for the server as a whole, with no per-interface entry: an interface that cannot build
its backend exits the process, so the server never serves a partial set.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable

import grpc.aio
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from themis.services.evidence.literature import interface as literature_interface

# An interface registers on the server and hands the stack the clients it holds for the server's
# lifetime. No service here installs a SIGTERM handler, so Cloud Run's stop kills the process and the
# stack unwinds only when a later interface fails to build — an async `register` is what lets an
# adapter be built with `await` at all, and graceful drain is a separate, service-wide change.
Register = Callable[[grpc.aio.Server, contextlib.AsyncExitStack], Awaitable[None]]

INTERFACES: tuple[Register, ...] = (literature_interface.register,)


async def _serve() -> None:
    server = grpc.aio.server()
    async with contextlib.AsyncExitStack() as stack:
        for register in INTERFACES:
            await register(server, stack)
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
