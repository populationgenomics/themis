"""The guest's one channel to the hatch, and the deadline it supplies.

An unbounded call is the failure these tests exist for: gRPC's default deadline is infinite, and a snippet that
forgets to pass one hangs until the worker abandons the whole tool call, taking the unprinted results of every call
before it. Guidance in a prompt does not prevent that — the snippet that forgets is the one that hangs — so the
deadline is the channel's, and a bare stub call is what gets tested.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import pathlib
import shutil
import tempfile
import time
from collections.abc import Iterator
from typing import override

import grpc
import pytest

from themis.rpc import hello_pb2, hello_pb2_grpc
from themis.services.sandbox_worker import worker
from themis.services.sandbox_worker.guest import channel, retry, services

# An AF_UNIX path is capped near 104 characters, which pytest's `tmp_path` alone overruns.
_SHORT_TMP_ROOT = '/tmp'  # noqa: S108


class _DeadlineReporting(hello_pb2_grpc.HelloServicer):
    """Records the time each call had left, so the deadline that reached the wire is readable."""

    def __init__(self, *, stall: float = 0.0) -> None:
        self.remaining: list[float | None] = []
        self.metadata: list[tuple[str, str | bytes]] = []
        self._stall = stall

    @override
    def SayHello(self, request: hello_pb2.SayHelloRequest, context: grpc.ServicerContext) -> hello_pb2.SayHelloResponse:
        self.remaining.append(context.time_remaining())
        self.metadata.extend((key, value) for key, value in context.invocation_metadata() if key == 'x-probe')
        time.sleep(self._stall)
        return hello_pb2.SayHelloResponse(greeting=f'hi {request.note}')


@contextlib.contextmanager
def _hatch(servicer: _DeadlineReporting, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Serve `hello` on a Unix socket and point `POSTERN_HATCH` at it, as the sandbox does."""
    directory = pathlib.Path(tempfile.mkdtemp(prefix='hatch-', dir=_SHORT_TMP_ROOT))
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=2))
    hello_pb2_grpc.add_HelloServicer_to_server(servicer, server)
    server.add_insecure_port(f'unix:{directory / "s"}')
    server.start()
    monkeypatch.setenv('POSTERN_HATCH', str(directory / 's'))
    try:
        yield
    finally:
        server.stop(None)
        shutil.rmtree(directory, ignore_errors=True)


def test_a_bare_stub_call_carries_a_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No timeout named anywhere: the channel supplies one, so an unbounded call is unreachable."""
    servicer = _DeadlineReporting()
    with _hatch(servicer, monkeypatch):
        stub = hello_pb2_grpc.HelloStub(channel.to_hatch())
        assert stub.SayHello(hello_pb2.SayHelloRequest(note='n')).greeting == 'hi n'
    [remaining] = servicer.remaining
    assert remaining is not None
    # gRPC rounds the serialised deadline up, so the server can see marginally more than was asked.
    assert 0 < remaining <= channel.DEFAULT_TIMEOUT_S + 1


def test_a_caller_who_names_a_deadline_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The channel fills a gap; it never overrides. `retry.call` sets its own budget this way."""
    servicer = _DeadlineReporting()
    with _hatch(servicer, monkeypatch):
        hello_pb2_grpc.HelloStub(channel.to_hatch()).SayHello(hello_pb2.SayHelloRequest(note='n'), timeout=5.0)
    [remaining] = servicer.remaining
    assert remaining is not None
    # The caller's 5 s reached the wire, not the channel's default. gRPC rounds the serialised
    # deadline up, so the server can see marginally more than was asked.
    assert 0 < remaining <= 6.0
    assert remaining < channel.DEFAULT_TIMEOUT_S


