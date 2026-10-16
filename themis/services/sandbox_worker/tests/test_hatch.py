"""Tests for the hatch forwarders: allowlist scope, session-token injection, upstream status propagation.

All three are checked against the proto descriptors, not a literal method list, so a service that adds an rpc
surfaces here — the allowlist must cover it, and its forwarder must inject the token and carry an upstream
status through — rather than silently widening or drifting.
"""

from __future__ import annotations

import concurrent.futures
import inspect
import threading
import time
from collections.abc import Callable, Iterator
from typing import NoReturn, Protocol, cast, override

import grpc
import pytest
from google.protobuf import descriptor as protobuf_descriptor

from themis.clients.auth import claim as claim_mod
from themis.common import authored_status
from themis.rpc import (
    clinvar_pb2,
    cspec_pb2,
    gene_disease_pb2,
    gnomad_pb2,
    gnomad_pb2_grpc,
    hello_pb2,
    literature_pb2,
    literature_pb2_grpc,
    mavedb_pb2,
    sandbox_options_pb2,
    splice_pb2,
    transcript_pb2,
    variant_pb2,
    vep_pb2,
)
from themis.services.sandbox_worker import hatch, tool
from themis.services.sandbox_worker.guest import services

_TOKEN = 'TOK'


class _ForwarderClass(Protocol):
    """A forwarding servicer's constructor: the channel it forwards over, and the token it injects."""

    def __call__(self, channel: grpc.Channel, *, session_token: str) -> object: ...


def _service(
    file_descriptor: protobuf_descriptor.FileDescriptor, service_full_name: str
) -> protobuf_descriptor.ServiceDescriptor:
    return file_descriptor.services_by_name[service_full_name.rsplit('.', 1)[1]]


def _declared_methods(file_descriptor: protobuf_descriptor.FileDescriptor, service_full_name: str) -> frozenset[str]:
    """The fully-qualified ``/service/Method`` strings a proto service declares, from its generated descriptor."""
    service = _service(file_descriptor, service_full_name)
    return frozenset(f'/{service.full_name}/{method.name}' for method in service.methods)


# The services the hatch forwards to the guest, each paired with the forwarder that fronts it. A forwarder is a
# whole-service pass-through; which of its rpcs the guest reaches is the allowlist's decision, per rpc. Every one
# is an agent-facing tool called in code mode; the store is deliberately absent — it is the trusted worker's,
# reached over no hatch method.
_REACHABLE_SERVICES: list[tuple[protobuf_descriptor.FileDescriptor, str, _ForwarderClass]] = [
    (hello_pb2.DESCRIPTOR, 'themis.rpc.hello.Hello', hatch.HelloForwarder),
    (variant_pb2.DESCRIPTOR, 'themis.rpc.variant.Variant', hatch.VariantForwarder),
    (vep_pb2.DESCRIPTOR, 'themis.rpc.vep.Vep', hatch.VepForwarder),
    (gnomad_pb2.DESCRIPTOR, 'themis.rpc.gnomad.Gnomad', hatch.GnomadForwarder),
    (clinvar_pb2.DESCRIPTOR, 'themis.rpc.clinvar.ClinVar', hatch.ClinVarForwarder),
    (gene_disease_pb2.DESCRIPTOR, 'themis.rpc.gene_disease.GeneDisease', hatch.GeneDiseaseForwarder),
    (transcript_pb2.DESCRIPTOR, 'themis.rpc.transcript.Transcript', hatch.TranscriptForwarder),
    (splice_pb2.DESCRIPTOR, 'themis.rpc.splice.Splice', hatch.SpliceForwarder),
    (mavedb_pb2.DESCRIPTOR, 'themis.rpc.mavedb.MaveDb', hatch.MaveDbForwarder),
    (cspec_pb2.DESCRIPTOR, 'themis.rpc.cspec.Cspec', hatch.CspecForwarder),
    (literature_pb2.DESCRIPTOR, 'themis.rpc.literature.Literature', hatch.LiteratureForwarder),
]

# One injection case per rpc each reachable service declares, derived from the descriptors so a newly-declared
# rpc is covered without editing a list here.
_FORWARDER_RPC_CASES = [
    pytest.param(forwarder_class, method.name, id=f'{_service(fd, name).name}.{method.name}')
    for fd, name, forwarder_class in _REACHABLE_SERVICES
    for method in _service(fd, name).methods
]

