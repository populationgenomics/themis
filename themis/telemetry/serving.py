"""Serving a gRPC server until Cloud Run stops it, so the telemetry it holds is exported before the process ends.

Cloud Run sends SIGTERM when it stops an instance, and kills the process ten seconds later. A meter provider exports
on a timer, so whatever the last interval recorded is lost unless the process shuts the provider down in that window.
`serve_until_stopped` turns SIGTERM into a stop: it lets in-flight calls finish within `STOP_GRACE`, then returns, and
the service's entry point shuts its telemetry down within `FLUSH_TIMEOUT` — the two together inside the ten seconds.
"""

from __future__ import annotations

import asyncio
import datetime
import signal
from collections.abc import Iterable

import grpc.aio

# How long in-flight calls may run on after SIGTERM, before they are cancelled.
STOP_GRACE = datetime.timedelta(seconds=5)
# How long each final export, metrics and spans, may take once the server has stopped.
FLUSH_TIMEOUT = datetime.timedelta(seconds=2)


async def serve_until_stopped(server: grpc.aio.Server, *, watch: Iterable[asyncio.Task[None]] = ()) -> None:
    """Wait until the server terminates, SIGTERM arrives or a watched task ends; then stop the server.

    Args:
        server: A started server.
        watch: Background tasks the server cannot serve without, such as a refresher; each only ends by raising, and
            its exception propagates once the server has stopped.

    Raises:
        Exception: Whatever a watched task raised.
    """
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stopping.set)
    terminated = asyncio.create_task(server.wait_for_termination())
    signalled = asyncio.create_task(stopping.wait())
    watched = set(watch)
    try:
        done, _ = await asyncio.wait({terminated, signalled, *watched}, return_when=asyncio.FIRST_COMPLETED)
        for task in done & watched:
            task.result()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        for task in (signalled, *watched):
            task.cancel()
        # Not cancelled first: the server's shutdown awaits the termination this task waits on, and fails once that
        # wait is cancelled. The stop completes it.
        await server.stop(STOP_GRACE.total_seconds())
        await terminated
