"""The gate over the streaming shapes: a stream-out rpc's context spans its iteration; a stream-in rpc's status lands.

Driven over a real in-process ``grpc.aio`` server with generic handlers at the sheaf contract's paths —
``FetchPack`` streams out, ``Publish`` streams in — since those are the streaming rpcs a gated server
holds. The stream-in cases use the synchronous client, the shape every production caller of a stream-in
rpc has: a status the server sends while that client is still writing reaches it as the status, so each
case asserts the code the client received and how far it had got when it did. The asyncio client is not
used for them: it overwrites an early status with ``INTERNAL`` when its in-flight write then fails
(grpc/grpc#36066; fixed by grpc/grpc#43486), so until a grpcio release carries the fix these cases would
flake under load for the client's reason, not the gate's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator

import grpc
import grpc.aio
import pytest
from google.protobuf import message

from themis.clients.auth import claim as claim_mod
from themis.clients.auth import context as auth_context
from themis.clients.auth import interceptor as interceptor_mod
from themis.rpc import sandbox_options_pb2, sheaf_pb2
from themis.testing import auth as fixture
from themis.testing import in_process_grpc

_SHEAF = 'themis.rpc.sheaf.Sheaf'
_FETCH = f'/{_SHEAF}/FetchPack'
_PUBLISH = f'/{_SHEAF}/Publish'

_CHUNK = sheaf_pb2.PublishRequest(chunk=sheaf_pb2.PublishChunk(pack=0, content=bytes(1 << 20)))
_INTENT = sheaf_pb2.PublishRequest(intent=sheaf_pb2.PublishIntent(base_generation=0))
_FAULTING_INTENT = sheaf_pb2.PublishRequest(intent=sheaf_pb2.PublishIntent(base_generation=7))
# Well past the transport's send window, so the server decides while the client is still writing.
_STREAM_LENGTH = 64

_WORKER = auth_context.AuthContext(
    caller=fixture.SANDBOX_JOB_EMAIL, calling_as=sandbox_options_pb2.CALLING_AS_WORKER_SESSION, session=fixture.SESSION
)


def _serialize(m: message.Message) -> bytes:
    return m.SerializeToString()


class _Seen:
    """What the handlers observed of the calls they served."""

    def __init__(self) -> None:
        self.contexts: list[auth_context.AuthContext] = []
        self.received = 0


def _register(seen: _Seen, *, chunks: int = 3) -> Callable[[grpc.aio.Server], None]:
    async def fetch_pack(
        request: sheaf_pb2.FetchPackRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[sheaf_pb2.PackChunk]:
        del request, context
        # One context read per chunk: the binding has to hold while the response is drawn, not only
        # while the generator is created.
        for _ in range(chunks):
            seen.contexts.append(auth_context.current())
            yield sheaf_pb2.PackChunk(content=b'x')

    async def publish(
        requests: AsyncIterator[sheaf_pb2.PublishRequest], context: grpc.aio.ServicerContext
    ) -> sheaf_pb2.PublishResponse:
        del context
        seen.contexts.append(auth_context.current())
        # Decide on the first message, while the client is still writing the rest.
        first = await anext(requests)
        seen.received += 1
        if first.WhichOneof('message') != 'intent':
            raise interceptor_mod.StatusError(grpc.StatusCode.INVALID_ARGUMENT, 'the first message is the intent')
        if first.intent.base_generation == 7:
            raise RuntimeError('a fault, not a refusal')
        return sheaf_pb2.PublishResponse(generation=1)

    def register(server: grpc.aio.Server) -> None:
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    _SHEAF,
                    {
                        'FetchPack': grpc.unary_stream_rpc_method_handler(
                            fetch_pack, sheaf_pb2.FetchPackRequest.FromString, _serialize
                        ),
                        'Publish': grpc.stream_unary_rpc_method_handler(
                            publish, sheaf_pb2.PublishRequest.FromString, _serialize
                        ),
                    },
                ),
            )
        )

    return register


class _Sender:
    """A publish's client side: the messages it writes, and how many it had written when the call ended."""

    def __init__(self, first: sheaf_pb2.PublishRequest) -> None:
        self._first = first
        self.written = 0

    def stream(self) -> Iterator[sheaf_pb2.PublishRequest]:
        yield self._first
        for _ in range(_STREAM_LENGTH):
            self.written += 1
            yield _CHUNK


