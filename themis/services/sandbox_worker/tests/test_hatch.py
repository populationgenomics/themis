"""Tests for the hatch forwarders: allowlist scope, session-token injection, upstream status propagation.

All three are checked against the proto descriptors, not a literal method list, so a service that adds an rpc
surfaces here — the allowlist must cover it, and its forwarder must inject the token and carry an upstream
status through — rather than silently widening or drifting.
"""

from __future__ import annotations

import concurrent.futures
from typing import NoReturn, Protocol, cast, override

import grpc
import pytest
from google.protobuf import descriptor as protobuf_descriptor

from themis.rpc import (
    clinvar_pb2,
    cspec_pb2,
    gene_disease_pb2,
    gnomad_pb2,
    gnomad_pb2_grpc,
    hello_pb2,
    literature_pb2,
    mavedb_pb2,
    splice_pb2,
    transcript_pb2,
    variant_pb2,
    vep_pb2,
)
from themis.services.sandbox_worker import hatch
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


@pytest.mark.parametrize(('file_descriptor', 'service_full_name'), [(fd, name) for fd, name, _ in _REACHABLE_SERVICES])
def test_every_reachable_service_has_an_allowlisted_rpc(
    file_descriptor: protobuf_descriptor.FileDescriptor, service_full_name: str
) -> None:
    # a forwarder fronting a service none of whose rpcs is marked is a forwarder nothing can reach.
    declared = _declared_methods(file_descriptor, service_full_name)
    assert declared, f'{service_full_name} declares no rpc — the check would be vacuous'
    assert declared & hatch.GUEST_METHODS, f'{service_full_name} has a forwarder but no allowlisted rpc'


def test_allowlist_reaches_nothing_beyond_the_reachable_services() -> None:
    # closed world: every allowlisted method is an rpc a forwarded service declares — no store method, no stray
    # entry. The store (working document + ephemeral-workspace scratch) is the trusted worker's, never over the hatch.
    reachable = frozenset().union(*(_declared_methods(fd, name) for fd, name, _ in _REACHABLE_SERVICES))
    assert hatch.GUEST_METHODS, 'the allowlist is empty — the check would be vacuous'
    assert reachable >= hatch.GUEST_METHODS
    assert not any('Store' in method for method in hatch.GUEST_METHODS)


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