# The declared rpcs that stream their response, which a caller dials and a forwarder serves in a shape of their own.
_SERVER_STREAMING = frozenset(
    f'/{_service(fd, name).full_name}/{method.name}'
    for fd, name, _ in _REACHABLE_SERVICES
    for method in _service(fd, name).methods
    if method.server_streaming
)


@pytest.mark.parametrize(('file_descriptor', 'service_full_name'), [(fd, name) for fd, name, _ in _REACHABLE_SERVICES])
def test_every_reachable_service_has_an_allowlisted_rpc(
    file_descriptor: protobuf_descriptor.FileDescriptor, service_full_name: str
) -> None:
    # a forwarder fronting a service none of whose rpcs is marked is a forwarder nothing can reach.
    declared = _declared_methods(file_descriptor, service_full_name)
    assert declared, f'{service_full_name} declares no rpc — the check would be vacuous'
    assert declared & hatch.GUEST_METHODS, f'{service_full_name} has a forwarder but no allowlisted rpc'


def test_allowlist_reaches_nothing_beyond_the_reachable_services() -> None:
    # closed world: every allowlisted method is an rpc a forwarded service declares — no sheaf method, no stray
    # entry. The repository's storage protocol is the trusted worker's, never over the hatch.
    reachable = frozenset().union(*(_declared_methods(fd, name) for fd, name, _ in _REACHABLE_SERVICES))
    assert hatch.GUEST_METHODS, 'the allowlist is empty — the check would be vacuous'
    assert reachable >= hatch.GUEST_METHODS
    assert not any(method.startswith('/themis.rpc.sheaf.') for method in hatch.GUEST_METHODS)


class _RecordingCall:
    """A stand-in stub method: records the request, metadata and deadline one forwarded call carries."""

    def __init__(self, error: Exception | None) -> None:
        self.request: object = None
        self.metadata: object = None
        self.timeout: float | None = None
        self._error = error

    def __call__(self, request: object, metadata: object = None, timeout: float | None = None) -> object:
        self.request = request
        self.metadata = metadata
        self.timeout = timeout
        if self._error is not None:
            raise self._error
        return object()


class _RecordedStream:
    """What a stand-in server-streaming call returns: the messages it was given, then its error if it has one.

    A real stub never raises when the call is made; a stream's failure arrives while it is read, which is the
    shape a forwarder has to handle — after any number of messages, none included.
    """

    def __init__(self, messages: list[object], error: Exception | None) -> None:
        self._messages = iter(messages)
        self._error = error
        self.cancelled = False

    def __iter__(self) -> Iterator[object]:
        return self

    def __next__(self) -> object:
        message = next(self._messages, None)
        if message is not None:
            return message
        if self._error is not None:
            raise self._error
        raise StopIteration

    def cancel(self) -> bool:
        self.cancelled = True
        return True


class _RecordingStreamCall(_RecordingCall):
    """A stand-in server-streaming stub method: records what `_RecordingCall` does, and answers with a stream."""

    def __init__(self, error: Exception | None, messages: list[object]) -> None:
        super().__init__(error=None)
        self._stream_error = error
        self._messages = messages
        self.stream: _RecordedStream | None = None

    @override
    def __call__(self, request: object, metadata: object = None, timeout: float | None = None) -> _RecordedStream:
        super().__call__(request, metadata, timeout)
        self.stream = _RecordedStream(self._messages, self._stream_error)
        return self.stream


class _RecordingChannel:
    """The channel a forwarder is constructed over, so the real generated stub sits between it and the test.

    The stub binds one callable per rpc at construction time, each recorded here under its rpc name — and under
    its full method path, which is what says *which* channel a forwarder was handed. `error` makes every one of
    them fail the call instead of recording a result; a streaming one fails after delivering `messages`.
    """

    def __init__(self, error: Exception | None = None, *, messages: tuple[object, ...] = ()) -> None:
        self.calls: dict[str, _RecordingCall] = {}
        self.methods: set[str] = set()
        self._error = error
        self._messages = list(messages)

    def unary_unary(self, method: str, **_kwargs: object) -> _RecordingCall:
        return self._record(method, _RecordingCall(self._error))

    def unary_stream(self, method: str, **_kwargs: object) -> _RecordingStreamCall:
        call = _RecordingStreamCall(self._error, self._messages)
        self._record(method, call)
        return call

    def _record[Call: _RecordingCall](self, method: str, call: Call) -> Call:
        self.calls[method.rsplit('/', 1)[1]] = call
        self.methods.add(method)
        return call


def _forwarder(forwarder_class: _ForwarderClass, channel: _RecordingChannel) -> object:
    return forwarder_class(cast('grpc.Channel', channel), session_token=_TOKEN)


