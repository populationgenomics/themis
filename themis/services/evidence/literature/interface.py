"""How the literature interface attaches to the evidence image's server.

``register`` is the entrypoint's whole view of literature. It must run before the server starts —
a gRPC server rejects a handler added after that.
"""

from __future__ import annotations

import contextlib

import grpc.aio

from themis.rpc import literature_pb2_grpc
from themis.services.evidence.literature import config, servicer


async def register(server: grpc.aio.Server, stack: contextlib.AsyncExitStack) -> None:
    """Install the ``Literature`` servicer, over the env-selected backend, on ``server``.

    Args:
        server: The image's server, not yet started.
        stack: Owns the backend's client for as long as the server runs. Nothing runs a service's
            SIGTERM to ground, so in practice it unwinds only when a later interface fails to build
            (see ``__main__``).
    """
    backend = config.backend_from_env(stack)
    literature_pb2_grpc.add_LiteratureServicer_to_server(servicer.Servicer(backend), server)
