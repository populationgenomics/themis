"""Retrying (and optionally caching) calls to the internal Themis services, for code-mode snippets.

Guest-side: runs inside the postern sandbox, shipped into the guest rootfs as ``themis.agent.retry`` alongside
``themis.agent.services``. The curated-data upstreams behind those services are intermittently unreliable — NCBI 502s,
gnomAD read timeouts — so a bare call fails a whole analysis on a fault a second attempt would clear. ``call`` retries
those, and only those: the decision reads the ``grpc.StatusCode``, never the error text, because a service states a
settled answer *in the status* (``NOT_FOUND`` for "the source holds no record", ``INVALID_ARGUMENT`` for a request it
does not accept). Retrying a settled answer only wastes the upstream's rate limit. Errors are never swallowed: once the
attempts are spent, the last ``grpc.RpcError`` propagates. A retry that ends ``DEADLINE_EXCEEDED`` — this caller's
budget running out, or the service's own deadline — is raised from the failure that prompted it, with a note naming
that failure's status and details: a deadline alone reads as a slow service, when the attempt before may have said
which upstream was down.

``cache_dir`` makes a repeated call within one session free, for a sub-agent thread or a reviewer re-issuing the
author's calls. Put it under ``/workspace/scratch/``, which is never pushed, so it lasts as long as the session and no
longer. The key is the rpc and the request together: the method path plus the request's type
and contents. The rpc has to be in it, because a request type says nothing about which method received it — two rpcs
on one request type, such as two reads whose only input is the session (``google.protobuf.Empty``), would otherwise
answer each other's calls.

The cache holds itself to a fixed size, evicting its oldest entries, so an identical request can still miss. Nothing
about it is allowed to cost a
caller an answer the rpc already gave: an entry this build cannot parse — or that another caller evicted mid-read — is
a miss, and a write that fails is a warning.

``timeout`` is a budget for the whole call — attempts and backoff together — not a per-attempt deadline. Every attempt
names it, so the channel's own default deadline (``services``) never applies to one.

``stream`` is the same for an rpc that streams its response, with one rule ``call`` has no use for: a stream is reissued
only while nothing of it has arrived. Once a message has, a failure ends the stream where it stands and is raised —
resuming would mean stitching a second response onto the first, and nothing says the two are the same answer. Streams
are never cached.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys
import tempfile
import time
from collections.abc import Generator, Iterator
from typing import Protocol, cast

import grpc
from google.protobuf import message, symbol_database

from . import channel

DEFAULT_TIMEOUT_S = channel.DEFAULT_TIMEOUT_S

# What the cache may reach before a write evicts by age: a session's cache stays a small part of the sandbox's disk.
_MAX_CACHE_ENTRIES = 256
_MAX_CACHE_BYTES = 64 * 1024 * 1024
# Suffix of a write still staging; not an entry, and not another caller's to remove.
_STAGING_SUFFIX = '.partial'

# A status that says the call never reached an answer, so the same request may still get one. RESOURCE_EXHAUSTED means
# a response over the transport's message limit — no rpc the hatch reaches rate-limits its caller — and DEADLINE_
# EXCEEDED a budget this caller set; a retry of either repeats itself.
TRANSIENT_CODES = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.ABORTED,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.UNKNOWN,
    }
)


class UnaryMethod[Request: message.Message, Response: message.Message](Protocol):
    """A generated stub's unary-unary method, as ``call`` invokes it: one request, one deadline."""

    def __call__(self, request: Request, timeout: float | None = ...) -> Response: ...


class StreamMethod[Request: message.Message, Response: message.Message](Protocol):
    """A generated stub's unary-stream method, as ``stream`` invokes it: one request, one deadline, many responses."""

    def __call__(self, request: Request, timeout: float | None = ...) -> Iterator[Response]: ...


def _status(error: grpc.RpcError) -> grpc.StatusCode | None:
    """The status of a failed call; ``None`` when the error carries none, which is never retried."""
    return error.code() if isinstance(error, grpc.Call) else None


