"""Generic gRPC forward proxy for the agent's internal-service calls (self-hosted-sandbox.md §6).

The agent, in code mode, calls an internal gRPC service through the proxy on localhost. The proxy
forwards to a **fixed upstream** (from config, never client-named), permits only exact
``/pkg.Service/Method`` paths, and injects the per-session token (``x-themis-session-token``) as
metadata — the upstream channel itself carries the job SA's ID token (``authorization``). Messages
pass through as opaque bytes, so a new service is reachable by adding its method to the allowlist, not
by compiling its stub into the proxy.

Register the ``ForwardProxy`` on a ``grpc.aio`` server via ``add_generic_rpc_handlers``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

import grpc
import grpc.aio

_SESSION_TOKEN_METADATA = 'x-themis-session-token'  # noqa: S105 — a metadata key name, not a secret

_StreamBehavior = Callable[[AsyncIterator[bytes], grpc.aio.ServicerContext], AsyncIterator[bytes] | Awaitable[None]]


class ForwardProxy(grpc.GenericRpcHandler):
    """Allowlist the method, inject the session token, and forward opaque bytes to the fixed upstream."""

    def __init__(self, upstream: grpc.aio.Channel, *, allowed_methods: Iterable[str], session_token: str) -> None:
        self._upstream = upstream
        self._allowed = frozenset(allowed_methods)
        self._metadata = ((_SESSION_TOKEN_METADATA, session_token),)

    def service(self, handler_call_details: grpc.HandlerCallDetails) -> grpc.RpcMethodHandler:
        method = handler_call_details.method
        behavior = self._forward(method) if method in self._allowed else _deny
        return grpc.stream_stream_rpc_method_handler(behavior, request_deserializer=None, response_serializer=None)

    def _forward(self, method: str) -> _StreamBehavior:
        async def forward(
            request_iterator: AsyncIterator[bytes], context: grpc.aio.ServicerContext
        ) -> AsyncIterator[bytes]:
            del context
            multicallable = self._upstream.stream_stream(method, request_serializer=None, response_deserializer=None)

            async def requests() -> AsyncIterator[bytes]:
                async for message in request_iterator:
                    yield message

            async for response in multicallable(requests(), metadata=self._metadata):
                yield response

        return forward


async def _deny(request_iterator: AsyncIterator[bytes], context: grpc.aio.ServicerContext) -> None:
    del request_iterator
    await context.abort(grpc.StatusCode.PERMISSION_DENIED, 'method not allowlisted')