def _publish(first: sheaf_pb2.PublishRequest, metadata: fixture.Metadata) -> tuple[grpc.StatusCode, int, _Seen]:
    """One stream-in call from the synchronous client: its status, the chunks written by then, the handler's view."""
    seen = _Seen()
    sender = _Sender(first)
    gate = interceptor_mod.AuthInterceptor(fixture.authorizer())
    with (
        in_process_grpc.serving_in_thread(_register(seen), server_interceptors=(gate,)) as target,
        grpc.insecure_channel(target) as channel,
    ):
        method = channel.stream_unary(
            _PUBLISH,
            request_serializer=sheaf_pb2.PublishRequest.SerializeToString,
            response_deserializer=sheaf_pb2.PublishResponse.FromString,
        )
        try:
            method(sender.stream(), metadata=metadata)
        except grpc.RpcError as e:
            return e.code(), sender.written, seen  # pyright: ignore[reportAttributeAccessIssue]
        return grpc.StatusCode.OK, sender.written, seen


def _fetch(metadata: fixture.Metadata) -> tuple[grpc.StatusCode, _Seen]:
    """One gated stream-out call, read to its end."""
    seen = _Seen()

    async def run() -> grpc.StatusCode:
        gate = interceptor_mod.AuthInterceptor(fixture.authorizer())
        async with in_process_grpc.serving(_register(seen), server_interceptors=(gate,)) as channel:
            method = channel.unary_stream(
                _FETCH,
                request_serializer=sheaf_pb2.FetchPackRequest.SerializeToString,
                response_deserializer=sheaf_pb2.PackChunk.FromString,
            )
            try:
                async for _ in method(sheaf_pb2.FetchPackRequest(pack_id='p'), metadata=claim_mod.pairs(metadata)):
                    pass
            except grpc.aio.AioRpcError as e:
                return e.code()
            return grpc.StatusCode.OK

    return asyncio.run(run()), seen


# --- stream-out ---------------------------------------------------------------------------------------------


def test_the_context_is_bound_for_every_chunk_a_stream_out_rpc_yields() -> None:
    code, seen = _fetch(fixture.WORKER)
    assert code is grpc.StatusCode.OK
    assert seen.contexts == [_WORKER] * 3


def test_a_stream_out_rpc_denies_before_the_first_chunk() -> None:
    code, seen = _fetch(fixture.AGENT)  # the sheaf contract names the worker, not the agent
    assert code is grpc.StatusCode.PERMISSION_DENIED
    assert seen.contexts == []


def test_a_client_cancelling_a_stream_out_rpc_mid_way_leaves_no_unhandled_error_behind() -> None:
    # The generator is finalised in a task other than the one that bound the context; resetting the
    # binding there would raise, unseen by any caller, as "Task exception was never retrieved".
    unhandled: list[str] = []

    def record(loop: asyncio.AbstractEventLoop, ctx: dict[str, object]) -> None:
        del loop
        unhandled.append(str(ctx.get('exception') or ctx.get('message')))

    async def run() -> None:
        asyncio.get_running_loop().set_exception_handler(record)
        gate = interceptor_mod.AuthInterceptor(fixture.authorizer())
        async with in_process_grpc.serving(_register(_Seen(), chunks=100_000), server_interceptors=(gate,)) as channel:
            method = channel.unary_stream(
                _FETCH,
                request_serializer=sheaf_pb2.FetchPackRequest.SerializeToString,
                response_deserializer=sheaf_pb2.PackChunk.FromString,
            )
            call = method(sheaf_pb2.FetchPackRequest(pack_id='p'), metadata=claim_mod.pairs(fixture.WORKER))
            await call.read()
            await call.read()
            call.cancel()
            await asyncio.sleep(0.2)  # the server-side cancellation and any finaliser run here

    asyncio.run(run())
    assert unhandled == []


def test_a_stream_out_handler_that_writes_through_the_context_is_refused_at_registration() -> None:
    # grpc.aio also runs a coroutine that writes with context.write(); the gate does not, and says so on
    # the first call rather than iterating a coroutine.
    async def writing(request: object, context: grpc.aio.ServicerContext) -> None:
        del request
        await context.write(sheaf_pb2.PackChunk(content=b'x'))

    def register(server: grpc.aio.Server) -> None:
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    _SHEAF,
                    {
                        'FetchPack': grpc.unary_stream_rpc_method_handler(
                            writing, sheaf_pb2.FetchPackRequest.FromString, _serialize
                        )
                    },
                ),
            )
        )

    async def run() -> grpc.StatusCode:
        gate = interceptor_mod.AuthInterceptor(fixture.authorizer())
        async with in_process_grpc.serving(register, server_interceptors=(gate,)) as channel:
            method = channel.unary_stream(
                _FETCH,
                request_serializer=sheaf_pb2.FetchPackRequest.SerializeToString,
                response_deserializer=sheaf_pb2.PackChunk.FromString,
            )
            try:
                async for _ in method(
                    sheaf_pb2.FetchPackRequest(pack_id='p'), metadata=claim_mod.pairs(fixture.WORKER)
                ):
                    raise AssertionError('a chunk was served by an ungated handler')
            except grpc.aio.AioRpcError as e:
                return e.code()
            return grpc.StatusCode.OK

    assert asyncio.run(run()) is grpc.StatusCode.UNKNOWN  # the interceptor raised; nothing was served


