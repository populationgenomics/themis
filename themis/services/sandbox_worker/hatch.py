"""The gRPC hatch's hello forwarder: the guest's allowlisted door to the hello service (postern-sandbox-swap.md §4).

The forwarding servicer runs in the trusted worker process: it injects the per-session token and forwards an
allowlisted call to the real service, so the guest never holds a credential and never names an upstream. The hatch
server is synchronous (postern's ``grpc.server``), so the forward stub dials over a synchronous channel — distinct from
the worker's own async checkpoint channel.
"""

from __future__ import annotations

import grpc
from postern import grpc as postern_grpc

from themis.rpc import hello_pb2, hello_pb2_grpc

_SESSION_TOKEN_METADATA = 'x-themis-session-token'  # noqa: S105 — a metadata key name, not a secret
_HELLO = 'themis.rpc.hello.Hello'

# The only method the guest may reach: hello's connectivity check. The store is worker-only — the working
# document and scratch are checkpointed/restored by the worker, never over the hatch — so the guest gets no
# store method (a guest-facing store API, if ever wanted, must be designed scoped and size-capped).
# TODO: generate GUEST_METHODS (and guest/services.py's stubs) from the proto so the allowlist matches the
# exposed RPCs by construction rather than by hand.
GUEST_METHODS = frozenset({f'/{_HELLO}/SayHello'})


class HelloForwarder(hello_pb2_grpc.HelloServicer):
    """Forward the guest's allowlisted hello call to the real hello service, session-token-injected (sync)."""

    def __init__(self, channel: grpc.Channel, *, session_token: str) -> None:
        self._stub = hello_pb2_grpc.HelloStub(channel)
        self._metadata = ((_SESSION_TOKEN_METADATA, session_token),)

    def SayHello(  # noqa: N802 — mirrors the proto service's PascalCase rpc name (generated base)
        self, request: hello_pb2.SayHelloRequest, context: grpc.ServicerContext
    ) -> hello_pb2.SayHelloResponse:
        del context
        return self._stub.SayHello(request, metadata=self._metadata)


def build_hatch(hello_channel: grpc.Channel, *, session_token: str) -> postern_grpc.GrpcHatch:
    """A hatch exposing the allowlisted hello method, forwarded with the session token injected."""
    hatch = postern_grpc.GrpcHatch(allowlist=GUEST_METHODS)
    hatch.add_servicer(
        hello_pb2_grpc.add_HelloServicer_to_server,
        HelloForwarder(hello_channel, session_token=session_token),
    )
    return hatch
