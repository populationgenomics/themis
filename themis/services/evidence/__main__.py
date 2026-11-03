"""Server entrypoint: attach every interface the image serves to one gRPC server.

`INTERFACES` is the image's composition; each entry builds its own backend and installs its servicer,
so `PORT` (the Cloud Run convention) is the only env var read here. `Deps` is what the image builds
once — the authorizer the server is gated by, and the shared HTTP client each interface is handed —
being the image's concern rather than any one interface's (see `deps`). The server is built by
`interceptor.gated_server`, so the auth interceptor gates every rpc of every interface before its
handler runs (rpc-authorization.md); the `grpc.health.v1` service is its one exemption, and reports
SERVING for the server as a whole, with no per-interface entry: an interface that cannot build its
backend exits the process, so the server never serves a partial set. Deployed (`THEMIS_AUTHORIZER_BACKEND`
`http`), the server records each call's duration and status as a metric; on SIGTERM it stops, exports
them, and unwinds what the interfaces hold open.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable

import grpc.aio
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from opentelemetry.sdk import metrics as sdk_metrics

from themis.clients.auth import interceptor as interceptor_mod
from themis.services.evidence import deps as deps_mod
from themis.services.evidence.clinvar import interface as clinvar_interface
from themis.services.evidence.cspec import interface as cspec_interface
from themis.services.evidence.gene_disease import interface as gene_disease_interface
from themis.services.evidence.gnomad import interface as gnomad_interface
from themis.services.evidence.literature import interface as literature_interface
from themis.services.evidence.mavedb import interface as mavedb_interface
from themis.services.evidence.splice import interface as splice_interface
from themis.services.evidence.transcript import interface as transcript_interface
from themis.services.evidence.variant import interface as variant_interface
from themis.services.evidence.vep import interface as vep_interface
from themis.telemetry import metrics as telemetry_metrics
from themis.telemetry import rpc_metrics, serving

# The Cloud Run service's name: the metrics' `job` label.
SERVICE_NAME = 'themis-evidence'

# An interface registers on the server and builds its own adapter from `deps`, whose exit stack holds
# whatever that adapter keeps open for the server's lifetime; an async `register` is what lets an adapter
# be built with `await` at all.
Register = Callable[[grpc.aio.Server, deps_mod.Deps], Awaitable[None]]

INTERFACES: tuple[Register, ...] = (
    literature_interface.register,
    variant_interface.register,
    vep_interface.register,
    gnomad_interface.register,
    clinvar_interface.register,
    gene_disease_interface.register,
    transcript_interface.register,
    splice_interface.register,
    mavedb_interface.register,
    cspec_interface.register,
)


def install_metrics() -> sdk_metrics.MeterProvider | None:
    """The meter provider when deployed (`THEMIS_AUTHORIZER_BACKEND` `http`); a fixture run records nothing."""
    if os.environ.get(interceptor_mod.AUTHORIZER_VAR) == 'http':
        return telemetry_metrics.service_meter_provider(SERVICE_NAME)
    return None


async def _serve() -> None:
    meter_provider = install_metrics()
    meter = telemetry_metrics.service_meter(meter_provider)
    async with contextlib.AsyncExitStack() as stack:
        # The final export runs before the stack closes what the interfaces hold open, so it falls inside the seconds
        # Cloud Run leaves a stopping instance rather than after their teardown.
        try:
            deps = await deps_mod.deps_from_env(stack)
            server = interceptor_mod.gated_server(deps.authorizer, observers=[rpc_metrics.RpcMetrics(meter)])
            for register in INTERFACES:
                await register(server, deps)
            # grpc_health ships no py.typed; `health.aio` is a runtime re-export pyright can't see.
            health_servicer = health.aio.HealthServicer()  # pyright: ignore[reportAttributeAccessIssue]
            await health_servicer.set('', health_pb2.HealthCheckResponse.SERVING)
            health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
            server.add_insecure_port(f'[::]:{os.environ.get("PORT", "8080")}')  # TLS terminated by Cloud Run
            await server.start()
            await serving.serve_until_stopped(server)
        finally:
            if meter_provider is not None:
                meter_provider.shutdown(timeout_millis=serving.FLUSH_TIMEOUT.total_seconds() * 1000)


def main() -> None:
    # `httpx2` at INFO is the record of every upstream call: method, full URL, status. Root stays at
    # WARNING so google.auth, grpc and urllib3 do not come with it.
    logging.basicConfig(level=logging.WARNING)
    level = os.environ.get('THEMIS_LOG', 'INFO')
    logging.getLogger('themis').setLevel(level)
    logging.getLogger('httpx2').setLevel(level)
    asyncio.run(_serve())


if __name__ == '__main__':
    main()