# --- undeclared paths, streaming shapes ---------------------------------------------------------------------------

_NOWHERE = 'themis.rpc.nowhere.Nothing'


def _register_undeclared(server: grpc.aio.Server) -> None:
    async def out(request: object, context: grpc.aio.ServicerContext) -> AsyncIterator[sheaf_pb2.PackChunk]:
        del request, context
        yield sheaf_pb2.PackChunk(content=b'served')

    async def inward(requests: AsyncIterator[sheaf_pb2.PublishRequest], context: grpc.aio.ServicerContext) -> object:
        del requests, context
        return sheaf_pb2.PublishResponse(generation=1)

    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                _NOWHERE,
                {
                    'Out': grpc.unary_stream_rpc_method_handler(out, sheaf_pb2.FetchPackRequest.FromString, _serialize),
                    'In': grpc.stream_unary_rpc_method_handler(inward, sheaf_pb2.PublishRequest.FromString, _serialize),
                },
            ),
        )
    )


def test_an_undeclared_stream_out_path_is_denied_before_a_chunk_is_served() -> None:
    async def run() -> tuple[grpc.StatusCode, int]:
        gate = interceptor_mod.AuthInterceptor(fixture.authorizer())
        async with in_process_grpc.serving(_register_undeclared, server_interceptors=(gate,)) as channel:
            method = channel.unary_stream(
                f'/{_NOWHERE}/Out',
                request_serializer=sheaf_pb2.FetchPackRequest.SerializeToString,
                response_deserializer=sheaf_pb2.PackChunk.FromString,
            )
            served = 0
            try:
                async for _ in method(sheaf_pb2.FetchPackRequest(pack_id='p'), metadata=claim_mod.pairs(fixture.CLU)):
                    served += 1
            except grpc.aio.AioRpcError as e:
                return e.code(), served
            return grpc.StatusCode.OK, served

    assert asyncio.run(run()) == (grpc.StatusCode.PERMISSION_DENIED, 0)


def test_an_undeclared_stream_in_path_is_denied_to_a_client_still_writing() -> None:
    sender = _Sender(_INTENT)
    gate = interceptor_mod.AuthInterceptor(fixture.authorizer())
    with (
        in_process_grpc.serving_in_thread(_register_undeclared, server_interceptors=(gate,)) as target,
        grpc.insecure_channel(target) as channel,
    ):
        method = channel.stream_unary(
            f'/{_NOWHERE}/In',
            request_serializer=sheaf_pb2.PublishRequest.SerializeToString,
            response_deserializer=sheaf_pb2.PublishResponse.FromString,
        )
        with pytest.raises(grpc.RpcError) as caught:
            method(sender.stream(), metadata=fixture.CLU)
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED  # pyright: ignore[reportAttributeAccessIssue]
    assert sender.written < _STREAM_LENGTH


# --- stream-in ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('metadata', 'code'),
    [
        (fixture.AGENT, grpc.StatusCode.PERMISSION_DENIED),  # the gate's own denial
        ((), grpc.StatusCode.UNAUTHENTICATED),  # no verifiable caller
        (fixture.AGENT_OUTAGE, grpc.StatusCode.UNKNOWN),  # a fault inside the gate: the resolver is down
    ],
)
def test_an_outcome_decided_before_a_byte_is_read_reaches_a_client_still_writing(
    metadata: fixture.Metadata, code: grpc.StatusCode
) -> None:
    got, written, seen = _publish(_INTENT, metadata)
    assert got is code
    assert written < _STREAM_LENGTH, 'the client had finished writing; the status was not early'
    assert seen.received == 0


def test_a_handler_s_refusal_reaches_a_client_still_writing() -> None:
    code, written, seen = _publish(_CHUNK, fixture.WORKER)  # not an intent: refused on the first message
    assert code is grpc.StatusCode.INVALID_ARGUMENT
    assert written < _STREAM_LENGTH
    assert seen.received == 1


def test_a_handler_fault_reaches_a_client_still_writing_as_the_fault_it_is() -> None:
    code, written, _ = _publish(_FAULTING_INTENT, fixture.WORKER)
    assert code is grpc.StatusCode.UNKNOWN  # the raise surfaced as itself, not as a denial
    assert written < _STREAM_LENGTH


def test_a_response_decided_early_reaches_a_client_still_writing() -> None:
    code, written, seen = _publish(_INTENT, fixture.WORKER)
    assert code is grpc.StatusCode.OK
    assert written < _STREAM_LENGTH
    assert seen.received == 1  # the handler read one message and answered; the rest was never read
    assert seen.contexts == [_WORKER]
