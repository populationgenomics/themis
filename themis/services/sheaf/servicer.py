"""The Sheaf servicer: sheaf's storage protocol, served for the repository the call's session names.

Subclasses the generated `themis.rpc.sheaf_pb2_grpc.SheafServicer`. Every rpc opens
`themis.sheaf.Store` on the prefix of the Analysis the call's `AuthContext` carries — the session
the auth interceptor resolved from the caller's claim — so a caller reaches its own repository and no
other. The protocol — what an intent may contain, how a moved document is classified, what a pack
must hash to — is `themis.sheaf`'s; this module decodes the wire messages, runs the store's blocking
calls off the event loop, and raises each refusal as the `StatusError` the gate ends the call with, so
the servicer never touches a context's status itself. Contract: `schema/proto/themis/rpc/sheaf.proto`;
design: `docs/design/sheaf-service.md`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import hashlib
from collections.abc import AsyncIterator, Sequence
from typing import override

import grpc
import grpc.aio
from google.protobuf import empty_pb2, timestamp_pb2

from themis.clients.auth import context as context_mod
from themis.clients.auth import interceptor as interceptor_mod
from themis.rpc import sheaf_pb2, sheaf_pb2_grpc
from themis.sheaf import backend as backend_mod
from themis.sheaf import errors, refdoc
from themis.sheaf import store as store_mod

# Under gRPC's 4 MiB default per-message limit, with margin, so a large pack streams.
_CHUNK_SIZE = 1 << 20
# The generation a repository that does not exist yet has on the wire.
_NO_DOCUMENT = 0
# Packs one SignPackUrls call may name: each costs a signing request, so a larger want asks in batches.
_MAX_SIGNED_PACKS = 256
# Signing requests in flight per call. A signature is one signBlob round trip, so eight at a time sign a full call
# in 32 rounds. The signer's HTTP session pools ten connections per host, shared by the instance's concurrent
# calls; a request past them opens a connection it discards afterwards, a cost and not a failure.
_SIGNING_CONCURRENCY = 8
# Ids a refusal names before it only counts the rest: the message rides in a trailer whose size gRPC caps, and one
# naming every id of a full call arrives as RESOURCE_EXHAUSTED instead.
_NAMED_IDS = 3
# The longest a V4 signed URL may live.
_MAX_URL_LIFETIME_SECONDS = 7 * 24 * 60 * 60

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
    """The deployment's bounds: its ceilings on a publish, and how long a signed pack URL lives.

    A publish over a ceiling is refused with RESOURCE_EXHAUSTED. No defaults: the values are
    deployment configuration, and a publish's bytes are whatever the guest pushed with nothing ever
    reclaimed, so an unstated ceiling is no ceiling; a signed URL is a bearer capability for its
    whole lifetime, so its lifetime is stated too.

    Raises:
        ValueError: If a value is not a positive integer, or the URL lifetime exceeds the seven days
            a V4 signature can carry.
    """

    max_publish_bytes: int
    max_refs: int
    max_document_bytes: int
    pack_url_lifetime_seconds: int

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if value <= 0:
                raise ValueError(f'{field.name} must be a positive integer, got {value!r}')
        if self.pack_url_lifetime_seconds > _MAX_URL_LIFETIME_SECONDS:
            raise ValueError(
                f'pack_url_lifetime_seconds is {self.pack_url_lifetime_seconds}; '
                f'a V4 signed URL lives at most {_MAX_URL_LIFETIME_SECONDS}'
            )

    @property
    def pack_url_lifetime(self) -> datetime.timedelta:
        """How long a signed pack URL works for."""
        return datetime.timedelta(seconds=self.pack_url_lifetime_seconds)


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
        raise interceptor_mod.StatusError(grpc.StatusCode.INVALID_ARGUMENT, f'HEAD: {exc}') from exc


def _decode_intent(message: sheaf_pb2.PublishIntent, limits: Limits) -> store_mod.Intent:
    """Turn the wire intent into the store's, refusing what is wrong with it on its own.

    Everything decidable without the document is decided here, before the document is read and
    before any pack byte: names, ids, deletions, the reflog ref, that a ref outside `refs/sheaf/`
    moves, HEAD's form, each declared pack's id and size, and the per-publish byte ceiling. The
    declared packs are the intent's `stored_packs`: the stream stores each before the publish
    names it.

    Raises:
        interceptor_mod.StatusError: INVALID_ARGUMENT for a malformed intent; RESOURCE_EXHAUSTED over the byte ceiling.
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
        raise interceptor_mod.StatusError(grpc.StatusCode.INVALID_ARGUMENT, str(exc)) from exc
    seen: set[str] = set()
    for index, descriptor in enumerate(message.packs):
        if descriptor.size == 0:
            raise interceptor_mod.StatusError(
                grpc.StatusCode.INVALID_ARGUMENT, f'pack {index} declares no bytes; a pack has bytes'
            )
        if descriptor.pack_id in seen:
            raise interceptor_mod.StatusError(grpc.StatusCode.INVALID_ARGUMENT, f'pack {index} is declared twice')
        seen.add(descriptor.pack_id)
    declared = sum(descriptor.size for descriptor in message.packs)
    if declared > limits.max_publish_bytes:
        raise interceptor_mod.StatusError(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            f'the declared packs total {declared} bytes; the ceiling is {limits.max_publish_bytes} per publish',
        )
    return intent