def _invoke(forwarder: object, method_name: str, request: object, context: grpc.ServicerContext) -> list[object]:
    """Call one forwarder rpc the way the hatch's server does, reading a streamed response through to its end.

    A streaming forwarder is a generator, so nothing is forwarded until it is read; a case that only called it
    would pass having checked nothing.
    """
    result = getattr(forwarder, method_name)(request, context)
    return list(result) if inspect.isgenerator(result) else [result]


# What a synchronous servicer context reports for a caller that named no deadline: not an absence, but the
# remainder of an int64-nanosecond one. A fake that answered None here would let an unbounded forward pass.
_NO_DEADLINE_REMAINING = 9.223372035066877e18

# grpc carries a deadline as a coarse `grpc-timeout` header and rounds it up, so a servicer can read back
# marginally more than the client asked for.
_DEADLINE_TOLERANCE_S = 1.0


class _GuestContext:
    """The servicer context a forwarder reads: the caller's remaining deadline, and how it aborts."""

    def __init__(self, remaining: float = 30.0) -> None:
        self.remaining = remaining
        self.code: grpc.StatusCode | None = None
        self.details: str | None = None
        self.callbacks: list[Callable[[], None]] = []
        self.accepts_callbacks = True

    def time_remaining(self) -> float:
        return self.remaining

    def add_callback(self, callback: Callable[[], None]) -> bool:
        if self.accepts_callbacks:
            self.callbacks.append(callback)
        return self.accepts_callbacks

    def abort(self, code: grpc.StatusCode, details: str) -> NoReturn:
        self.code = code
        self.details = details
        raise grpc.RpcError(details)


def _context(remaining: float = 30.0) -> grpc.ServicerContext:
    return cast('grpc.ServicerContext', _GuestContext(remaining))


@pytest.mark.parametrize(('forwarder_class', 'method_name'), _FORWARDER_RPC_CASES)
def test_forwarder_injects_the_session_token_on_every_rpc(forwarder_class: _ForwarderClass, method_name: str) -> None:
    # Drift guard: a forwarder method that forgets metadata=self._metadata records None here and fails; an rpc the
    # proto declares but the forwarder never overrides resolves to the generated servicer base and raises.
    channel = _RecordingChannel()
    forwarder = _forwarder(forwarder_class, channel)
    request = object()
    _invoke(forwarder, method_name, request, _context())
    call = channel.calls[method_name]
    assert call.request is request
    assert call.metadata == claim_mod.metadata_for(sandbox_options_pb2.CALLING_AS_AGENT_SESSION, _TOKEN)


class _SettledFailure(grpc.RpcError):
    """An upstream failure carrying a settled status, shaped like the ``_InactiveRpcError`` a real stub raises."""

    def __init__(
        self, code: grpc.StatusCode, details: str, trailing_metadata: tuple[tuple[str, str], ...] = ()
    ) -> None:
        super().__init__()
        self._code = code
        self._details = details
        self._trailing_metadata = trailing_metadata

    @override
    def code(self) -> grpc.StatusCode:
        return self._code

    @override
    def details(self) -> str:
        return self._details

    @override
    def trailing_metadata(self) -> tuple[tuple[str, str], ...]:  # pyright: ignore[reportIncompatibleMethodOverride] — pairs unpack as grpc's metadatum does
        return self._trailing_metadata


# The forwarder gates on `isinstance(error, grpc.Call)`; registering satisfies it without implementing the six
# abstract methods of that interface no forwarder consults.
grpc.Call.register(_SettledFailure)

# The trailer a servicer ends a failure with when it wrote the details itself (`authored_status.abort`).
_MARKED = ((authored_status.AUTHORED_METADATA, '1'),)


@pytest.mark.parametrize(('forwarder_class', 'method_name'), _FORWARDER_RPC_CASES)
def test_forwarder_carries_a_settled_upstream_status_through(
    forwarder_class: _ForwarderClass, method_name: str
) -> None:
    # A settled status must reach the guest as itself. Left to escape the servicer it becomes UNKNOWN, so a
    # NOT_FOUND would read as a fault the caller retries rather than as the answer it is.
    failure = _SettledFailure(grpc.StatusCode.NOT_FOUND, 'the source holds no record', _MARKED)
    forwarder = _forwarder(forwarder_class, _RecordingChannel(failure))
    context = _GuestContext()
    with pytest.raises(grpc.RpcError):
        _invoke(forwarder, method_name, object(), cast('grpc.ServicerContext', context))
    assert context.code is grpc.StatusCode.NOT_FOUND
    assert context.details == 'the source holds no record'


