"""Retrying (and optionally caching) calls to the internal Themis services, for code-mode snippets.

Guest-side: runs inside the postern sandbox, shipped into the guest rootfs as ``themis.agent.retry`` alongside
``themis.agent.services``. The curated-data upstreams behind those services are intermittently unreliable — NCBI 502s,
gnomAD read timeouts — so a bare call fails a whole analysis on a fault a second attempt would clear. ``call`` retries
those, and only those: the decision reads the ``grpc.StatusCode``, never the error text, because a service states a
settled answer *in the status* (``NOT_FOUND`` for "the source holds no record", ``INVALID_ARGUMENT`` for a request it
does not accept). Retrying a settled answer only wastes the upstream's rate limit. Errors are never swallowed: once the
attempts are spent, the last ``grpc.RpcError`` propagates.

``cache_dir`` makes a repeated call within one session free. ``/workspace`` persists across turns (it is checkpointed
to the store), so a cache under it survives a re-run — and inflates every checkpoint, which is why it is opt-in and
per-call rather than always on. The key is the rpc and the request together: the method path plus the request's type
and contents. The rpc has to be in it, because a request type says nothing about which method received it — two rpcs
on one request type would otherwise answer each other's calls, and ``store.proto`` already has two on
``google.protobuf.Empty``.

A cache under ``/workspace`` is also restored, not just written, and a scratch tree too large to restore is abandoned
whole rather than trimmed — the session then starts with none of it (`sync.py`). So the cache holds itself to a small
fraction of that limit, evicting its oldest entries rather than growing into it. Nothing about it is allowed to cost a
caller an answer the rpc already gave: an entry this build cannot parse — or that another caller evicted mid-read — is
a miss, and a write that fails is a warning.

``timeout`` is a budget for the whole call — attempts and backoff together — not a per-attempt deadline. Every attempt
names it, so the channel's own default deadline (``services``) never applies to one.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys
import tempfile
import time
from typing import Protocol, cast

import grpc
from google.protobuf import message, symbol_database

from . import channel

DEFAULT_TIMEOUT_S = channel.DEFAULT_TIMEOUT_S

# What the cache may reach before a write evicts by age. sync.py restores the scratch tree at 20k entries / 512 MiB
# and abandons an over-large one whole, so a cache that grew into that ceiling would cost a session everything else
# under /workspace.
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
        TypeError: If ``cache_dir`` is set and ``method`` is not a generated stub method, which is
            the only kind carrying the rpc path the key needs.
        grpc.RpcError: The last failure, once a settled status is returned (never retried), the
            attempts are spent, or the budget is.
    """
    if attempts < 1:
        raise ValueError(f'attempts must be at least 1, got {attempts}')
    if backoff < 0:
        raise ValueError(f'backoff must not be negative, got {backoff}')
    if timeout <= 0:
        raise ValueError(f'timeout must be positive, got {timeout}')
    entry = _entry_path(pathlib.Path(cache_dir), _rpc_path(method), request) if cache_dir is not None else None
    if entry is not None:
        cached = _read(entry)
        if cached is not None:
            return cast('Response', cached)
    attempt = 0
    deadline = time.monotonic() + timeout
    while True:
        attempt += 1
        try:
            response = method(request, timeout=deadline - time.monotonic())
        except grpc.RpcError as error:
            if attempt >= attempts or _status(error) not in TRANSIENT_CODES:
                raise
            wait = backoff * 2 ** (attempt - 1)
            if deadline - time.monotonic() <= wait:
                raise
            time.sleep(wait)
            continue
        if entry is not None:
            try:
                _write(entry, response)
            except OSError as error:
                # The rpc already answered; a full or read-only /workspace must not discard that.
                _warn(f'response not cached: {error}')
        return response