def _plan(base: store_mod.Snapshot, intent: store_mod.Intent, limits: Limits) -> None:
    """Refuse what the intent gets wrong against the document it claims to have read, and the ceilings.

    `base` is at the intent's generation, so a ref not holding its `old` is the caller's error —
    its intent disagrees with the document it was built from — and not a race.

    Raises:
        interceptor_mod.StatusError: INVALID_ARGUMENT for a ref set git cannot store or an `old` the document does not
            hold; RESOURCE_EXHAUSTED when the document the publish would leave is over a ceiling.
    """
    try:
        planned = store_mod.plan(base, intent)
    except (*_INVALID_INTENT, errors.RefConflict) as exc:
        raise interceptor_mod.StatusError(grpc.StatusCode.INVALID_ARGUMENT, str(exc)) from exc
    refs = len(planned.refs)
    if refs > limits.max_refs:
        raise interceptor_mod.StatusError(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            f'the publish would leave {refs} refs; this deployment holds {limits.max_refs} per repository',
        )
    size = len(planned.to_bytes())
    if size > limits.max_document_bytes:
        raise interceptor_mod.StatusError(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            f'the publish would leave a {size}-byte ref document; this deployment holds {limits.max_document_bytes}',
        )


def _settle(live: store_mod.Snapshot, intent: store_mod.Intent) -> sheaf_pb2.PublishResponse:
    """Decide a publish whose base generation the document has left.

    Returns:
        The response when the publish already landed: the current generation, so a retry completes.

    Raises:
        interceptor_mod.StatusError: ABORTED when an unrelated publish won; FAILED_PRECONDITION when a ref the intent
            moves has moved under the caller.
    """
    classification = store_mod.classify(live.refs, intent.ref_updates)
    refs = ', '.join(classification.refs)
    if classification.verdict is store_mod.Verdict.LANDED:
        return sheaf_pb2.PublishResponse(generation=_generation(live))
    if classification.verdict is store_mod.Verdict.LOST_RACE:
        raise interceptor_mod.StatusError(
            grpc.StatusCode.ABORTED,
            f'the document is at generation {_generation(live)}, not the base; {refs} unchanged: rebuild against it',
        )
    raise interceptor_mod.StatusError(
        grpc.StatusCode.FAILED_PRECONDITION, f'{refs} moved under this publish: not a fast-forward'
    )


async def _read(store: store_mod.Store) -> store_mod.Snapshot:
    try:
        return await asyncio.to_thread(store.read)
    except errors.CorruptRepository as exc:
        raise interceptor_mod.StatusError(grpc.StatusCode.DATA_LOSS, str(exc)) from exc