# Text something below the servicer writes, under a code a servicer writes too. grpc synthesises every one of these
# for a failed channel or a cut deadline, naming the resolved upstream; Cloud Run and the ID-token plugin set the auth
# codes naming the audience, which is the upstream URL. None carries the mark, whatever the code.
_UNAUTHORED_FAILURES = [
    (grpc.StatusCode.UNAVAILABLE, 'failed to connect to all addresses; last error: ipv4:10.4.0.7:443'),
    (grpc.StatusCode.PERMISSION_DENIED, 'audience https://themis-evidence-service-xyz.a.run.app was rejected'),
    (grpc.StatusCode.DEADLINE_EXCEEDED, 'Deadline Exceeded while dialling ipv4:10.4.0.7:443'),
    (grpc.StatusCode.INVALID_ARGUMENT, 'grpc: error unmarshalling request from 10.4.0.7'),
    (grpc.StatusCode.NOT_FOUND, 'Method not found on https://themis-evidence-service-xyz.a.run.app'),
    (grpc.StatusCode.FAILED_PRECONDITION, 'the ALTS handshake with 10.4.0.7 failed'),
]


@pytest.mark.parametrize(
    ('code', 'upstream_details'), _UNAUTHORED_FAILURES, ids=[c.name for c, _ in _UNAUTHORED_FAILURES]
)
@pytest.mark.parametrize(('forwarder_class', 'method_name'), _FORWARDER_RPC_CASES)
def test_forwarder_withholds_details_the_servicer_did_not_mark(
    forwarder_class: _ForwarderClass, method_name: str, code: grpc.StatusCode, upstream_details: str
) -> None:
    # The mark is the one rule: unmarked text under any code was written below the servicer, and names the upstream.
    # The code crosses so the guest can tell a fault from an answer; the string does not.
    failure = _SettledFailure(code, upstream_details)
    forwarder = _forwarder(forwarder_class, _RecordingChannel(failure))
    context = _GuestContext()
    with pytest.raises(grpc.RpcError):
        _invoke(forwarder, method_name, object(), cast('grpc.ServicerContext', context))
    assert context.code is code
    assert context.details == code.name


@pytest.mark.parametrize(('forwarder_class', 'method_name'), _FORWARDER_RPC_CASES)
def test_forwarder_carries_details_the_servicer_marked_as_its_own(
    forwarder_class: _ForwarderClass, method_name: str
) -> None:
    # An UNAVAILABLE a servicer wrote says which of its upstreams failed, which is what lets the guest tell an
    # outage from a fault of ours; the mark is what tells it from the channel-level text withheld above.
    details = 'Normalize failed upstream: reg.clinicalgenome.org did not answer in time (ReadTimeout)'
    failure = _SettledFailure(grpc.StatusCode.UNAVAILABLE, details, _MARKED)
    forwarder = _forwarder(forwarder_class, _RecordingChannel(failure))
    context = _GuestContext()
    with pytest.raises(grpc.RpcError):
        _invoke(forwarder, method_name, object(), cast('grpc.ServicerContext', context))
    assert context.code is grpc.StatusCode.UNAVAILABLE
    assert context.details == details


@pytest.mark.parametrize(('forwarder_class', 'method_name'), _FORWARDER_RPC_CASES)
def test_forwarder_reraises_a_failure_carrying_no_status(forwarder_class: _ForwarderClass, method_name: str) -> None:
    # Nothing to restate: an error with no status is propagated rather than relabelled under a made-up one.
    forwarder = _forwarder(forwarder_class, _RecordingChannel(grpc.RpcError('no status')))
    context = _GuestContext()
    with pytest.raises(grpc.RpcError):
        _invoke(forwarder, method_name, object(), cast('grpc.ServicerContext', context))
    assert context.code is None


@pytest.mark.parametrize(('forwarder_class', 'method_name'), _FORWARDER_RPC_CASES)
def test_forwarder_forwards_under_a_deadline_shorter_than_the_ceiling(
    forwarder_class: _ForwarderClass, method_name: str
) -> None:
    """A caller with less time left than the ceiling keeps its own budget: re-imposing one would outlast it."""
    channel = _RecordingChannel()
    forwarder = _forwarder(forwarder_class, channel)
    _invoke(forwarder, method_name, object(), _context(12.5))
    assert channel.calls[method_name].timeout == 12.5


