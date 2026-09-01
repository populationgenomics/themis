"""Tests for the guest's retrying rpc helper, against a real sync gRPC server.

The server is real, and so are the errors it produces: the whole point of the helper is that it reads the
``grpc.StatusCode`` a failed call carries, which only a genuine ``grpc.RpcError`` has.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import pathlib
import time
from collections.abc import Callable, Iterator, Sequence
from typing import override

import grpc
import pytest

from themis.rpc import hello_pb2, hello_pb2_grpc
from themis.services.sandbox_worker.guest import retry


class _ScriptedHello(hello_pb2_grpc.HelloServicer):
    """Aborts with each scripted status in turn, then serves; records how many calls it saw.

    ``stall`` holds each call open for that many seconds first, standing in for an upstream that
    does not answer.
    """

    def __init__(self, statuses: Sequence[grpc.StatusCode], *, stall: float = 0.0) -> None:
        self._statuses = list(statuses)
        self._stall = stall
        self.calls = 0
        self.deadlines: list[float | None] = []

    @override
    def SayHello(self, request: hello_pb2.SayHelloRequest, context: grpc.ServicerContext) -> hello_pb2.SayHelloResponse:
        self.calls += 1
        self.deadlines.append(context.time_remaining())
        time.sleep(self._stall)
        if self._statuses:
            context.abort(self._statuses.pop(0), 'scripted failure')
        return hello_pb2.SayHelloResponse(greeting=f'hi {request.note}', project_id='p', analysis_id='a')


@contextlib.contextmanager
def _serving(servicer: _ScriptedHello, *, workers: int = 2) -> Iterator[hello_pb2_grpc.HelloStub]:
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=workers))
    hello_pb2_grpc.add_HelloServicer_to_server(servicer, server)
    port = server.add_insecure_port('127.0.0.1:0')
    server.start()
    try:
        with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
            yield hello_pb2_grpc.HelloStub(channel)
    finally:
        server.stop(None)


def _say_hello(
    stub: hello_pb2_grpc.HelloStub,
    *,
    attempts: int = 4,
    timeout: float = retry.DEFAULT_TIMEOUT_S,
    cache_dir: str | pathlib.Path | None = None,
) -> hello_pb2.SayHelloResponse:
    return retry.call(
        stub.SayHello,
        hello_pb2.SayHelloRequest(note='n'),
        attempts=attempts,
        backoff=0.0,
        timeout=timeout,
        cache_dir=cache_dir,
    )


def test_a_transient_failure_is_retried_until_it_succeeds() -> None:
    servicer = _ScriptedHello([grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.UNKNOWN])
    with _serving(servicer) as stub:
        assert _say_hello(stub).greeting == 'hi n'
    assert servicer.calls == 3


@pytest.mark.parametrize(
    'settled',
    [grpc.StatusCode.NOT_FOUND, grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.RESOURCE_EXHAUSTED],
)
def test_a_settled_answer_is_never_retried(settled: grpc.StatusCode) -> None:
    """Answers, not faults: the source holds no record, the request is malformed, the response is too large.

    No rpc here rate-limits its caller, so RESOURCE_EXHAUSTED means the payload exceeded the
    transport limit — re-fetching produces the same oversized response.
    """
    servicer = _ScriptedHello([settled] * 5)
    with _serving(servicer) as stub, pytest.raises(grpc.RpcError) as raised:
        _say_hello(stub)
    assert servicer.calls == 1
    assert raised.value.code() is settled


def test_exhausted_attempts_re_raise_rather_than_swallow() -> None:
    servicer = _ScriptedHello([grpc.StatusCode.UNAVAILABLE] * 5)
    with _serving(servicer) as stub, pytest.raises(grpc.RpcError) as raised:
        _say_hello(stub, attempts=3)
    assert servicer.calls == 3
    assert raised.value.code() is grpc.StatusCode.UNAVAILABLE


class _RecordingClock:
    """Stands in for the ``time`` module: records each wait, and advances a virtual clock by it."""

    def __init__(self) -> None:
        self.slept: list[float] = []
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_backoff_grows_between_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rebind the module's own `time` name; patching `time.sleep` itself would also capture the
    # serving threads' sleeps.
    clock = _RecordingClock()
    monkeypatch.setattr(retry, 'time', clock)
    servicer = _ScriptedHello([grpc.StatusCode.UNAVAILABLE] * 2)
    with _serving(servicer) as stub:
        retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='n'), backoff=0.01)
    assert clock.slept == sorted(clock.slept)
    assert clock.slept[0] < clock.slept[-1]


def test_attempts_below_one_is_a_caller_error() -> None:
    with _serving(_ScriptedHello([])) as stub, pytest.raises(ValueError, match='at least 1'):
        _say_hello(stub, attempts=0)


@pytest.mark.parametrize('as_cache_dir', [pathlib.Path, str], ids=['path', 'str'])
def test_a_cached_response_spares_the_upstream(
    tmp_path: pathlib.Path, as_cache_dir: Callable[[pathlib.Path], str | pathlib.Path]
) -> None:
    # Snippets name the directory inline, so a bare string reaches this as readily as a Path.
    servicer = _ScriptedHello([])
    with _serving(servicer) as stub:
        first = _say_hello(stub, cache_dir=as_cache_dir(tmp_path))
        second = _say_hello(stub, cache_dir=as_cache_dir(tmp_path))
    assert servicer.calls == 1
    assert second == first


def test_a_different_request_is_a_different_cache_entry(tmp_path: pathlib.Path) -> None:
    servicer = _ScriptedHello([])
    with _serving(servicer) as stub:
        first = retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='a'), cache_dir=tmp_path)
        second = retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='b'), cache_dir=tmp_path)
    assert servicer.calls == 2
    assert (first.greeting, second.greeting) == ('hi a', 'hi b')


def test_a_failed_call_leaves_no_cache_entry(tmp_path: pathlib.Path) -> None:
    servicer = _ScriptedHello([grpc.StatusCode.NOT_FOUND])
    with _serving(servicer) as stub:
        with pytest.raises(grpc.RpcError):
            _say_hello(stub, cache_dir=tmp_path)
        assert not list(tmp_path.iterdir())
        assert _say_hello(stub).greeting == 'hi n'


def test_a_call_that_would_never_answer_ends_at_the_deadline() -> None:
    """The gRPC default is infinite, so without this a wedged service is an unkillable wait.

    The snippet gets control back with a status it can print, instead of being killed with the
    results of every call before it in the same script still unreported.
    """
    servicer = _ScriptedHello([], stall=1.0)
    with _serving(servicer) as stub, pytest.raises(grpc.RpcError) as raised:
        _say_hello(stub, timeout=0.1)
    assert raised.value.code() is grpc.StatusCode.DEADLINE_EXCEEDED


def test_a_deadline_this_caller_set_is_not_retried() -> None:
    """Reissuing spends the same budget on the same slow path — four times over, to the same end."""
    servicer = _ScriptedHello([], stall=1.0)
    with _serving(servicer) as stub, pytest.raises(grpc.RpcError):
        _say_hello(stub, timeout=0.1, attempts=4)
    assert servicer.calls == 1


def test_the_budget_covers_the_retries_not_each_attempt_separately() -> None:
    """Four attempts each granted the full budget would overrun it fourfold, which is what it bounds.

    Here the backoff alone outlasts the budget, so the retry is abandoned and the failure that
    actually happened is what the caller sees — not a deadline standing in for it.
    """
    servicer = _ScriptedHello([grpc.StatusCode.UNAVAILABLE] * 5)
    with _serving(servicer) as stub, pytest.raises(grpc.RpcError) as raised:
        retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='n'), timeout=0.3, backoff=0.5)
    assert servicer.calls == 1
    assert raised.value.code() is grpc.StatusCode.UNAVAILABLE


def test_every_call_carries_a_deadline_even_when_the_caller_names_none() -> None:
    """The server sees a deadline on the wire: an unbounded call is not reachable through this helper."""
    servicer = _ScriptedHello([])
    with _serving(servicer) as stub:
        retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='n'))
    [remaining] = servicer.deadlines
    assert remaining is not None
    # gRPC rounds the serialised deadline up, so the server can see marginally more than was asked.
    assert 0 < remaining <= retry.DEFAULT_TIMEOUT_S + 1


def test_a_timeout_at_or_below_zero_is_a_caller_error() -> None:
    with _serving(_ScriptedHello([])) as stub, pytest.raises(ValueError, match='must be positive'):
        _say_hello(stub, timeout=0)


def test_a_negative_backoff_is_a_caller_error() -> None:
    with _serving(_ScriptedHello([])) as stub, pytest.raises(ValueError, match='must not be negative'):
        retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='n'), backoff=-1.0)


def test_each_attempt_is_issued_under_less_time_than_the_one_before() -> None:
    """Attempts share one deadline rather than each renewing it — the invariant `timeout` stands for.

    Were it per-attempt, four attempts would be free to outlast the caller's budget fourfold. The
    deadline each attempt is issued under is asserted here rather than the time the server saw
    remaining, which also carries gRPC's own send timing and is not this helper's to hold.
    """
    servicer = _ScriptedHello([grpc.StatusCode.UNAVAILABLE] * 2)
    issued: list[float] = []

    with _serving(servicer) as stub:

        def recording(request: hello_pb2.SayHelloRequest, timeout: float | None = None) -> hello_pb2.SayHelloResponse:
            assert timeout is not None
            issued.append(timeout)
            return stub.SayHello(request, timeout=timeout)

        retry.call(recording, hello_pb2.SayHelloRequest(note='n'), timeout=10.0, backoff=0.05)

    assert servicer.calls == 3
    assert len(issued) == 3
    assert issued == sorted(issued, reverse=True)
    assert issued[0] > issued[-1]


_TWIN_SERVICE = 'guest_retry_test.Twin'


def _twin(greeting: str) -> grpc.RpcMethodHandler:
    return grpc.unary_unary_rpc_method_handler(
        lambda request, context: hello_pb2.SayHelloResponse(greeting=greeting),  # noqa: ARG005
        request_deserializer=hello_pb2.SayHelloRequest.FromString,
        response_serializer=hello_pb2.SayHelloResponse.SerializeToString,
    )


@contextlib.contextmanager
def _serving_twins() -> Iterator[tuple[grpc.UnaryUnaryMultiCallable, grpc.UnaryUnaryMultiCallable]]:
    """Two unary rpcs on one request type — the shape `store.proto` already has on `google.protobuf.Empty`."""
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler(_TWIN_SERVICE, {'First': _twin('first'), 'Second': _twin('second')}),)
    )
    port = server.add_insecure_port('127.0.0.1:0')
    server.start()
    try:
        with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
            yield tuple(  # pyright: ignore[reportReturnType] — a two-element tuple, by construction
                channel.unary_unary(
                    f'/{_TWIN_SERVICE}/{name}',
                    request_serializer=hello_pb2.SayHelloRequest.SerializeToString,
                    response_deserializer=hello_pb2.SayHelloResponse.FromString,
                )
                for name in ('First', 'Second')
            )
    finally:
        server.stop(None)


def test_two_rpcs_on_one_request_type_do_not_share_a_cache_entry(tmp_path: pathlib.Path) -> None:
    """A request type says nothing about which method received it, so the key cannot be the request alone.

    Sharing one would hand the second rpc the first's response — a wrong answer, of the type the
    cast to `Response` cannot catch.
    """
    request = hello_pb2.SayHelloRequest(note='n')
    with _serving_twins() as (first, second):
        assert retry.call(first, request, cache_dir=tmp_path).greeting == 'first'
        assert retry.call(second, request, cache_dir=tmp_path).greeting == 'second'
    assert len(list(tmp_path.iterdir())) == 2


def test_caching_a_call_that_is_not_a_stub_method_is_refused(tmp_path: pathlib.Path) -> None:
    """No rpc path, no key that can tell this call from another — so it is refused, not keyed on the request."""
    with pytest.raises(TypeError, match='no rpc path'):
        retry.call(
            lambda request, timeout=None: hello_pb2.SayHelloResponse(),  # noqa: ARG005
            hello_pb2.SayHelloRequest(note='n'),
            cache_dir=tmp_path,
        )


@pytest.mark.parametrize('corrupt', ['type', 'payload'], ids=['unregistered-type', 'truncated-body'])
def test_an_entry_this_build_cannot_read_is_a_warned_miss(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], corrupt: str
) -> None:
    """An unreadable entry costs a re-fetch, never the answer — and it says so on stderr.

    A response type renamed since the entry was written, or a body that does not parse, would
    otherwise raise out of every later call: state the cache holds, that clearing it would fix.
    """
    servicer = _ScriptedHello([])
    with _serving(servicer) as stub:
        _say_hello(stub, cache_dir=tmp_path)
        [entry] = list(tmp_path.iterdir())
        name, _, payload = entry.read_bytes().partition(b'\n')
        if corrupt == 'type':
            entry.write_bytes(b'themis.rpc.hello.NoSuchResponse\n' + payload)
        else:
            entry.write_bytes(name + b'\n' + b'\xff\xff\xff\xff')
        capsys.readouterr()
        assert _say_hello(stub, cache_dir=tmp_path).greeting == 'hi n'
    assert servicer.calls == 2
    assert 'themis.agent.retry' in capsys.readouterr().err
    # The bad entry is replaced, so the fault does not repeat on every later call.
    assert _read_entry_type(entry) == 'themis.rpc.hello.SayHelloResponse'


def _read_entry_type(entry: pathlib.Path) -> str:
    return entry.read_bytes().partition(b'\n')[0].decode()


def test_a_cache_that_cannot_be_written_does_not_cost_the_response(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rpc already answered. A full or read-only /workspace must not turn that into a failure."""
    read_only = tmp_path / 'cache'
    read_only.mkdir()
    read_only.chmod(0o500)
    try:
        servicer = _ScriptedHello([])
        with _serving(servicer) as stub:
            assert _say_hello(stub, cache_dir=read_only).greeting == 'hi n'
    finally:
        read_only.chmod(0o700)
    assert not list(read_only.iterdir())
    assert 'not cached' in capsys.readouterr().err


