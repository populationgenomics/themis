"""The harness the sheaf servicer tests share.

An in-process server over any `themis.sheaf.Backend`, a fixture session resolver with two tokens so
two Analyses' repositories can be told apart, builders for the wire messages a publish is made of,
a second writer that publishes through `themis.sheaf.Store` directly over the same backend, and
backend wrappers that count uploads or land a competing publish inside `cas_mutable`. Imported by
the test modules as a module, as `themis/sheaf/tests/conftest.py` is.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import pathlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from typing import override

import grpc
import grpc.aio
import pytest

from themis import sheaf
from themis.clients.auth import session as session_mod
from themis.clients.auth.tests import fixture_session
from themis.rpc import sheaf_pb2, sheaf_pb2_grpc
from themis.services.sheaf import servicer as servicer_mod
from themis.sheaf import backend as backend_mod
from themis.sheaf import refdoc
from themis.sheaf.models import refdoc_pb2
from themis.testing import in_process_grpc

REF = 'refs/heads/main'
SIDE = 'refs/heads/side'
SHA_A, SHA_B, SHA_C = 'a' * 40, 'b' * 40, 'c' * 40
PACK_1, PACK_2 = b'PACK-1 ' * 100, b'PACK-2 ' * 100

OTHER_TOKEN = 'other'
OTHER_ANALYSIS_ID = 'ana-other'
OTHER_METADATA = fixture_session.session_metadata(OTHER_TOKEN)

# Generous enough that every test not about a ceiling clears them.
LIMITS = servicer_mod.Limits(max_publish_bytes=1 << 22, max_refs=64, max_document_bytes=1 << 16)

Moves = Mapping[str, tuple[str | None, str | None]]


def resolver() -> session_mod.SessionResolver:
    seeds = (
        f'{{"{fixture_session.GOOD_TOKEN}": {{"project_id": "{fixture_session.PROJECT_ID}", '
        f'"analysis_id": "{fixture_session.ANALYSIS_ID}"}}, '
        f'"{OTHER_TOKEN}": {{"project_id": "{fixture_session.PROJECT_ID}", "analysis_id": "{OTHER_ANALYSIS_ID}"}}}}'
    )
    return session_mod.fixture_session_resolver_from_json(seeds, var_name='test')


@contextlib.asynccontextmanager
async def serving(
    backend: backend_mod.Backend, limits: servicer_mod.Limits = LIMITS
) -> AsyncIterator[sheaf_pb2_grpc.SheafAsyncStub]:
    servicer = servicer_mod.Servicer(resolver(), backend, limits)
    async with in_process_grpc.serving(
        lambda server: sheaf_pb2_grpc.add_SheafServicer_to_server(servicer, server)
    ) as channel:
        yield sheaf_pb2_grpc.SheafStub(channel)


def reflog_entry(previous: str | None, moves: Moves) -> str:
    """A synthetic reflog commit id, derived from the transitions so a replay produces the same intent."""
    return hashlib.sha1(repr((previous, sorted(moves.items()))).encode()).hexdigest()  # noqa: S324


def updates(moves: Moves, *, reflog_previous: str | None) -> dict[str, sheaf_pb2.RefUpdate]:
    """Wire ref updates for `moves`, with the reflog update the store requires alongside them."""
    updates = {}
    for ref, (old, new) in moves.items():
        update = sheaf_pb2.RefUpdate()
        if old is not None:
            update.old = old
        if new is not None:
            update.new = new
        updates[ref] = update
    if refdoc.REFLOG_REF not in updates:
        entry = sheaf_pb2.RefUpdate(new=reflog_entry(reflog_previous, moves))
        if reflog_previous is not None:
            entry.old = reflog_previous
        updates[refdoc.REFLOG_REF] = entry
    return updates


def descriptor(pack: bytes, *, size: int | None = None, pack_id: str | None = None) -> sheaf_pb2.PackDescriptor:
    return sheaf_pb2.PackDescriptor(
        size=len(pack) if size is None else size, pack_id=sheaf.pack_id(pack) if pack_id is None else pack_id
    )


def intent(
    base_generation: int,
    moves: Moves,
    *,
    packs: Sequence[bytes] = (),
    reflog_previous: str | None = None,
    head: refdoc_pb2.RefTarget | None = None,
    descriptors: Sequence[sheaf_pb2.PackDescriptor] | None = None,
) -> sheaf_pb2.PublishIntent:
    intent = sheaf_pb2.PublishIntent(base_generation=base_generation)
    for ref, update in updates(moves, reflog_previous=reflog_previous).items():
        intent.ref_updates[ref].CopyFrom(update)
    intent.packs.extend([descriptor(pack) for pack in packs] if descriptors is None else descriptors)
    if head is not None:
        intent.head.CopyFrom(head)
    return intent


def chunks(packs: Sequence[bytes], *, chunk_size: int = 128) -> list[sheaf_pb2.PublishRequest]:
    requests = []
    for index, pack in enumerate(packs):
        for start in range(0, len(pack), chunk_size):
            chunk = sheaf_pb2.PublishChunk(pack=index, content=pack[start : start + chunk_size])
            requests.append(sheaf_pb2.PublishRequest(chunk=chunk))
    return requests


def stream(intent: sheaf_pb2.PublishIntent, packs: Sequence[bytes] = ()) -> list[sheaf_pb2.PublishRequest]:
    return [sheaf_pb2.PublishRequest(intent=intent), *chunks(packs)]


async def requests(messages: Sequence[sheaf_pb2.PublishRequest]) -> AsyncIterator[sheaf_pb2.PublishRequest]:
    for message in messages:
        yield message


async def publish(
    stub: sheaf_pb2_grpc.SheafAsyncStub,
    messages: Sequence[sheaf_pb2.PublishRequest],
    metadata: tuple[tuple[str, str], ...] = fixture_session.GOOD_METADATA,
) -> sheaf_pb2.PublishResponse:
    return await stub.Publish(requests(messages), metadata=metadata)


async def fetch(stub: sheaf_pb2_grpc.SheafAsyncStub, pack_id: str) -> bytes:
    request = sheaf_pb2.FetchPackRequest(pack_id=pack_id)
    return b''.join([chunk.content async for chunk in stub.FetchPack(request, metadata=fixture_session.GOOD_METADATA)])


def run[T](
    scenario: Callable[[sheaf_pb2_grpc.SheafAsyncStub], Awaitable[T]],
    backend: backend_mod.Backend,
    limits: servicer_mod.Limits = LIMITS,
) -> T:
    """Serve `backend` in-process and run `scenario` against a stub to it."""

    async def run() -> T:
        async with serving(backend, limits) as stub:
            return await scenario(stub)

    return asyncio.run(run())


def refused(code: grpc.StatusCode) -> Callable[[grpc.aio.AioRpcError], bool]:
    return lambda exc: exc.code() is code


def store_for(backend: backend_mod.Backend, analysis_id: str = fixture_session.ANALYSIS_ID) -> sheaf.Store:
    return sheaf.Store(backend, analysis_id)


def stored_packs(backend: backend_mod.Backend) -> set[str]:
    store = store_for(backend)
    return {info.key for info in backend.list_immutable(store.pack_prefix)}


def seed(backend: backend_mod.Backend, moves: Moves, packs: Sequence[bytes] = ()) -> sheaf.Snapshot:
    """A second writer's publish, through the in-process store over the same backend."""
    store = store_for(backend)
    base = store.read()
    previous = base.tip(refdoc.REFLOG_REF)
    updates = {ref: sheaf.RefUpdate(old, new) for ref, (old, new) in moves.items()}
    updates[refdoc.REFLOG_REF] = sheaf.RefUpdate(previous, reflog_entry(previous, moves))
    return store.publish(base, sheaf.Intent(ref_updates=updates, packs=packs))