class _RecordingChannel:
    """The channel a forwarder is constructed over, so the real generated stub sits between it and the test.

    The stub binds one callable per rpc at construction time, each recorded here under its rpc name — and under
    its full method path, which is what says *which* channel a forwarder was handed. `error` makes every one of
    them fail the call instead of recording a result.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: dict[str, _RecordingCall] = {}
        self.methods: set[str] = set()
        self._error = error

    def unary_unary(self, method: str, **_kwargs: object) -> _RecordingCall:
        call = _RecordingCall(self._error)
        self.calls[method.rsplit('/', 1)[1]] = call
        self.methods.add(method)
        return call


def _forwarder(forwarder_class: _ForwarderClass, channel: _RecordingChannel) -> object:
    return forwarder_class(cast('grpc.Channel', channel), session_token=_TOKEN)


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

    def time_remaining(self) -> float:
        return self.remaining

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
    getattr(forwarder, method_name)(request, _context())
    call = channel.calls[method_name]
    assert call.request is request
    assert call.metadata == (('x-themis-session-token', _TOKEN),)


class _SettledFailure(grpc.RpcError):
    """An upstream failure carrying a settled status, shaped like the ``_InactiveRpcError`` a real stub raises."""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__()
        self._code = code
        self._details = details

    @override
    def code(self) -> grpc.StatusCode:
        return self._code

    @override
    def details(self) -> str:
        return self._details


# The forwarder gates on `isinstance(error, grpc.Call)`; registering satisfies it without implementing the six
# abstract methods of that interface no forwarder consults.
grpc.Call.register(_SettledFailure)


@pytest.mark.parametrize(('forwarder_class', 'method_name'), _FORWARDER_RPC_CASES)
def test_forwarder_carries_a_settled_upstream_status_through(
    forwarder_class: _ForwarderClass, method_name: str
) -> None:
    # A settled status must reach the guest as itself. Left to escape the servicer it becomes UNKNOWN, so a
    # NOT_FOUND would read as a fault the caller retries rather than as the answer it is.
    failure = _SettledFailure(grpc.StatusCode.NOT_FOUND, 'the source holds no record')
    forwarder = _forwarder(forwarder_class, _RecordingChannel(failure))
    context = _GuestContext()
    with pytest.raises(grpc.RpcError):
        getattr(forwarder, method_name)(object(), cast('grpc.ServicerContext', context))
    assert context.code is grpc.StatusCode.NOT_FOUND
    assert context.details == 'the source holds no record'


# A code the upstream contract does not exclusively own, paired with text only something below the servicer writes.
# PERMISSION_DENIED is the one an evidence servicer also sets: Cloud Run and the ID-token plugin set it too, and
# theirs names the audience — which is the upstream URL.
_UNAUTHORED_FAILURES = [
    (grpc.StatusCode.UNAVAILABLE, 'failed to connect to all addresses; last error: ipv4:10.4.0.7:443'),
    (grpc.StatusCode.PERMISSION_DENIED, 'audience https://themis-evidence-service-xyz.a.run.app was rejected'),
]


@pytest.mark.parametrize(('code', 'upstream_details'), _UNAUTHORED_FAILURES)
@pytest.mark.parametrize(('forwarder_class', 'method_name'), _FORWARDER_RPC_CASES)
def test_forwarder_withholds_a_diagnostic_the_upstream_did_not_author(
    forwarder_class: _ForwarderClass, method_name: str, code: grpc.StatusCode, upstream_details: str
) -> None:
    # Text under a code no evidence servicer exclusively owns was written below it, and names the upstream. The
    # code crosses so the guest can tell a fault from an answer; the string does not.
    failure = _SettledFailure(code, upstream_details)
    forwarder = _forwarder(forwarder_class, _RecordingChannel(failure))
    context = _GuestContext()
    with pytest.raises(grpc.RpcError):
        getattr(forwarder, method_name)(object(), cast('grpc.ServicerContext', context))
    assert context.code is code
    assert context.details == code.name


@pytest.mark.parametrize(('forwarder_class', 'method_name'), _FORWARDER_RPC_CASES)
def test_forwarder_reraises_a_failure_carrying_no_status(forwarder_class: _ForwarderClass, method_name: str) -> None:
    # Nothing to restate: an error with no status is propagated rather than relabelled under a made-up one.
    forwarder = _forwarder(forwarder_class, _RecordingChannel(grpc.RpcError('no status')))
    context = _GuestContext()
    with pytest.raises(grpc.RpcError):
        getattr(forwarder, method_name)(object(), cast('grpc.ServicerContext', context))
    assert context.code is None


@pytest.mark.parametrize(('forwarder_class', 'method_name'), _FORWARDER_RPC_CASES)
def test_forwarder_forwards_under_a_deadline_shorter_than_the_ceiling(
    forwarder_class: _ForwarderClass, method_name: str
) -> None:
    """A caller with less time left than the ceiling keeps its own budget: re-imposing one would outlast it."""
    channel = _RecordingChannel()
    forwarder = _forwarder(forwarder_class, channel)
    getattr(forwarder, method_name)(object(), _context(12.5))
    assert channel.calls[method_name].timeout == 12.5


@pytest.mark.parametrize('remaining', [_NO_DEADLINE_REMAINING, 36000.0])
@pytest.mark.parametrize(('forwarder_class', 'method_name'), _FORWARDER_RPC_CASES)
def test_forwarder_caps_a_budget_beyond_the_ceiling(
    forwarder_class: _ForwarderClass, method_name: str, remaining: float
) -> None:
    """A budget past the ceiling is capped, not honoured: the bound is not a default the caller can raise."""
    channel = _RecordingChannel()
    forwarder = _forwarder(forwarder_class, channel)
    getattr(forwarder, method_name)(object(), _context(remaining))
    assert channel.calls[method_name].timeout == hatch._FORWARD_CEILING_S


def _service_of(method: str) -> str:
    """The `pkg.Service` an allowlisted `/pkg.Service/Method` path names."""
    return method.split('/')[1]


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
                call = guest.unary_unary(method)
                with pytest.raises(grpc.RpcError) as raised:
                    call(b'', timeout=10)
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
                    guest.unary_unary(method)(b'', timeout=10)
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
