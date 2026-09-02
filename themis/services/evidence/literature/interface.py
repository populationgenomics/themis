"""How the literature interface attaches to the evidence image's server.

`register` is the entrypoint's whole view of literature. It must run before the server starts —
a gRPC server rejects a handler added after that.
"""

from __future__ import annotations

import grpc.aio

from themis.rpc import literature_pb2_grpc
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.literature import config, servicer


async def register(server: grpc.aio.Server, deps: deps_mod.Deps) -> None:
    """Install the `Literature` servicer, over the env-selected backend, on `server`.

    Args:
        server: The image's server, not yet started.
        deps: The image's collaborators. `deps.stack` owns the live backend's GCS client and Cloud
            SQL connector for as long as the server runs — nothing runs a service's SIGTERM to
            ground, so in practice it unwinds only when a later interface fails to build (see
            `__main__`) — and `deps.http_client` is what it calls the upstream indexes on.
            `deps.session_resolver` gates one step of one rpc: the reads here authenticate no
            caller at all, which is why this file stays outside the agent hatch — exposure requires
            the gate at every door (`sandbox-rpc-exposure.md`) — but the conversion
            `MaybeIngestPapers` enqueues spends Anthropic tokens, and that is not a cost an
            unauthorized caller may incur.
    """
    literature_pb2_grpc.add_LiteratureServicer_to_server(
        servicer.Servicer(config.backend_from_env(deps), deps.session_resolver), server
    )