@pytest.mark.parametrize('remaining', [_NO_DEADLINE_REMAINING, 36000.0])
@pytest.mark.parametrize(('forwarder_class', 'method_name'), _FORWARDER_RPC_CASES)
def test_forwarder_caps_a_budget_beyond_the_ceiling(
    forwarder_class: _ForwarderClass, method_name: str, remaining: float
) -> None:
    """A budget past the ceiling is capped, not honoured: the bound is not a default the caller can raise."""
    channel = _RecordingChannel()
    forwarder = _forwarder(forwarder_class, channel)
    _invoke(forwarder, method_name, object(), _context(remaining))
    assert channel.calls[method_name].timeout == hatch._FORWARD_CEILING_S


def _service_of(method: str) -> str:
    """The `pkg.Service` an allowlisted `/pkg.Service/Method` path names."""
    return method.split('/')[1]


def _dial(guest: grpc.Channel, method: str) -> None:
    """Call `method` with an empty request in the shape it is declared, reading a stream through to its status."""
    if method in _SERVER_STREAMING:
        list(guest.unary_stream(method)(b'', timeout=10))
    else:
        guest.unary_unary(method)(b'', timeout=10)


def test_build_hatch_hands_each_forwarder_the_channel_for_its_deployment() -> None:
    """Both channels are `grpc.Channel`, so swapping them is not a type error — and the stubs bind at construction.

    hello is its own deployment; the ten evidence interfaces share one, so the split is the whole of the routing.
    """
    hello_channel, evidence_channel = _RecordingChannel(), _RecordingChannel()
    hatch.build_hatch(
        hello_channel=cast('grpc.Channel', hello_channel),
        evidence_channel=cast('grpc.Channel', evidence_channel),
        session_token=_TOKEN,
    ).close()
    hello_methods = {method for method in hatch.GUEST_METHODS if _service_of(method) == 'themis.rpc.hello.Hello'}
    evidence_methods = hatch.GUEST_METHODS - hello_methods
    assert hello_methods, 'no hello method is allowlisted — the split would pass vacuously'
    assert evidence_methods, 'no evidence method is allowlisted — the split would pass vacuously'
    assert hello_channel.methods >= hello_methods
    assert not hello_methods & evidence_channel.methods
    assert evidence_channel.methods >= evidence_methods
    assert not evidence_methods & hello_channel.methods


def test_build_hatch_serves_every_allowlisted_method() -> None:
    """A service `build_hatch` never registers keeps its allowlist entry and answers UNIMPLEMENTED.

    No other test here notices: the forwarder classes and the allowlist can both be right while the wiring
    between them is missing. Dialling every allowlisted method against a hatch whose upstreams refuse
    connections separates the two — a registered method fails on the forward leg, an unregistered one is
    UNIMPLEMENTED before any forwarding is attempted. The same refusal is what grpc writes an address into,
    so the forward leg's real diagnostic is checked here rather than only against a hand-built one.
    """
    unreachable = grpc.insecure_channel('127.0.0.1:1')
    hatch_server = hatch.build_hatch(hello_channel=unreachable, evidence_channel=unreachable, session_token=_TOKEN)
    hatch_server.start()
    try:
        with grpc.insecure_channel(f'unix:{hatch_server.socket_path}') as guest:
            for method in sorted(hatch.GUEST_METHODS):
                with pytest.raises(grpc.RpcError) as raised:
                    _dial(guest, method)
                call_error = cast('grpc.Call', raised.value)
                code = call_error.code()
                assert code is not grpc.StatusCode.UNIMPLEMENTED, f'{_service_of(method)} has no forwarder registered'
                assert code is not grpc.StatusCode.PERMISSION_DENIED, f'{method} is served but refused at the hatch'
                # The refusal is the real thing grpc synthesises a diagnostic for, address and all.
                assert call_error.details() == code.name, f'{method} leaked an upstream diagnostic to the guest'
    finally:
        hatch_server.close()
        unreachable.close()


