"""SIGTERM stops the server, so the entry point gets to export; a failed background task stops it too."""

from __future__ import annotations

import asyncio
import os
import signal

import grpc.aio
import pytest

from themis.telemetry import serving

_SETTLE_SECONDS = 5.0


async def _started() -> grpc.aio.Server:
    server = grpc.aio.server()
    server.add_insecure_port('127.0.0.1:0')
    await server.start()
    return server


def test_sigterm_stops_the_server_and_returns() -> None:
    async def run() -> None:
        server = await _started()
        loop = asyncio.get_running_loop()
        # Once the loop is serving, signal this process: the handler the loop installs takes it, not the default.
        loop.call_later(0.1, os.kill, os.getpid(), signal.SIGTERM)
        async with asyncio.timeout(_SETTLE_SECONDS):
            await serving.serve_until_stopped(server)
        # Stopped: a stopped server terminates at once.
        async with asyncio.timeout(_SETTLE_SECONDS):
            await server.wait_for_termination()

    asyncio.run(run())


def test_the_handler_is_removed_once_serving_ends() -> None:
    async def run() -> None:
        server = await _started()
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, os.kill, os.getpid(), signal.SIGTERM)
        await serving.serve_until_stopped(server)
        assert not loop.remove_signal_handler(signal.SIGTERM)

    asyncio.run(run())


def test_a_failed_background_task_stops_the_server_and_raises() -> None:
    async def fail() -> None:
        await asyncio.sleep(0.05)
        raise RuntimeError('the refresher gave up')

    async def run() -> None:
        server = await _started()
        with pytest.raises(RuntimeError, match='refresher'):
            async with asyncio.timeout(_SETTLE_SECONDS):
                await serving.serve_until_stopped(server, watch=[asyncio.create_task(fail())])
        async with asyncio.timeout(_SETTLE_SECONDS):
            await server.wait_for_termination()

    asyncio.run(run())
