"""Serve gRPC handlers on an in-process ``grpc.aio`` server and hand back a channel to them.

A servicer's behaviour tests drive it over a real server on loopback — real metadata, real status
codes, real serialization — rather than calling its methods with a hand-built context. This module
is that scaffolding, held once: it starts the server, yields a channel, and tears both down. The
caller registers what it serves and wraps the channel in whatever stub it wants, so nothing here
knows a service's protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import queue
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

import grpc.aio

Register = Callable[[grpc.aio.Server], None] | Callable[[grpc.aio.Server], Awaitable[None]]


@contextlib.asynccontextmanager
async def serving(register: Register) -> AsyncIterator[grpc.aio.Channel]:
    """Serve whatever ``register`` installs on a loopback port; yield a channel to it.

    Args:
        register: Installs the servicer or handlers under test on the server it is given. An async
            ``register`` — one that builds a backend needing ``await`` — is awaited before the server
            starts.

    Yields:
        A channel to the running server; it and the server are torn down on exit.
    """
    server = grpc.aio.server()
    registration = register(server)
    if inspect.isawaitable(registration):
        await registration
    port = server.add_insecure_port('127.0.0.1:0')
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f'127.0.0.1:{port}') as channel:
            yield channel
    finally:
        await server.stop(None)


@contextlib.contextmanager
def serving_in_thread(register: Register) -> Iterator[str]:
    """Serve whatever ``register`` installs on a loopback port, on a thread of its own; yield the target.

    For a synchronous caller — a blocking stub, or a subprocess that dials the port — which a
    ``grpc.aio`` server cannot share a thread with. The server runs its own event loop on a
    daemon thread; exit stops it and joins the thread.

    Yields:
        The ``host:port`` the server listens on.

    Raises:
        Exception: Whatever ``register`` or the server's start raised, re-raised on the caller's thread.
    """
    started: queue.Queue[int | BaseException] = queue.Queue(maxsize=1)
    stopping: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    async def run() -> None:
        server = grpc.aio.server()
        registration = register(server)
        if inspect.isawaitable(registration):
            await registration
        port = server.add_insecure_port('127.0.0.1:0')
        await server.start()
        stopped = asyncio.Event()
        stopping.append((asyncio.get_running_loop(), stopped))
        started.put(port)
        try:
            await stopped.wait()
        finally:
            await server.stop(None)

    def main() -> None:
        try:
            asyncio.run(run())
        except BaseException as exc:
            if stopping:
                raise
            started.put(exc)  # failed before it served: the caller raises it, not this thread

    thread = threading.Thread(target=main, name='in-process-grpc', daemon=True)
    thread.start()
    outcome = started.get()
    if isinstance(outcome, BaseException):
        raise outcome
    try:
        yield f'127.0.0.1:{outcome}'
    finally:
        loop, stopped = stopping[0]
        if not loop.is_closed():  # the server thread may have died already; its own error is the report
            loop.call_soon_threadsafe(stopped.set)
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError('the in-process gRPC server did not stop within 10s')