def _rpc_path(method: object) -> str:
    """The ``/package.Service/Method`` path a generated stub method dials, for the cache key.

    Raises:
        TypeError: If the callable does not carry one, so no key can distinguish it from another
            rpc on the same request type. A generated stub method always does; anything else must
            be called with the cache off.
    """
    path = getattr(method, '_method', None)  # grpc exposes the path on no public attribute
    if path is None:
        raise TypeError(f'{method!r} carries no rpc path, so it cannot be cached; pass cache_dir=None')
    return path.decode() if isinstance(path, bytes) else str(path)


def _entry_path(cache_dir: pathlib.Path, rpc_path: str, request: message.Message) -> pathlib.Path:
    """Where this rpc's response to this exact request is held. The rpc is in the digest, not just the name."""
    keyed = f'{rpc_path}\n'.encode() + request.SerializeToString(deterministic=True)
    return cache_dir / f'{request.DESCRIPTOR.full_name}.{hashlib.sha256(keyed).hexdigest()}'


def _warn(reason: str) -> None:
    """Report a cache fault where the model will see it: the guest's stderr is echoed into the tool result."""
    print(f'[themis.agent.retry] {reason}', file=sys.stderr)


def _read(entry: pathlib.Path) -> message.Message | None:
    """The response an entry holds, or ``None`` when there is none this build can parse.

    An entry written against a contract this build no longer has — a renamed response type, an
    unparseable body — is a miss rather than an error. It is reported and then overwritten by the
    call it failed to answer, so no cache state can wedge a snippet that a cleared cache would not.
    What is *not* checked is that the type named is the one this rpc returns: the name is trusted, so
    a contract change that keeps a response type's name while changing its fields yields a message
    parsed under the old shape rather than a fault. The rpc path in the key is what makes that the
    only way to get one, since no two rpcs write the same entry.

    Reading is one attempt rather than a check and then a read: the entry can be evicted by another
    caller in between, and a cache race must not surface as a failed rpc.
    """
    try:
        stored = entry.read_bytes()
    except FileNotFoundError:
        return None  # the ordinary miss, and what an eviction between callers looks like
    except OSError as error:
        _warn(f'cache entry {entry.name} unreadable ({error}); calling the service instead')
        return None
    name, _, payload = stored.partition(b'\n')
    try:
        response_type = symbol_database.Default().GetSymbol(name.decode())
    except KeyError:
        _warn(f'cache entry names unregistered type {name.decode()!r}; calling the service instead')
        return None
    try:
        return response_type.FromString(payload)
    except message.DecodeError:
        _warn(f'cache entry {entry.name} does not parse as {name.decode()}; calling the service instead')
        return None


def _write(entry: pathlib.Path, response: message.Message) -> None:
    """Store a response under ``entry``, atomically: a reader sees a whole entry or none.

    The staging file is unique per write, so two callers racing on the same entry cannot hand each
    other a half-written body to rename into place.
    """
    payload = response.DESCRIPTOR.full_name.encode() + b'\n' + response.SerializeToString()
    entry.parent.mkdir(parents=True, exist_ok=True)
    _evict(entry.parent, entry, len(payload))
    with tempfile.NamedTemporaryFile(dir=entry.parent, suffix=_STAGING_SUFFIX, delete=False) as staged:
        staged.write(payload)
    os.replace(staged.name, entry)


