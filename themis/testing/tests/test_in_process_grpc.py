"""The threaded in-process server: a failure to start is the caller's, and a served port is reachable."""

from __future__ import annotations

import grpc
import grpc.aio
import pytest

from themis.testing import in_process_grpc


def test_a_register_that_raises_fails_the_caller_not_the_thread() -> None:
    def register(_server: grpc.aio.Server) -> None:
        raise RuntimeError('nothing to serve')

    with pytest.raises(RuntimeError, match='nothing to serve'), in_process_grpc.serving_in_thread(register):
        pass


def test_the_served_port_accepts_a_connection_and_is_released_on_exit() -> None:
    with in_process_grpc.serving_in_thread(lambda _server: None) as target, grpc.insecure_channel(target) as channel:
        grpc.channel_ready_future(channel).result(timeout=10)
    with grpc.insecure_channel(target) as channel, pytest.raises(grpc.FutureTimeoutError):
        grpc.channel_ready_future(channel).result(timeout=1)