async def _receive_pack(
    requests: AsyncIterator[sheaf_pb2.PublishRequest], index: int, descriptor: sheaf_pb2.PackDescriptor
) -> bytes:
    """Read exactly the bytes declared for pack `index`, refusing a stream that delivers anything else.

    Raises:
        interceptor_mod.StatusError: INVALID_ARGUMENT for a chunk of another pack, a second intent, a chunk with no
            bytes, more bytes than declared, a stream that ends short, or bytes that hash to
            something other than the declared id.
    """
    hasher = hashlib.sha256()
    buffers: list[bytes] = []
    received = 0
    while received < descriptor.size:
        request = await anext(requests, None)
        if request is None:
            raise interceptor_mod.StatusError(
                grpc.StatusCode.INVALID_ARGUMENT,
                f'the stream ended with {received} of the {descriptor.size} bytes declared for pack {index}',
            )
        if request.WhichOneof('message') != 'chunk':
            raise interceptor_mod.StatusError(
                grpc.StatusCode.INVALID_ARGUMENT, 'a publish carries one intent, first, then chunks'
            )
        chunk = request.chunk
        if chunk.pack != index:
            raise interceptor_mod.StatusError(
                grpc.StatusCode.INVALID_ARGUMENT,
                f'a chunk of pack {chunk.pack} arrived while pack {index} was incomplete',
            )
        if not chunk.content:
            raise interceptor_mod.StatusError(grpc.StatusCode.INVALID_ARGUMENT, f'an empty chunk of pack {index}')
        received += len(chunk.content)
        if received > descriptor.size:
            raise interceptor_mod.StatusError(
                grpc.StatusCode.INVALID_ARGUMENT,
                f'pack {index} delivered more than its declared {descriptor.size} bytes',
            )
        hasher.update(chunk.content)
        buffers.append(chunk.content)
    digest = hasher.hexdigest()
    if digest != descriptor.pack_id:
        raise interceptor_mod.StatusError(
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
        interceptor_mod.StatusError: INVALID_ARGUMENT as `_receive_pack`, or for a message after the last
            declared pack.
    """
    for index, descriptor in enumerate(declared):
        data = await _receive_pack(requests, index, descriptor)
        await asyncio.to_thread(store.put_pack, data)
    if await anext(requests, None) is not None:
        raise interceptor_mod.StatusError(
            grpc.StatusCode.INVALID_ARGUMENT, f'a message after the {len(declared)} declared packs'
        )


def _requested_pack_ids(request: sheaf_pb2.SignPackUrlsRequest) -> list[str]:
    """The ids a signing request names, refusing what is wrong with the list on its own.

    Raises:
        interceptor_mod.StatusError: INVALID_ARGUMENT for an empty list, more than the per-call maximum, an id
            that is not a pack id, or an id named twice.
    """
    ids = list(request.pack_ids)
    if not ids:
        raise interceptor_mod.StatusError(grpc.StatusCode.INVALID_ARGUMENT, 'name at least one pack to sign')
    if len(ids) > _MAX_SIGNED_PACKS:
        raise interceptor_mod.StatusError(
            grpc.StatusCode.INVALID_ARGUMENT,
            f'{len(ids)} packs named; one call signs at most {_MAX_SIGNED_PACKS}, so ask in batches',
        )
    seen: set[str] = set()
    for ident in ids:
        try:
            refdoc.validate_pack_id(ident)
        except errors.InvalidPackId as exc:
            raise interceptor_mod.StatusError(grpc.StatusCode.INVALID_ARGUMENT, str(exc)) from exc
        if ident in seen:
            raise interceptor_mod.StatusError(grpc.StatusCode.INVALID_ARGUMENT, f'pack {ident} is named twice')
        seen.add(ident)
    return ids


def _sign_all(store: store_mod.Store, ids: Sequence[str], lifetime: datetime.timedelta) -> list[store_mod.SignedPack]:
    """Size and sign each pack, in the order named.

    Raises:
        interceptor_mod.StatusError: DATA_LOSS if a pack the document lists is not stored; UNIMPLEMENTED if
            this deployment's storage cannot sign.
    """
    try:
        return store.sign_packs(ids, lifetime, concurrency=_SIGNING_CONCURRENCY)
    except errors.PacksAbsent as exc:
        raise interceptor_mod.StatusError(
            grpc.StatusCode.DATA_LOSS,
            f'the document lists {len(exc.idents)} packs the store does not hold ({_some_of(exc.idents)})',
        ) from exc
    except errors.SigningUnsupported as exc:
        raise interceptor_mod.StatusError(grpc.StatusCode.UNIMPLEMENTED, str(exc)) from exc


def _some_of(ids: Sequence[str]) -> str:
    named = ', '.join(ids[:_NAMED_IDS])
    rest = len(ids) - _NAMED_IDS
    return f'{named} and {rest} more' if rest > 0 else named


def _signed_pack(signed: store_mod.SignedPack) -> sheaf_pb2.SignedPack:
    expire_time = timestamp_pb2.Timestamp()
    expire_time.FromDatetime(signed.expire_time)
    return sheaf_pb2.SignedPack(pack_id=signed.ident, url=signed.url, size=signed.size, expire_time=expire_time)


class Servicer(sheaf_pb2_grpc.SheafServicer):
    """Serves one repository per call: the Analysis the call's bound `AuthContext` names.

    Holds the backend and the deployment's limits, and no per-repository state; every call opens
    the store afresh on the Analysis's prefix. Signing a pack URL is the backend's to do, so a
    backend that cannot sign answers `SignPackUrls` UNIMPLEMENTED and every other rpc as usual.
    """

    def __init__(self, backend: backend_mod.Backend, limits: Limits) -> None:
        self._backend = backend
        self._limits = limits

    def _store(self) -> store_mod.Store:
        """Open the store on the Analysis the call's session names.

        Raises:
            interceptor_mod.StatusError: INVALID_ARGUMENT when the admitted call named no session — a
                developer calling as themself without one — since no request names a repository.
        """
        session = context_mod.current().session
        if session is None:
            raise interceptor_mod.StatusError(
                grpc.StatusCode.INVALID_ARGUMENT,
                'every sheaf rpc serves the repository of the session its claim names, and this call named none',
            )
        return store_mod.Store(self._backend, repo=session.analysis_id)

    @override
    async def ReadRefDoc(self, request: empty_pb2.Empty, context: grpc.aio.ServicerContext) -> sheaf_pb2.RefDocSnapshot:
        del context
        snapshot = await _read(self._store())
        response = sheaf_pb2.RefDocSnapshot(generation=_generation(snapshot))
        if snapshot.generation is not None:
            response.document.CopyFrom(snapshot.doc.to_message())
        return response

    @override
    async def FetchPack(
        self, request: sheaf_pb2.FetchPackRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[sheaf_pb2.PackChunk]:
        del context
        store = self._store()
        try:
            refdoc.validate_pack_id(request.pack_id)
        except errors.InvalidPackId as exc:
            raise interceptor_mod.StatusError(grpc.StatusCode.INVALID_ARGUMENT, str(exc)) from exc
        try:
            data = await asyncio.to_thread(store.fetch_pack, request.pack_id)
        except errors.NotFound as exc:
            raise interceptor_mod.StatusError(grpc.StatusCode.NOT_FOUND, f'no pack {request.pack_id}') from exc
        for start in range(0, len(data), _CHUNK_SIZE):
            yield sheaf_pb2.PackChunk(content=data[start : start + _CHUNK_SIZE])

    @override
    async def SignPackUrls(
        self, request: sheaf_pb2.SignPackUrlsRequest, context: grpc.aio.ServicerContext
    ) -> sheaf_pb2.SignPackUrlsResponse:
        del context
        store = self._store()
        ids = _requested_pack_ids(request)
        listed = set((await _read(store)).packs)
        unlisted = [ident for ident in ids if ident not in listed]
        if unlisted:
            raise interceptor_mod.StatusError(
                grpc.StatusCode.NOT_FOUND,
                f'the current document does not list {len(unlisted)} of the packs named ({_some_of(unlisted)}): '
                'read it again and ask for what it lists',
            )
        signed = await asyncio.to_thread(_sign_all, store, ids, self._limits.pack_url_lifetime)
        return sheaf_pb2.SignPackUrlsResponse(packs=[_signed_pack(pack) for pack in signed])

    @override
    async def Publish(
        self, request_iterator: AsyncIterator[sheaf_pb2.PublishRequest], context: grpc.aio.ServicerContext
    ) -> sheaf_pb2.PublishResponse:
        del context
        return await self._publish(self._store(), request_iterator)

    async def _publish(
        self, store: store_mod.Store, requests: AsyncIterator[sheaf_pb2.PublishRequest]
    ) -> sheaf_pb2.PublishResponse:
        first = await anext(requests, None)
        if first is None or first.WhichOneof('message') != 'intent':
            raise interceptor_mod.StatusError(
                grpc.StatusCode.INVALID_ARGUMENT, 'the first message of a publish is its intent'
            )
        intent = _decode_intent(first.intent, self._limits)
        base = await _read(store)
        if _generation(base) != first.intent.base_generation:
            return _settle(base, intent)
        _plan(base, intent, self._limits)
        await _store_packs(store, requests, list(first.intent.packs))
        try:
            published = await asyncio.to_thread(store.publish, base, intent)
        except errors.RaceLost:
            return _settle(await _read(store), intent)
        return sheaf_pb2.PublishResponse(generation=_generation(published))
