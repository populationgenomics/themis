"""The harness the sheaf servicer tests share.

An in-process server over any `themis.sheaf.Backend`, gated by the auth interceptor as the deployed
one is, with the shared authorization fixture's callers and a session resolver holding two tokens so
two Analyses' repositories can be told apart; the credentials each caller presents; builders for the
wire messages a publish is made of; a second writer that publishes through `themis.sheaf.Store`
directly over the same backend; and backend wrappers that count uploads or land a competing publish
inside `cas_mutable`. The server runs on its own thread and the tests drive it with the synchronous
stub, the client every production caller of a stream-in rpc is (rpc-authorization.md). Imported by the
test modules as a module, as `themis/sheaf/tests/conftest.py` is.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import pathlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import override

import grpc
import pytest

from themis import sheaf
from themis.clients.auth import interceptor as interceptor_mod
from themis.clients.auth import session as session_mod
from themis.clients.auth.tests import fixture_session
from themis.rpc import sandbox_options_pb2, sheaf_pb2, sheaf_pb2_grpc
from themis.services.sheaf import servicer as servicer_mod
from themis.sheaf import backend as backend_mod
from themis.sheaf import refdoc
from themis.sheaf.models import refdoc_pb2
from themis.testing import auth as auth_fixture
from themis.testing import in_process_grpc

REF = 'refs/heads/main'
SIDE = 'refs/heads/side'
SHA_A, SHA_B, SHA_C = 'a' * 40, 'b' * 40, 'c' * 40
PACK_1, PACK_2 = b'PACK-1 ' * 100, b'PACK-2 ' * 100

OTHER_TOKEN = 'other'
OTHER_ANALYSIS_ID = 'ana-other'

Metadata = auth_fixture.Metadata

# The callers a sheaf rpc meets, as the gate sees them. The worker, claiming its session, is the
# principal every sheaf rpc names; the developer identity reaches every rpc and names the session
# whose repository it wants in a self claim.
WORKER: Metadata = auth_fixture.WORKER
OTHER: Metadata = (
    *auth_fixture.SANDBOX_JOB,
    *auth_fixture.claim(sandbox_options_pb2.CALLING_AS_WORKER_SESSION, OTHER_TOKEN),
)
BAD_SESSION: Metadata = (
    *auth_fixture.SANDBOX_JOB,
    *auth_fixture.claim(sandbox_options_pb2.CALLING_AS_WORKER_SESSION, 'bad'),
)
AGENT: Metadata = auth_fixture.AGENT
CLU: Metadata = auth_fixture.CLU
CLU_WITH_SESSION: Metadata = auth_fixture.CLU_WITH_SESSION
NOBODY: Metadata = ()

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


def authorizer(session_resolver: session_mod.SessionResolver | None = None) -> interceptor_mod.Authorizer:
    """The shared fixture's callers over the two-token session map, or over `session_resolver`."""
    return dataclasses.replace(
        auth_fixture.authorizer(), session_resolver=resolver() if session_resolver is None else session_resolver
    )


def gate(session_resolver: session_mod.SessionResolver | None = None) -> interceptor_mod.AuthInterceptor:
    """The auth interceptor a sheaf server is gated by."""
    return interceptor_mod.AuthInterceptor(authorizer(session_resolver))


@contextlib.contextmanager
def serving(
    backend: backend_mod.Backend,
    limits: servicer_mod.Limits = LIMITS,
    session_resolver: session_mod.SessionResolver | None = None,
) -> Iterator[sheaf_pb2_grpc.SheafStub]:
    """Serve `backend` behind the gate on its own thread; `session_resolver` unstated is the two-token fixture map."""
    servicer = servicer_mod.Servicer(backend, limits)
    with (
        in_process_grpc.serving_in_thread(
            lambda server: sheaf_pb2_grpc.add_SheafServicer_to_server(servicer, server),
            server_interceptors=(gate(session_resolver),),
        ) as target,
        grpc.insecure_channel(target) as channel,
    ):
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


def publish(
    stub: sheaf_pb2_grpc.SheafStub,
    messages: Sequence[sheaf_pb2.PublishRequest],
    metadata: Metadata = WORKER,
) -> sheaf_pb2.PublishResponse:
    return stub.Publish(iter(messages), metadata=metadata)


def fetch(stub: sheaf_pb2_grpc.SheafStub, pack_id: str, metadata: Metadata = WORKER) -> bytes:
    request = sheaf_pb2.FetchPackRequest(pack_id=pack_id)
    return b''.join(chunk.content for chunk in stub.FetchPack(request, metadata=metadata))


def run[T](
    scenario: Callable[[sheaf_pb2_grpc.SheafStub], T],
    backend: backend_mod.Backend,
    limits: servicer_mod.Limits = LIMITS,
    session_resolver: session_mod.SessionResolver | None = None,
) -> T:
    """Serve `backend` in-process and run `scenario` against a stub to it."""
    with serving(backend, limits, session_resolver) as stub:
        return scenario(stub)


def refused(code: grpc.StatusCode) -> Callable[[grpc.RpcError], bool]:
    return lambda exc: exc.code() is code  # pyright: ignore[reportAttributeAccessIssue]


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
    backend: backend_mod.Backend,
    messages: Sequence[sheaf_pb2.PublishRequest],
    limits: servicer_mod.Limits = LIMITS,
    *,
    metadata: Metadata = WORKER,
    session_resolver: session_mod.SessionResolver | None = None,
) -> Outcome:
    """Run one publish and report its status alongside the store's state afterwards."""
    code, details = None, ''
    try:
        run(lambda stub: stub.Publish(iter(messages), metadata=metadata), backend, limits, session_resolver)
    except grpc.RpcError as exc:
        code, details = exc.code(), exc.details() or ''  # pyright: ignore[reportAttributeAccessIssue]
    return Outcome(code, details, store_for(backend).read().generation, stored_packs(backend))
