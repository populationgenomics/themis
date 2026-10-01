"""The mark on a servicer's own failure details, and the bound every marked message is held to."""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import override

import grpc
import grpc.aio
import pytest

from themis.common import authored_status, trailer
from themis.rpc import hello_pb2, hello_pb2_grpc
from themis.testing import in_process_grpc


class _Refusing(hello_pb2_grpc.HelloServicer):
    """Refuses every call through the mark, with `details`."""

    def __init__(self, details: str) -> None:
        self._details = details

    @override
    async def SayHello(
        self, request: hello_pb2.SayHelloRequest, context: grpc.aio.ServicerContext
    ) -> hello_pb2.SayHelloResponse:
        await authored_status.abort(context, grpc.StatusCode.INVALID_ARGUMENT, self._details)


def _refused(details: str) -> grpc.aio.AioRpcError:
    async def run() -> grpc.aio.AioRpcError:
        servicer = _Refusing(details)
        async with in_process_grpc.serving(
            lambda server: hello_pb2_grpc.add_HelloServicer_to_server(servicer, server)
        ) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await hello_pb2_grpc.HelloStub(channel).SayHello(hello_pb2.SayHelloRequest(note='n'))
            return caught.value

    return asyncio.run(run())


def _wire_cost(text: str) -> int:
    return len(urllib.parse.quote(text, errors='replace'))


def test_a_marked_failure_carries_its_details_and_the_mark() -> None:
    failure = _refused('PredictDeltas takes variant as chrom-pos-ref-alt')
    assert failure.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert failure.details() == 'PredictDeltas takes variant as chrom-pos-ref-alt'
    assert authored_status.authored(failure)


@pytest.mark.parametrize('filler', ['x' * 40_000, '\U0001f9ec' * 40_000], ids=['ascii', 'four-byte'])
def test_an_over_long_message_is_clipped_to_the_bound_rather_than_dropped(filler: str) -> None:
    """Unclipped, the trailer would exceed the metadata limit and reach the caller as a size error naming nothing."""
    failure = _refused(f'the caller sent {filler}')
    details = failure.details() or ''
    assert failure.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert details.startswith('the caller sent ')
    assert details.endswith('…')
    assert _wire_cost(details) <= trailer.MESSAGE_LIMIT
    assert authored_status.authored(failure)


def test_a_failure_with_no_mark_is_not_read_as_marked() -> None:
    class _Plain(hello_pb2_grpc.HelloServicer):
        @override
        async def SayHello(
            self, request: hello_pb2.SayHelloRequest, context: grpc.aio.ServicerContext
        ) -> hello_pb2.SayHelloResponse:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'grpc wrote this')

    async def run() -> grpc.aio.AioRpcError:
        async with in_process_grpc.serving(
            lambda server: hello_pb2_grpc.add_HelloServicer_to_server(_Plain(), server)
        ) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await hello_pb2_grpc.HelloStub(channel).SayHello(hello_pb2.SayHelloRequest(note='n'))
            return caught.value

    assert not authored_status.authored(asyncio.run(run()))