def test_refreshing_an_entry_does_not_shed_a_live_one(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An overwrite replaces an entry rather than adding one, so at the cap it evicts nothing.

    Reached through the unreadable-entry path, which is the only way a write lands on a path the
    cache already holds — a readable entry is returned before any write. Counted as an addition, the
    refresh sheds a live entry every time, and the cache thins out at exactly the point it is useful.
    """
    monkeypatch.setattr(retry, '_MAX_CACHE_ENTRIES', 2)
    servicer = _ScriptedHello([])
    with _serving(servicer) as stub:
        for note in ('a', 'b'):
            retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note=note), cache_dir=tmp_path)
        assert len(list(tmp_path.iterdir())) == 2
        stale = tmp_path / _entry_for(tmp_path, 'a')
        stale.write_bytes(b'themis.rpc.hello.SayHelloResponse\n\xff\xff\xff\xff')
        retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='a'), cache_dir=tmp_path)
        assert servicer.calls == 3
        assert len(list(tmp_path.iterdir())) == 2
        # `b` was neither the target nor an addition, so it is still answered from the cache.
        retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='b'), cache_dir=tmp_path)
    assert servicer.calls == 3


def _entry_for(cache_dir: pathlib.Path, note: str) -> str:
    """The entry holding the response to `SayHello(note=…)`, found by reading, not by re-deriving the key."""
    request = hello_pb2.SayHelloRequest(note=note)
    for entry in cache_dir.iterdir():
        _, _, payload = entry.read_bytes().partition(b'\n')
        if hello_pb2.SayHelloResponse.FromString(payload).greeting == f'hi {request.note}':
            return entry.name
    raise AssertionError(f'no cache entry holds the response to note={note!r}')


def test_the_cache_does_not_grow_without_bound(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It shares `/workspace` with the working tree, and a scratch tree too large to restore is dropped whole.

    So the oldest entries go rather than the budget: the earliest request is answered by the upstream
    again, not from a cache that kept growing.
    """
    monkeypatch.setattr(retry, '_MAX_CACHE_ENTRIES', 3)
    servicer = _ScriptedHello([])
    with _serving(servicer) as stub:
        for index in range(5):
            retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note=str(index)), cache_dir=tmp_path)
        assert servicer.calls == 5
        assert len(list(tmp_path.iterdir())) <= 3
        retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='0'), cache_dir=tmp_path)
    assert servicer.calls == 6