def test_the_hatch_refuses_every_declared_method_the_allowlist_omits() -> None:
    """A forwarder is a whole-service pass-through, so the hatch serves rpcs the allowlist omits; each is refused.

    The first partially exposed service is where this stops being vacuous: the literature forwarder registers
    eleven rpcs and the allowlist admits eight. The refusal has to come from the hatch, before any forwarding,
    so the code is PERMISSION_DENIED and never the forward leg's UNAVAILABLE — a hatch that stopped consulting
    its allowlist would hand the guest a storage location here with every other test still green.
    """
    declared = frozenset().union(*(_declared_methods(fd, name) for fd, name, _ in _REACHABLE_SERVICES))
    omitted = sorted(declared - hatch.GUEST_METHODS)
    assert omitted, 'every declared rpc is allowlisted — the check would be vacuous'
    unreachable = grpc.insecure_channel('127.0.0.1:1')
    hatch_server = hatch.build_hatch(hello_channel=unreachable, evidence_channel=unreachable, session_token=_TOKEN)
    hatch_server.start()
    try:
        with grpc.insecure_channel(f'unix:{hatch_server.socket_path}') as guest:
            for method in omitted:
                with pytest.raises(grpc.RpcError) as raised:
                    _dial(guest, method)
                assert cast('grpc.Call', raised.value).code() is grpc.StatusCode.PERMISSION_DENIED, method
    finally:
        hatch_server.close()
        unreachable.close()


def test_the_guest_has_an_accessor_for_every_exposed_service() -> None:
    """The guest reaches a service through `themis.agent.services`, so an rpc with no accessor is unreachable.

    The accessor name is the proto package's last segment — the same word the service is named for.
    """
    exposed = {_service_of(method).rsplit('.', 1)[0].rsplit('.', 1)[1] for method in hatch.GUEST_METHODS}
    assert exposed, 'no services parsed out of the allowlist'
    missing = sorted(name for name in exposed if not callable(getattr(services, name, None)))
    assert not missing, f'{missing} are agent-exposed but have no guest accessor'


class _RecordingUpstream(gnomad_pb2_grpc.GnomadServicer):
    """A real upstream, so the deadline a forwarded call actually carried can be read back off the wire."""

    def __init__(self) -> None:
        self.remaining: float | None = None

    @override
    def DescribeVariant(
        self, request: gnomad_pb2.DescribeVariantRequest, context: grpc.ServicerContext
    ) -> gnomad_pb2.DescribeVariantResponse:
        del request
        self.remaining = context.time_remaining()
        return gnomad_pb2.DescribeVariantResponse()


def test_the_ceiling_binds_a_live_call_that_named_no_deadline() -> None:
    """What `time_remaining()` reports for an absent deadline is grpc's behaviour, not this repo's.

    A fake context can only assert the cap against a value chosen here. Reading the deadline back off a real
    forwarded call is what says the cap binds what grpc actually hands a servicer — the case the guest hits
    every time, since a stub from `guest.services` carries no deadline unless the model's code names one.
    """
    upstream_impl = _RecordingUpstream()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    server = grpc.server(pool)
    port = server.add_insecure_port('127.0.0.1:0')
    gnomad_pb2_grpc.add_GnomadServicer_to_server(upstream_impl, server)
    server.start()
    upstream = grpc.insecure_channel(f'127.0.0.1:{port}')
    hatch_server = hatch.build_hatch(hello_channel=upstream, evidence_channel=upstream, session_token=_TOKEN)
    hatch_server.start()
    try:
        with grpc.insecure_channel(f'unix:{hatch_server.socket_path}') as guest:
            gnomad_pb2_grpc.GnomadStub(guest).DescribeVariant(gnomad_pb2.DescribeVariantRequest())
    finally:
        hatch_server.close()
        upstream.close()
        server.stop(0).wait()
        pool.shutdown(wait=False)
    assert upstream_impl.remaining is not None, 'the upstream was never reached'
    assert upstream_impl.remaining <= hatch._FORWARD_CEILING_S + _DEADLINE_TOLERANCE_S
    # Rules out the cap passing by arriving as an already-spent deadline rather than as the ceiling. Twenty orders
    # of magnitude separate a capped deadline from an uncapped one, so no tolerance blurs the two.
    assert upstream_impl.remaining > hatch._FORWARD_CEILING_S / 2


def test_the_forwarding_ceiling_sits_under_the_command_bound() -> None:
    """A forwarded call that outlives the command that asked for it holds a serving thread for nothing.

    The bound that fires on the command is the shim's, not the shell call's, so the ceiling is asserted
    against that one — against the constant, not a number.
    """
    assert hatch._FORWARD_CEILING_S < tool.COMMAND_TIMEOUT_S


# The forwarder cases for the rpcs that stream their response: what a stream adds is a failure after data.
_STREAMING_FORWARDER_CASES = [
    pytest.param(forwarder_class, method.name, id=f'{_service(fd, name).name}.{method.name}')
    for fd, name, forwarder_class in _REACHABLE_SERVICES
    for method in _service(fd, name).methods
    if method.server_streaming
]


