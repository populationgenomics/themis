"""Tests for the hatch forwarder: allowlist scope and session-token injection (hello only)."""

from __future__ import annotations

from typing import cast

import grpc

from themis.rpc import hello_pb2, hello_pb2_grpc
from themis.services.sandbox_worker import hatch

_NULL_CONTEXT = cast('grpc.ServicerContext', None)  # the forwarder methods ignore context


def test_allowlist_is_only_the_hello_method() -> None:
    assert frozenset({'/themis.rpc.hello.Hello/SayHello'}) == hatch.GUEST_METHODS
    # the store — the working document and the ephemeral-workspace scratch — is the trusted worker's, never
    # reachable over the hatch
    assert not any('Store' in method for method in hatch.GUEST_METHODS)


class _RecordingStub:
    """Records each call's request and metadata; stands in for the hello stub."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []

    def SayHello(self, request: object, metadata: object = None) -> hello_pb2.SayHelloResponse:  # noqa: N802 — mirrors the stub method name
        self.calls.append(('SayHello', request, metadata))
        return hello_pb2.SayHelloResponse(greeting='hi', project_id='p', analysis_id='a')


def _hello_forwarder(stub: _RecordingStub, token: str) -> hatch.HelloForwarder:
    # Bypass __init__ so no real channel is dialled; inject the recording stub in place of the real stub.
    forwarder = hatch.HelloForwarder.__new__(hatch.HelloForwarder)
    forwarder._stub = cast('hello_pb2_grpc.HelloStub', stub)
    forwarder._metadata = ((hatch._SESSION_TOKEN_METADATA, token),)
    return forwarder


def test_hello_forwards_with_the_injected_session_token() -> None:
    stub = _RecordingStub()
    request = hello_pb2.SayHelloRequest(note='hi')
    reply = _hello_forwarder(stub, 'TOK').SayHello(request, _NULL_CONTEXT)
    assert reply.greeting == 'hi'
    name, sent_request, metadata = stub.calls[0]
    assert name == 'SayHello'
    assert sent_request is request
    assert metadata == (('x-themis-session-token', 'TOK'),)