def test_an_entry_evicted_between_callers_is_a_plain_miss(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An entry can go between the decision to read it and the read — one caller's eviction is another's race.

    That is the ordinary course of a shared cache at its cap, so it costs a re-fetch and says nothing:
    a warning per evicted entry would be noise the model has to read past on every call.
    """
    servicer = _ScriptedHello([])
    with _serving(servicer) as stub:
        _say_hello(stub, cache_dir=tmp_path)
        [entry] = list(tmp_path.iterdir())
        entry.unlink()
        capsys.readouterr()
        assert _say_hello(stub, cache_dir=tmp_path).greeting == 'hi n'
    assert servicer.calls == 2
    assert capsys.readouterr().err == ''


def test_an_entry_that_cannot_be_opened_is_a_warned_miss(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unreadable for any reason other than absence is worth saying, and still never worth failing the rpc."""
    servicer = _ScriptedHello([])
    with _serving(servicer) as stub:
        _say_hello(stub, cache_dir=tmp_path)
        [entry] = list(tmp_path.iterdir())
        entry.chmod(0o000)
        try:
            capsys.readouterr()
            assert _say_hello(stub, cache_dir=tmp_path).greeting == 'hi n'
        finally:
            entry.chmod(0o600)
    assert servicer.calls == 2
    assert 'unreadable' in capsys.readouterr().err


def test_callers_sharing_a_cache_at_the_cap_all_get_their_answer(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent snippets share `/workspace`, so eviction runs under other callers' reads and writes.

    Every one of them holds a response the rpc already gave; a cache race must not turn any of those
    into a raised OSError.
    """
    monkeypatch.setattr(retry, '_MAX_CACHE_ENTRIES', 2)
    servicer = _ScriptedHello([])
    # Few distinct requests against a small cap: every read is of an entry another caller is about to
    # evict, which is the window a check-then-read leaves open.
    notes = [str(index % 3) for index in range(240)]

    def _call(note: str) -> str:
        return retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note=note), cache_dir=tmp_path).greeting

    with _serving(servicer, workers=8) as stub, concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        greetings = list(pool.map(_call, notes))
    assert greetings == [f'hi {note}' for note in notes]


def test_a_staging_file_is_neither_an_entry_nor_another_caller_s_to_remove(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staging file is not an entry, and not this caller's to remove.

    Unlinking one fails the rename of the write it belongs to, and counting it against the cap
    starves the cache of a slot for as long as a leaked one sits there.
    """
    monkeypatch.setattr(retry, '_MAX_CACHE_ENTRIES', 2)
    in_flight = tmp_path / 'someone-elses.partial'
    in_flight.write_bytes(b'half a response')
    servicer = _ScriptedHello([])
    with _serving(servicer) as stub:
        for note in ('a', 'b'):
            retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note=note), cache_dir=tmp_path)
        # Both entries fit the cap of 2 because the staging file is not one of them, and it survives.
        assert in_flight.is_file()
        assert len([entry for entry in tmp_path.iterdir() if entry.suffix != '.partial']) == 2
        for note in ('a', 'b'):
            retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note=note), cache_dir=tmp_path)
    assert servicer.calls == 2


def test_the_cache_holds_to_a_byte_budget_too(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, '_MAX_CACHE_BYTES', 1)
    servicer = _ScriptedHello([])
    with _serving(servicer) as stub:
        retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='a'), cache_dir=tmp_path)
        retry.call(stub.SayHello, hello_pb2.SayHelloRequest(note='b'), cache_dir=tmp_path)
    # A single entry already exceeds the budget, so each write clears what came before it.
    assert len(list(tmp_path.iterdir())) == 1
