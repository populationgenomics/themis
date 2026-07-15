"""Ready stubs for the internal Themis services, reached through the sandbox proxy on localhost.

Each function returns a gRPC stub for one service over a single, lazily-created channel to the proxy's
localhost port. The proxy injects the session token and the callee's identity, so calling code holds no
credentials and no service URL — get a stub, keep it, and call it.
"""

from __future__ import annotations

import functools

import grpc

from themis.rpc import hello_pb2_grpc

_PROXY_TARGET = '127.0.0.1:8081'  # the sandbox proxy's localhost h2c gRPC port


@functools.cache
def _channel() -> grpc.Channel:
    return grpc.insecure_channel(_PROXY_TARGET)


def hello() -> hello_pb2_grpc.HelloStub:
    """``hello``: echo a note against the Analysis the session is bound to (a connectivity check)."""
    return hello_pb2_grpc.HelloStub(_channel())