def _evict(cache_dir: pathlib.Path, target: pathlib.Path, incoming: int) -> None:
    """Drop the oldest entries until a write of ``incoming`` bytes to ``target`` leaves the cache within budget.

    ``target`` is excluded from the accounting: refreshing an entry that is already held replaces it
    rather than adding one, and counting it twice sheds a live entry on every refresh at the cap.
    Staging files are excluded too — one belongs to a write still in flight, and unlinking it would
    fail that caller's rename; a leaked one is not an entry either, and must not hold a slot.
    """
    held: list[tuple[float, int, pathlib.Path]] = []
    for entry in cache_dir.iterdir():
        if entry.is_file() and entry != target and entry.suffix != _STAGING_SUFFIX:
            info = entry.stat()
            held.append((info.st_mtime, info.st_size, entry))
    held.sort()
    entries = 1 + len(held)
    total = incoming + sum(size for _, size, _ in held)
    for _, size, entry in held:
        if entries <= _MAX_CACHE_ENTRIES and total <= _MAX_CACHE_BYTES:
            return
        entry.unlink(missing_ok=True)  # a racing caller may have evicted it already
        entries -= 1
        total -= size


def _name_the_cause(error: grpc.RpcError, earlier: grpc.Call, attempt: int) -> None:
    """Note on ``error`` the failure the attempt before it ended with, so the traceback's last lines show both."""
    error.add_note(
        f'[themis.agent.retry] attempt {attempt} ended DEADLINE_EXCEEDED; attempt {attempt - 1} had failed with '
        f'{earlier.code().name}: {earlier.details() or "no details"}'
    )


def _check_budget(attempts: int, backoff: float, timeout: float) -> None:
    if attempts < 1:
        raise ValueError(f'attempts must be at least 1, got {attempts}')
    if backoff < 0:
        raise ValueError(f'backoff must not be negative, got {backoff}')
    if timeout <= 0:
        raise ValueError(f'timeout must be positive, got {timeout}')


def _wait_before_retrying(
    error: grpc.RpcError, *, earlier: grpc.Call | None, attempt: int, attempts: int, backoff: float, deadline: float
) -> float:
    """How long to wait before reissuing a call that failed with ``error``, or raise it if it is not to be reissued.

    Raises:
        grpc.RpcError: ``error``, when its status is settled, the attempts are spent, or the budget would run out
            during the wait. A ``DEADLINE_EXCEEDED`` on a retry is raised from the failure before it, noted.
    """
    if earlier is not None and _status(error) == grpc.StatusCode.DEADLINE_EXCEEDED:
        _name_the_cause(error, earlier, attempt)
        raise error from cast('grpc.RpcError', earlier)
    if attempt >= attempts or _status(error) not in TRANSIENT_CODES:
        raise error
    wait = backoff * 2 ** (attempt - 1)
    if deadline - time.monotonic() <= wait:
        raise error
    return wait


def call[Request: message.Message, Response: message.Message](
    method: UnaryMethod[Request, Response],
    request: Request,
    *,
    attempts: int = 4,
    backoff: float = 1.0,
    timeout: float = DEFAULT_TIMEOUT_S,
    cache_dir: str | os.PathLike[str] | None = None,
) -> Response:
    """Issue one rpc under a deadline, retrying a transient failure and optionally caching the response.

    Args:
        method: The stub method to call, e.g. ``services.hello().SayHello``.
        request: Its request message.
        attempts: How many times to issue the call in total (1 disables retrying).
        backoff: Seconds to wait after the first failure; each further wait doubles it.
        timeout: Seconds this call gets in total, retries and backoff included — not per attempt.
            Each attempt is issued under what is left, and once it is spent the last failure is
            raised rather than another attempt made.
        cache_dir: Directory to answer a repeated identical call from, and to store this response
            in, as a path or a string. ``None`` (default) neither reads nor writes a cache. A cache
            that cannot be parsed or written never costs the caller the response: the fault goes to
            stderr and the rpc is made, or its answer returned uncached.

    Returns:
        The response message — from the cache when a readable entry is held for this exact call.

    Raises:
        ValueError: If ``attempts`` is below 1, ``backoff`` is negative, or ``timeout`` is not
            positive.
        TypeError: If ``method`` streams its response, which ``stream`` is for; or if ``cache_dir`` is
            set and ``method`` is not a generated stub method, which is the only kind carrying the rpc
            path the key needs.
        grpc.RpcError: The last failure, once a settled status is returned (never retried), the
            attempts are spent, or the budget is. A retry that ends ``DEADLINE_EXCEEDED`` is raised
            from the failure before it (``__cause__``), noted on the error.
    """
    if isinstance(method, grpc.UnaryStreamMultiCallable):
        raise TypeError(f'{method!r} streams its response; issue it with retry.stream, not retry.call')
    _check_budget(attempts, backoff, timeout)
    entry = _entry_path(pathlib.Path(cache_dir), _rpc_path(method), request) if cache_dir is not None else None
    if entry is not None:
        cached = _read(entry)
        if cached is not None:
            return cast('Response', cached)
    attempt = 0
    deadline = time.monotonic() + timeout
    # The transient failure that prompted the attempt in flight; only a call with a status is ever retried.
    earlier: grpc.Call | None = None
    while True:
        attempt += 1
        try:
            response = method(request, timeout=deadline - time.monotonic())
        except grpc.RpcError as error:
            wait = _wait_before_retrying(
                error, earlier=earlier, attempt=attempt, attempts=attempts, backoff=backoff, deadline=deadline
            )
            earlier = cast('grpc.Call', error)  # a status in TRANSIENT_CODES means `_status` found a Call
            time.sleep(wait)
            continue
        if entry is not None:
            try:
                _write(entry, response)
            except OSError as error:
                # The rpc already answered; a full or read-only /workspace must not discard that.
                _warn(f'response not cached: {error}')
        return response


