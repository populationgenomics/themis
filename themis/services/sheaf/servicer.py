"""The Sheaf servicer: sheaf's storage protocol, served for the repository the session names.

Subclasses the generated `themis.rpc.sheaf_pb2_grpc.SheafServicer`. Every rpc resolves its session
token to an Analysis and opens `themis.sheaf.Store` on that Analysis's prefix, so a caller reaches
its own repository and no other. The protocol — what an intent may contain, how a moved document
is classified, what a pack must hash to — is `themis.sheaf`'s; this module decodes the wire
messages, runs the store's blocking calls off the event loop, and maps each refusal to its status
code. Contract: `schema/proto/themis/rpc/sheaf.proto`; design: `docs/design/sheaf-service.md`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from collections.abc import AsyncIterator
from typing import override

import grpc
import grpc.aio
from google.protobuf import empty_pb2

from themis.clients.auth import session as session_mod
from themis.rpc import sheaf_pb2, sheaf_pb2_grpc
from themis.sheaf import backend as backend_mod
from themis.sheaf import errors, refdoc
from themis.sheaf import store as store_mod

# Under gRPC's 4 MiB default per-message limit, with margin, so a large pack streams.
_CHUNK_SIZE = 1 << 20
# The generation a repository that does not exist yet has on the wire.
_NO_DOCUMENT = 0

# Refusals of the intent itself, all INVALID_ARGUMENT.
_INVALID_INTENT = (
    errors.InvalidRefName,
    errors.RefDeletionRefused,
    errors.ReflogRequired,
    errors.InvalidPackId,
    errors.BookkeepingOnly,
)


@dataclasses.dataclass(frozen=True)
class Limits:
    """The deployment's ceilings on a publish, each refused with RESOURCE_EXHAUSTED beyond it.

    No defaults: the values are deployment configuration, and a publish's bytes are whatever the
    guest pushed with nothing ever reclaimed, so an unstated ceiling is no ceiling.
    """

    max_publish_bytes: int
    max_refs: int
    max_document_bytes: int

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if value <= 0:
                raise ValueError(f'{field.name} must be a positive integer, got {value!r}')


class _RefusalError(Exception):
    """A status to end the rpc with. Raised by the synchronous decoding steps; the rpc aborts with it."""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__(details)
        self.code = code
        self.details = details


def _generation(snapshot: store_mod.Snapshot) -> int:
    return _NO_DOCUMENT if snapshot.generation is None else snapshot.generation


def _ref_update(message: sheaf_pb2.RefUpdate) -> store_mod.RefUpdate:
    return store_mod.RefUpdate(
        old=message.old if message.HasField('old') else None,
        new=message.new if message.HasField('new') else None,
    )


def _head(message: sheaf_pb2.PublishIntent) -> refdoc.Target | None:
    if not message.HasField('head'):
        return None
    try:
        return refdoc.read_target(message.head)
    except ValueError as exc:
        raise _RefusalError(grpc.StatusCode.INVALID_ARGUMENT, f'HEAD: {exc}') from exc


def _decode_intent(message: sheaf_pb2.PublishIntent, limits: Limits) -> store_mod.Intent:
    """Turn the wire intent into the store's, refusing what is wrong with it on its own.

    Everything decidable without the document is decided here, before the document is read and
    before any pack byte: names, ids, deletions, the reflog ref, that a ref outside `refs/sheaf/`
    moves, HEAD's form, each declared pack's id and size, and the per-publish byte ceiling. The
    declared packs are the intent's `stored_packs`: the stream stores each before the publish
    names it.

    Raises:
        _RefusalError: INVALID_ARGUMENT for a malformed intent; RESOURCE_EXHAUSTED over the byte ceiling.
    """
    intent = store_mod.Intent(
        ref_updates={ref: _ref_update(update) for ref, update in message.ref_updates.items()},
        stored_packs=tuple(descriptor.pack_id for descriptor in message.packs),
        head=_head(message),
    )
    try:
        store_mod.validate_intent(intent)
        store_mod.require_moved_refs(intent.ref_updates)
    except _INVALID_INTENT as exc:
        raise _RefusalError(grpc.StatusCode.INVALID_ARGUMENT, str(exc)) from exc
    seen: set[str] = set()
    for index, descriptor in enumerate(message.packs):
        if descriptor.size == 0:
            raise _RefusalError(grpc.StatusCode.INVALID_ARGUMENT, f'pack {index} declares no bytes; a pack has bytes')
        if descriptor.pack_id in seen:
            raise _RefusalError(grpc.StatusCode.INVALID_ARGUMENT, f'pack {index} is declared twice')
        seen.add(descriptor.pack_id)
    declared = sum(descriptor.size for descriptor in message.packs)
    if declared > limits.max_publish_bytes:
        raise _RefusalError(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            f'the declared packs total {declared} bytes; the ceiling is {limits.max_publish_bytes} per publish',
        )
    return intent


def _plan(store: store_mod.Store, base: store_mod.Snapshot, intent: store_mod.Intent, limits: Limits) -> None:
    """Refuse what the intent gets wrong against the document it claims to have read, and the ceilings.

    `base` is at the intent's generation, so a ref not holding its `old` is the caller's error —
    its intent disagrees with the document it was built from — and not a race.

    Raises:
        _RefusalError: INVALID_ARGUMENT for a ref set git cannot store or an `old` the document does not
            hold; RESOURCE_EXHAUSTED when the document the publish would leave is over a ceiling.
    """
    try:
        planned = store.plan(base, intent)
    except (*_INVALID_INTENT, errors.RefConflict) as exc:
        raise _RefusalError(grpc.StatusCode.INVALID_ARGUMENT, str(exc)) from exc
    refs = len(planned.refs)
    if refs > limits.max_refs:
        raise _RefusalError(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            f'the publish would leave {refs} refs; this deployment holds {limits.max_refs} per repository',
        )
    size = len(planned.to_bytes())
    if size > limits.max_document_bytes:
        raise _RefusalError(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            f'the publish would leave a {size}-byte ref document; this deployment holds {limits.max_document_bytes}',
        )


def _settle(live: store_mod.Snapshot, intent: store_mod.Intent) -> sheaf_pb2.PublishResponse:
    """Decide a publish whose base generation the document has left.

    Returns:
        The response when the publish already landed: the current generation, so a retry completes.

    Raises:
        _RefusalError: ABORTED when an unrelated publish won; FAILED_PRECONDITION when a ref the intent
            moves has moved under the caller.
    """
    classification = store_mod.classify(live.refs, intent.ref_updates)
    refs = ', '.join(classification.refs)
    if classification.verdict is store_mod.Verdict.LANDED:
        return sheaf_pb2.PublishResponse(generation=_generation(live))
    if classification.verdict is store_mod.Verdict.LOST_RACE:
        raise _RefusalError(
            grpc.StatusCode.ABORTED,
            f'the document is at generation {_generation(live)}, not the base; {refs} unchanged: rebuild against it',
        )
    raise _RefusalError(grpc.StatusCode.FAILED_PRECONDITION, f'{refs} moved under this publish: not a fast-forward')


async def _drain(requests: AsyncIterator[sheaf_pb2.PublishRequest], budget: int) -> None:
    """Consume what the client has left to send, so the call's status reaches it.

    An outcome decided before the stream is consumed — a refused intent, a publish that already
    landed — is sent while the client may still be writing, and a status sent into a client's
    in-flight write reaches it as a transport error, not the status. Draining until the client
    half-closes delivers it, at the cost of the bytes a refused publish had left to send. `budget`
    bounds a client that will not stop: each message costs its encoded size, chunk or not, at least
    one byte, and the half-close after a stream summing to exactly the budget is still read.
    """
    while True:
        request = await anext(requests, None)
        if request is None:
            return
        budget -= max(request.ByteSize(), 1)
        if budget < 0:
            return


async def _read(store: store_mod.Store) -> store_mod.Snapshot:
    try:
        return await asyncio.to_thread(store.read)
    except errors.CorruptRepository as exc:
        raise _RefusalError(grpc.StatusCode.DATA_LOSS, str(exc)) from exc


async def _receive_pack(
    requests: AsyncIterator[sheaf_pb2.PublishRequest], index: int, descriptor: sheaf_pb2.PackDescriptor
) -> bytes:
    """Read exactly the bytes declared for pack `index`, refusing a stream that delivers anything else.

    Raises:
        _RefusalError: INVALID_ARGUMENT for a chunk of another pack, a second intent, a chunk with no
            bytes, more bytes than declared, a stream that ends short, or bytes that hash to
            something other than the declared id.
    """
    hasher = hashlib.sha256()
    buffers: list[bytes] = []
    received = 0
    while received < descriptor.size:
        request = await anext(requests, None)
        if request is None:
            raise _RefusalError(
                grpc.StatusCode.INVALID_ARGUMENT,
                f'the stream ended with {received} of the {descriptor.size} bytes declared for pack {index}',
            )
        if request.WhichOneof('message') != 'chunk':
            raise _RefusalError(grpc.StatusCode.INVALID_ARGUMENT, 'a publish carries one intent, first, then chunks')
        chunk = request.chunk
        if chunk.pack != index:
            raise _RefusalError(
                grpc.StatusCode.INVALID_ARGUMENT,
                f'a chunk of pack {chunk.pack} arrived while pack {index} was incomplete',
            )
        if not chunk.content:
            raise _RefusalError(grpc.StatusCode.INVALID_ARGUMENT, f'an empty chunk of pack {index}')
        received += len(chunk.content)
        if received > descriptor.size:
            raise _RefusalError(
                grpc.StatusCode.INVALID_ARGUMENT,
                f'pack {index} delivered more than its declared {descriptor.size} bytes',
            )
        hasher.update(chunk.content)
        buffers.append(chunk.content)
    digest = hasher.hexdigest()
    if digest != descriptor.pack_id:
        raise _RefusalError(
            grpc.StatusCode.INVALID_ARGUMENT, f'pack {index} hashes to {digest}, not the declared {descriptor.pack_id}'
        )
    return b''.join(buffers)


async def _store_packs(
    store: store_mod.Store,
    requests: AsyncIterator[sheaf_pb2.PublishRequest],
    declared: list[sheaf_pb2.PackDescriptor],
) -> None:
    """Receive and store each declared pack in turn, then require the stream to end.

    One pack is held at a time: it is hashed, checked and stored before the next begins.

    Raises:
        _RefusalError: INVALID_ARGUMENT as `_receive_pack`, or for a message after the last declared pack.
    """
    for index, descriptor in enumerate(declared):
        data = await _receive_pack(requests, index, descriptor)
        await asyncio.to_thread(store.put_pack, data)
    if await anext(requests, None) is not None:
        raise _RefusalError(grpc.StatusCode.INVALID_ARGUMENT, f'a message after the {len(declared)} declared packs')


class Servicer(sheaf_pb2_grpc.SheafServicer):
    """Serves one repository per call: the Analysis the session token resolves to.

    Holds the backend and the deployment's limits, and no per-repository state; every call opens
    the store afresh on the Analysis's prefix.
    """

    def __init__(
        self,
        session_resolver: session_mod.SessionResolver,
        backend: backend_mod.Backend,
        limits: Limits,
    ) -> None:
        self._session_resolver = session_resolver
        self._backend = backend
        self._limits = limits

    async def _store(self, context: grpc.aio.ServicerContext) -> store_mod.Store:
        session = await session_mod.require_session(context, self._session_resolver)
        return store_mod.Store(self._backend, repo=session.analysis_id)

    @override
    async def ReadRefDoc(self, request: empty_pb2.Empty, context: grpc.aio.ServicerContext) -> sheaf_pb2.RefDocSnapshot:
        store = await self._store(context)
        try:
            snapshot = await _read(store)
        except _RefusalError as refusal:
            await context.abort(refusal.code, refusal.details)
        response = sheaf_pb2.RefDocSnapshot(generation=_generation(snapshot))
        if snapshot.generation is not None:
            response.document.CopyFrom(snapshot.doc.to_message())
        return response

    @override
    async def FetchPack(
        self, request: sheaf_pb2.FetchPackRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[sheaf_pb2.PackChunk]:
        store = await self._store(context)
        try:
            refdoc.validate_pack_id(request.pack_id)
        except errors.InvalidPackId as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        try:
            data = await asyncio.to_thread(store.fetch_pack, request.pack_id)
        except errors.NotFound:
            await context.abort(grpc.StatusCode.NOT_FOUND, f'no pack {request.pack_id}')
        for start in range(0, len(data), _CHUNK_SIZE):
            yield sheaf_pb2.PackChunk(content=data[start : start + _CHUNK_SIZE])

    @override
    async def Publish(
        self, request_iterator: AsyncIterator[sheaf_pb2.PublishRequest], context: grpc.aio.ServicerContext
    ) -> sheaf_pb2.PublishResponse:
        # Twice the ceiling: a publish refused for exceeding it is drained whole up to that much.
        budget = 2 * self._limits.max_publish_bytes
        try:
            store = await self._store(context)
            response = await self._publish(store, request_iterator)
        except _RefusalError as refusal:
            await _drain(request_iterator, budget)
            await context.abort(refusal.code, refusal.details)
        except grpc.aio.AbortError:
            await _drain(request_iterator, budget)
            raise
        await _drain(request_iterator, budget)
        return response

    async def _publish(
        self, store: store_mod.Store, requests: AsyncIterator[sheaf_pb2.PublishRequest]
    ) -> sheaf_pb2.PublishResponse:
        first = await anext(requests, None)
        if first is None or first.WhichOneof('message') != 'intent':
            raise _RefusalError(grpc.StatusCode.INVALID_ARGUMENT, 'the first message of a publish is its intent')
        intent = _decode_intent(first.intent, self._limits)
        base = await _read(store)
        if _generation(base) != first.intent.base_generation:
            return _settle(base, intent)
        _plan(store, base, intent, self._limits)
        await _store_packs(store, requests, list(first.intent.packs))
        try:
            published = await asyncio.to_thread(store.publish, base, intent)
        except errors.RaceLost:
            return _settle(await _read(store), intent)
        return sheaf_pb2.PublishResponse(generation=_generation(published))
