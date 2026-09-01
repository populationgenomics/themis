"""The guest's one channel to the sandbox hatch, and the deadline every call over it carries.

Guest-side (sandbox-worker.md §"The guest's world is assembled at build time"): runs inside the postern sandbox,
shipped into the guest rootfs under ``themis.agent``. The channel is created on first use and memoised, and dials
the hatch UDS at ``unix:$POSTERN_HATCH``. The trusted worker's hatch injects the session token and forwards to the
real service, so calling code holds no credentials and no service URL. Only the allowlisted methods are reachable;
anything else is ``PERMISSION_DENIED``.

It fills in a deadline for any call that names none, so an unbounded call is not reachable through it at all.
Leaving that to each caller is what the design doc rules out: the guidance a snippet does not follow is no
protection, and the snippet that forgets is exactly the one that hangs.
"""

from __future__ import annotations

import collections
import functools
import os
from collections.abc import Callable, Iterator
from typing import override

import grpc

# The deadline a call gets when it names none, against the 120 s the worker allows one shell tool call before it
# abandons it: what is left is what the snippet needs to catch the failure, print what it did get, and exit.
DEFAULT_TIMEOUT_S = 90.0


class _CallDetails(
    collections.namedtuple(  # noqa: PYI024 — grpc.ClientCallDetails needs a base with these attributes
        '_CallDetails', ('method', 'timeout', 'metadata', 'credentials', 'wait_for_ready', 'compression')
    ),
    grpc.ClientCallDetails,
):
    """The call details grpc passes an interceptor, rebuilt with a deadline filled in."""


def _with_deadline(details: grpc.ClientCallDetails) -> grpc.ClientCallDetails:
    """`details` unchanged if the caller set a timeout, else carrying the default deadline."""
    if details.timeout is not None:
        return details
    return _CallDetails(
        method=details.method,
        timeout=DEFAULT_TIMEOUT_S,
        metadata=details.metadata,
        credentials=details.credentials,
        wait_for_ready=details.wait_for_ready,
        compression=details.compression,
    )


class _DefaultDeadline(
    grpc.UnaryUnaryClientInterceptor,
    grpc.UnaryStreamClientInterceptor,
    grpc.StreamUnaryClientInterceptor,
    grpc.StreamStreamClientInterceptor,
):
    """Gives every call a deadline unless its caller set one; all four call shapes, so none is left unbounded."""

    @override
    def intercept_unary_unary[Request, Response](
        self,
        continuation: Callable[[grpc.ClientCallDetails, Request], Response],
        client_call_details: grpc.ClientCallDetails,
        request: Request,
    ) -> Response:
        return continuation(_with_deadline(client_call_details), request)

    @override
    def intercept_unary_stream[Request, Response](
        self,
        continuation: Callable[[grpc.ClientCallDetails, Request], Response],
        client_call_details: grpc.ClientCallDetails,
        request: Request,
    ) -> Response:
        return continuation(_with_deadline(client_call_details), request)

    @override
    def intercept_stream_unary[Request, Response](
        self,
        continuation: Callable[[grpc.ClientCallDetails, Iterator[Request]], Response],
        client_call_details: grpc.ClientCallDetails,
        request_iterator: Iterator[Request],
    ) -> Response:
        return continuation(_with_deadline(client_call_details), request_iterator)

    @override
    def intercept_stream_stream[Request, Response](
        self,
        continuation: Callable[[grpc.ClientCallDetails, Iterator[Request]], Response],
        client_call_details: grpc.ClientCallDetails,
        request_iterator: Iterator[Request],
    ) -> Response:
        return continuation(_with_deadline(client_call_details), request_iterator)


@functools.cache
def to_hatch() -> grpc.Channel:
    """The process's channel to the hatch, created on first use.

    Raises:
        KeyError: If ``POSTERN_HATCH`` is unset — a guest reaching for a service outside the sandbox is a
            broken rootfs, not a case to fall back from.
    """
    return grpc.intercept_channel(grpc.insecure_channel('unix:' + os.environ['POSTERN_HATCH']), _DefaultDeadline())