def stream[Request: message.Message, Response: message.Message](
    method: StreamMethod[Request, Response],
    request: Request,
    *,
    attempts: int = 4,
    backoff: float = 1.0,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Generator[Response]:
    """Issue one server-streaming rpc under a deadline, reissuing it on a transient failure before any message.

    Messages are passed on as they arrive. A transient failure before the first one is retried as ``call``
    retries; a failure after it is raised as it stands, since what arrived is a prefix of the answer and a
    reissue would be a second answer, not the rest of the first. Closing the generator early cancels the call.

    Args:
        method: The stub method to call, e.g. ``services.literature().ReadMarkdown``.
        request: Its request message.
        attempts: How many times to issue the call in total (1 disables retrying).
        backoff: Seconds to wait after the first failure; each further wait doubles it.
        timeout: Seconds the whole stream gets, retries and backoff included: the last message has to
            arrive inside it.

    Returns:
        The responses, in order.

    Raises:
        ValueError: If ``attempts`` is below 1, ``backoff`` is negative, or ``timeout`` is not
            positive — when called, before any rpc is issued.
        grpc.RpcError: While iterating: the last failure before the first message, once a settled
            status is returned, the attempts are spent, or the budget is; or the failure after the
            first message, noted with how many had arrived.
    """
    _check_budget(attempts, backoff, timeout)
    return _stream(method, request, attempts=attempts, backoff=backoff, timeout=timeout)


def _stream[Request: message.Message, Response: message.Message](
    method: StreamMethod[Request, Response], request: Request, *, attempts: int, backoff: float, timeout: float
) -> Generator[Response]:
    deadline = time.monotonic() + timeout
    earlier: grpc.Call | None = None
    attempt = 0
    while True:
        attempt += 1
        responses = method(request, timeout=deadline - time.monotonic())
        received = 0
        try:
            for response in responses:
                received += 1
                yield response
            return
        except grpc.RpcError as error:
            if received:
                error.add_note(
                    f'[themis.agent.retry] the stream failed after {received} message(s) and was not reissued: '
                    'what arrived is a prefix of the answer, not the whole of it'
                )
                raise
            wait = _wait_before_retrying(
                error, earlier=earlier, attempt=attempt, attempts=attempts, backoff=backoff, deadline=deadline
            )
            earlier = cast('grpc.Call', error)
            time.sleep(wait)
        finally:
            if isinstance(responses, grpc.Call):
                responses.cancel()  # a no-op on a call that ended; ends one the caller stopped reading