def test_some_forwarded_rpc_streams() -> None:
    """Parametrising over an empty set skips every streaming case below and reports green."""
    assert _STREAMING_FORWARDER_CASES, 'no reachable rpc streams its response — the streaming cases would be vacuous'


@pytest.mark.parametrize(('marked', 'expected_details'), [(True, 'the rendering broke off'), (False, 'INTERNAL')])
@pytest.mark.parametrize(('forwarder_class', 'method_name'), _STREAMING_FORWARDER_CASES)
def test_a_stream_that_fails_after_data_delivers_what_arrived_and_ends_under_the_upstream_code(
    forwarder_class: _ForwarderClass, method_name: str, *, marked: bool, expected_details: str
) -> None:
    # The authored-status rule does not lapse once data has crossed: the stream ends under the upstream's code,
    # and its details cross only with the servicer's mark.
    failure = _SettledFailure(grpc.StatusCode.INTERNAL, 'the rendering broke off', _MARKED if marked else ())
    sent = (object(), object())
    forwarder = _forwarder(forwarder_class, _RecordingChannel(failure, messages=sent))
    context = _GuestContext()
    received: list[object] = []
    with pytest.raises(grpc.RpcError):  # noqa: PT012 — the messages before the failure are read inside the block
        for response in getattr(forwarder, method_name)(object(), cast('grpc.ServicerContext', context)):
            received.append(response)
    assert received == list(sent)
    assert context.code is grpc.StatusCode.INTERNAL
    assert context.details == expected_details


@pytest.mark.parametrize(('forwarder_class', 'method_name'), _STREAMING_FORWARDER_CASES)
def test_a_stream_passes_each_message_on_as_it_arrives(forwarder_class: _ForwarderClass, method_name: str) -> None:
    sent = (object(), object(), object())
    forwarder = _forwarder(forwarder_class, _RecordingChannel(messages=sent))
    assert _invoke(forwarder, method_name, object(), _context()) == list(sent)


@pytest.mark.parametrize(('forwarder_class', 'method_name'), _STREAMING_FORWARDER_CASES)
def test_a_stream_the_guest_abandons_is_cancelled_upstream(forwarder_class: _ForwarderClass, method_name: str) -> None:
    # A guest that stops reading must not leave the upstream call running out its deadline on a serving thread:
    # the forwarder registers the cancellation with the guest's rpc, and grpc runs it when that rpc ends.
    channel = _RecordingChannel(messages=(object(), object()))
    forwarder = _forwarder(forwarder_class, channel)
    context = _GuestContext()
    responses = getattr(forwarder, method_name)(object(), cast('grpc.ServicerContext', context))
    next(responses)
    stream = cast('_RecordingStreamCall', channel.calls[method_name]).stream
    assert stream is not None
    assert not stream.cancelled
    for callback in context.callbacks:
        callback()
    assert stream.cancelled


class _BreakingStream:
    """A real streaming upstream for `Literature.ReadMarkdown`: two chunks, then a failure, marked or not.

    Served through a generic handler rather than a `LiteratureServicer` subclass, which would have to stand in
    for every other rpc of the service.
    """

    def __init__(self, *, marked: bool) -> None:
        self.remaining: float | None = None
        self._marked = marked

    def __call__(
        self, request: literature_pb2.ReadMarkdownRequest, context: grpc.ServicerContext
    ) -> Iterator[literature_pb2.ReadMarkdownResponse]:
        del request
        self.remaining = context.time_remaining()
        yield literature_pb2.ReadMarkdownResponse(chunk=b'first ')
        yield literature_pb2.ReadMarkdownResponse(chunk=b'second')
        if self._marked:
            context.set_trailing_metadata(_MARKED)
        context.abort(grpc.StatusCode.INTERNAL, 'the rendering broke off at 10.4.0.7')


