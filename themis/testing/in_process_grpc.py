"""Serve gRPC handlers on an in-process ``grpc.aio`` server and hand back a channel to them.

A servicer's behaviour tests drive it over a real server on loopback — real metadata, real status
codes, real serialization — rather than calling its methods with a hand-built context. This module
is that scaffolding, held once: it starts the server, yields a channel, and tears both down. The
caller registers what it serves and wraps the channel in whatever stub it wants, so nothing here
knows a service's protocol.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable

import grpc.aio


@contextlib.asynccontextmanager
async def serving(register: Callable[[grpc.aio.Server], None]) -> AsyncIterator[grpc.aio.Channel]:
    """Serve whatever ``register`` installs on a loopback port; yield a channel to it.

    Args:
        register: Installs the servicer or handlers under test on the server it is given.

    Yields:
        A channel to the running server; it and the server are torn down on exit.
    """
    server = grpc.aio.server()
    register(server)
    port = server.add_insecure_port('127.0.0.1:0')
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f'127.0.0.1:{port}') as channel:
            yield channel
    finally:
        await server.stop(None)