def test_a_call_that_outlasts_the_default_is_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deadline is enforced, not merely announced: a wedged service returns control to the snippet."""
    monkeypatch.setattr(channel, 'DEFAULT_TIMEOUT_S', 0.1)
    with _hatch(_DeadlineReporting(stall=1.0), monkeypatch), pytest.raises(grpc.RpcError) as raised:
        hello_pb2_grpc.HelloStub(channel.to_hatch()).SayHello(hello_pb2.SayHelloRequest(note='n'))
    assert raised.value.code() is grpc.StatusCode.DEADLINE_EXCEEDED


def test_the_default_stays_under_the_tool_call_it_runs_inside() -> None:
    """It has to leave the snippet time to catch the failure and print what it did get.

    A default at or above what the worker allows one `shell` call would expire only once the results
    were already lost — so the two constants are asserted against each other, not against a number.
    """
    assert channel.DEFAULT_TIMEOUT_S < worker._TOOL_TIMEOUT_S


def test_the_retry_budget_is_the_channel_default() -> None:
    """One quantity — what a snippet may spend inside one tool call — so it is named in one place."""
    assert retry.DEFAULT_TIMEOUT_S == channel.DEFAULT_TIMEOUT_S


def test_metadata_survives_the_interceptor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Filling in a deadline rebuilds the call details, so everything else on them has to be carried over.

    Request metadata is what the hatch reads to authenticate a forwarded call; dropping it here would
    break every rpc while leaving the deadline looking right.
    """
    servicer = _DeadlineReporting()
    with _hatch(servicer, monkeypatch):
        stub = hello_pb2_grpc.HelloStub(channel.to_hatch())
        stub.SayHello(hello_pb2.SayHelloRequest(note='n'), metadata=(('x-probe', 'seen'),))
    assert servicer.metadata == [('x-probe', 'seen')]


def test_retry_call_works_over_the_intercepted_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production composition: `retry.call` reads the rpc path off a stub the interceptor wrapped.

    An intercepted multicallable reports that path as `str` where a bare one reports `bytes`, so a
    key derived on one and looked up on the other would miss every time.
    """
    servicer = _DeadlineReporting()
    with _hatch(servicer, monkeypatch):
        stub = services.hello()
        cache = pathlib.Path(tempfile.mkdtemp(prefix='cache-', dir=_SHORT_TMP_ROOT))
        try:
            first = retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='n'), cache_dir=cache)
            second = retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='n'), cache_dir=cache)
            [entry] = list(cache.iterdir())
            assert entry.name.startswith('themis.rpc.hello.SayHelloRequest.')
        finally:
            shutil.rmtree(cache, ignore_errors=True)
    assert (first.greeting, second.greeting) == ('hi n', 'hi n')
    assert len(servicer.remaining) == 1  # the second call was the cache's


def test_a_streaming_call_is_bounded_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every call shape is intercepted, so exposing a streaming rpc cannot quietly reintroduce an unbounded call."""
    seen: list[float | None] = []

    def _stream(
        request: hello_pb2.SayHelloRequest,  # noqa: ARG001 — the servicer signature grpc calls
        context: grpc.ServicerContext,
    ) -> Iterator[hello_pb2.SayHelloResponse]:
        seen.append(context.time_remaining())
        yield hello_pb2.SayHelloResponse(greeting='streamed')

    directory = pathlib.Path(tempfile.mkdtemp(prefix='hatch-', dir=_SHORT_TMP_ROOT))
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                'guest_services_test.Streaming',
                {
                    'Listen': grpc.unary_stream_rpc_method_handler(
                        _stream,
                        request_deserializer=hello_pb2.SayHelloRequest.FromString,
                        response_serializer=hello_pb2.SayHelloResponse.SerializeToString,
                    )
                },
            ),
        )
    )
    server.add_insecure_port(f'unix:{directory / "s"}')
    server.start()
    monkeypatch.setenv('POSTERN_HATCH', str(directory / 's'))
    try:
        listen = channel.to_hatch().unary_stream(
            '/guest_services_test.Streaming/Listen',
            request_serializer=hello_pb2.SayHelloRequest.SerializeToString,
            response_deserializer=hello_pb2.SayHelloResponse.FromString,
        )
        assert [reply.greeting for reply in listen(hello_pb2.SayHelloRequest(note='n'))] == ['streamed']
    finally:
        server.stop(None)
        shutil.rmtree(directory, ignore_errors=True)
    [remaining] = seen
    assert remaining is not None
    assert 0 < remaining <= channel.DEFAULT_TIMEOUT_S + 1