@pytest.mark.parametrize(
    ('marked', 'expected_details'), [(True, 'the rendering broke off at 10.4.0.7'), (False, 'INTERNAL')]
)
def test_a_live_stream_crosses_the_hatch_and_its_late_failure_keeps_the_authored_rule(
    *, marked: bool, expected_details: str
) -> None:
    """Over real channels: the chunks reach the guest, then the upstream's code, its details only when marked.

    A fake context cannot show that a status set after messages were written still reaches the caller as the
    stream's own, nor what deadline grpc hands a streaming servicer whose caller named none; this does both.
    """
    upstream_impl = _BreakingStream(marked=marked)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    server = grpc.server(pool)
    port = server.add_insecure_port('127.0.0.1:0')
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                'themis.rpc.literature.Literature',
                {
                    'ReadMarkdown': grpc.unary_stream_rpc_method_handler(
                        upstream_impl,
                        request_deserializer=literature_pb2.ReadMarkdownRequest.FromString,
                        response_serializer=literature_pb2.ReadMarkdownResponse.SerializeToString,
                    )
                },
            ),
        )
    )
    server.start()
    upstream = grpc.insecure_channel(f'127.0.0.1:{port}')
    hatch_server = hatch.build_hatch(hello_channel=upstream, evidence_channel=upstream, session_token=_TOKEN)
    hatch_server.start()
    received: list[bytes] = []
    try:
        with grpc.insecure_channel(f'unix:{hatch_server.socket_path}') as guest:
            stub = literature_pb2_grpc.LiteratureStub(guest)
            with pytest.raises(grpc.RpcError) as raised:  # noqa: PT012 — the chunks before the failure are read inside
                for response in stub.ReadMarkdown(literature_pb2.ReadMarkdownRequest(doc_id='doc-1')):
                    received.append(response.chunk)
    finally:
        hatch_server.close()
        upstream.close()
        server.stop(0).wait()
        pool.shutdown(wait=False)
    assert received == [b'first ', b'second']
    failure = cast('grpc.Call', raised.value)
    assert failure.code() is grpc.StatusCode.INTERNAL
    assert failure.details() == expected_details
    assert upstream_impl.remaining is not None, 'the upstream was never reached'
    assert hatch._FORWARD_CEILING_S / 2 < upstream_impl.remaining <= hatch._FORWARD_CEILING_S + _DEADLINE_TOLERANCE_S


@pytest.mark.parametrize(('forwarder_class', 'method_name'), _STREAMING_FORWARDER_CASES)
def test_a_stream_whose_guest_already_left_is_cancelled_and_not_read(
    forwarder_class: _ForwarderClass, method_name: str
) -> None:
    # grpc refuses a callback on an rpc that has ended and never runs it, so the cancellation is the forwarder's own.
    channel = _RecordingChannel(messages=(object(),))
    context = _GuestContext()
    context.accepts_callbacks = False
    responses = _invoke(
        _forwarder(forwarder_class, channel), method_name, object(), cast('grpc.ServicerContext', context)
    )
    stream = cast('_RecordingStreamCall', channel.calls[method_name]).stream
    assert stream is not None
    assert stream.cancelled
    assert responses == []


class _EndlessStream:
    """A real streaming upstream that sends one chunk, then holds the call open until it is cancelled.

    It sends nothing more, so the hatch's serving thread never learns of the guest's departure from a failed
    send: only the cancellation the forwarder registers with the guest's rpc can reach it.
    """

    def __init__(self) -> None:
        self.abandoned = threading.Event()

    def __call__(
        self, request: literature_pb2.ReadMarkdownRequest, context: grpc.ServicerContext
    ) -> Iterator[literature_pb2.ReadMarkdownResponse]:
        del request
        context.add_callback(self.abandoned.set)
        yield literature_pb2.ReadMarkdownResponse(chunk=b'first')
        while context.is_active():
            time.sleep(0.01)


def test_a_guest_that_cancels_a_live_stream_cancels_the_upstream() -> None:
    """Over real channels: the guest's cancellation reaches the upstream, which stops rather than streaming on."""
    upstream_impl = _EndlessStream()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    server = grpc.server(pool)
    port = server.add_insecure_port('127.0.0.1:0')
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                'themis.rpc.literature.Literature',
                {
                    'ReadMarkdown': grpc.unary_stream_rpc_method_handler(
                        upstream_impl,
                        request_deserializer=literature_pb2.ReadMarkdownRequest.FromString,
                        response_serializer=literature_pb2.ReadMarkdownResponse.SerializeToString,
                    )
                },
            ),
        )
    )
    server.start()
    upstream = grpc.insecure_channel(f'127.0.0.1:{port}')
    hatch_server = hatch.build_hatch(hello_channel=upstream, evidence_channel=upstream, session_token=_TOKEN)
    hatch_server.start()
    try:
        with grpc.insecure_channel(f'unix:{hatch_server.socket_path}') as guest:
            call = literature_pb2_grpc.LiteratureStub(guest).ReadMarkdown(literature_pb2.ReadMarkdownRequest())
            next(call)
            assert not upstream_impl.abandoned.is_set()
            call.cancel()
            assert upstream_impl.abandoned.wait(timeout=10)
    finally:
        hatch_server.close()
        upstream.close()
        server.stop(0).wait()
        pool.shutdown(wait=False)
