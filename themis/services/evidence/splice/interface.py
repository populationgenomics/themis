"""How the splice interface attaches to the evidence image's server."""

from __future__ import annotations

import grpc.aio

from themis.rpc import splice_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.splice import config, servicer


async def register(server: grpc.aio.Server, deps: deps_mod.Deps) -> None:
    """Install the `Splice` servicer, over the env-selected backend, on `server`.

    Args:
        server: The image's server, not yet started — a gRPC server rejects a handler added after that.
        deps: The image's session resolver and shared HTTP client.
    """
    backend = config.backend_from_env(deps)
    splice_pb2_grpc.add_SpliceServicer_to_server(servicer.Servicer(backend, deps.session_resolver), server)