class Delegating(backend_mod.Backend):
    """A backend that hands every call to another, for a test to hook one of them."""

    def __init__(self, inner: backend_mod.Backend) -> None:
        self.inner = inner

    @override
    def get_mutable(self, key: str) -> backend_mod.StoredBlob:
        return self.inner.get_mutable(key)

    @override
    def cas_mutable(self, key: str, data: bytes, expected: backend_mod.Generation | None) -> backend_mod.Generation:
        return self.inner.cas_mutable(key, data, expected)

    @override
    def history_mutable(self, key: str) -> list[backend_mod.StoredBlob]:
        return self.inner.history_mutable(key)

    @override
    def put_immutable(self, key: str, data: bytes) -> None:
        self.inner.put_immutable(key, data)

    @override
    def get_immutable(self, key: str) -> bytes:
        return self.inner.get_immutable(key)

    @override
    def list_immutable(self, prefix: str) -> Iterator[backend_mod.ObjectInfo]:
        return self.inner.list_immutable(prefix)


class CountingPuts(Delegating):
    """Counts pack uploads: the local backend's put is idempotent, so a listing cannot show a re-upload."""

    def __init__(self, inner: backend_mod.Backend) -> None:
        super().__init__(inner)
        self.puts = 0

    @override
    def put_immutable(self, key: str, data: bytes) -> None:
        self.puts += 1
        super().put_immutable(key, data)


class RacingCas(Delegating):
    """Lands a competing publish between the servicer's read and its compare-and-swap, once."""

    def __init__(self, inner: backend_mod.Backend, compete: Callable[[], object]) -> None:
        super().__init__(inner)
        self._compete: Callable[[], object] | None = compete

    @override
    def cas_mutable(self, key: str, data: bytes, expected: backend_mod.Generation | None) -> backend_mod.Generation:
        if self._compete is not None:
            compete, self._compete = self._compete, None
            compete()
        return super().cas_mutable(key, data, expected)


@pytest.fixture
def backend(tmp_path: pathlib.Path) -> sheaf.LocalBackend:
    return sheaf.LocalBackend(tmp_path / 'store')


@dataclasses.dataclass(frozen=True)
class Outcome:
    """What a publish attempt left behind, read straight from the store."""

    code: grpc.StatusCode | None
    details: str
    generation: int | None
    packs: set[str]


def attempt(
    backend: backend_mod.Backend, messages: Sequence[sheaf_pb2.PublishRequest], limits: servicer_mod.Limits = LIMITS
) -> Outcome:
    """Run one publish and report its status alongside the store's state afterwards."""
    code, details = None, ''
    try:
        run(lambda stub: publish(stub, messages), backend, limits)
    except grpc.aio.AioRpcError as exc:
        code, details = exc.code(), exc.details() or ''
    return Outcome(code, details, store_for(backend).read().generation, stored_packs(backend))
