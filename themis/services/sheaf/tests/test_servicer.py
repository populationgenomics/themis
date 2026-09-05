"""Behaviour tests for the Sheaf servicer over an in-process grpc.aio server.

The store is `LocalBackend` over a temporary directory; the session resolver is a fixture map with
two tokens, so two Analyses' repositories can be told apart. A second writer, where a test needs
one, publishes through `themis.sheaf.Store` directly over the same backend — the seam every in-process writer
uses — and a race between the servicer's read and its compare-and-swap is staged by a
backend wrapper that lands a competing publish inside `cas_mutable`.
"""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import AsyncIterator, Callable

import grpc
import grpc.aio
import pytest
from google.protobuf import empty_pb2

from themis import sheaf
from themis.clients.auth.tests import fixture_session
from themis.rpc import sheaf_pb2, sheaf_pb2_grpc
from themis.services.sheaf import servicer as servicer_mod
from themis.services.sheaf.tests import conftest
from themis.sheaf import refdoc
from themis.sheaf.models import refdoc_pb2

REF = conftest.REF
SIDE = conftest.SIDE
SHA_A = conftest.SHA_A
SHA_B = conftest.SHA_B
SHA_C = conftest.SHA_C
PACK_1 = conftest.PACK_1
PACK_2 = conftest.PACK_2
OTHER_ANALYSIS_ID = conftest.OTHER_ANALYSIS_ID
OTHER_METADATA = conftest.OTHER_METADATA
LIMITS = conftest.LIMITS


def test_missing_session_token_is_unauthenticated(backend: sheaf.LocalBackend) -> None:
    with pytest.raises(grpc.aio.AioRpcError, check=conftest.refused(grpc.StatusCode.UNAUTHENTICATED)):
        conftest.run(lambda stub: stub.ReadRefDoc(empty_pb2.Empty()), backend)


def test_unresolvable_token_is_permission_denied(backend: sheaf.LocalBackend) -> None:
    with pytest.raises(grpc.aio.AioRpcError, check=conftest.refused(grpc.StatusCode.PERMISSION_DENIED)):
        conftest.run(
            lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=fixture_session.session_metadata('bad')), backend
        )


def test_a_repository_that_does_not_exist_reads_as_generation_zero_and_no_document(
    backend: sheaf.LocalBackend,
) -> None:
    snapshot = conftest.run(
        lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=fixture_session.GOOD_METADATA), backend
    )
    assert snapshot.generation == 0
    assert not snapshot.HasField('document')


def test_each_analysis_reaches_only_its_own_repository(backend: sheaf.LocalBackend) -> None:
    async def scenario(
        stub: sheaf_pb2_grpc.SheafAsyncStub,
    ) -> tuple[sheaf_pb2.RefDocSnapshot, sheaf_pb2.RefDocSnapshot]:
        await conftest.publish(stub, conftest.stream(conftest.intent(0, {REF: (None, SHA_A)})))
        mine = await stub.ReadRefDoc(empty_pb2.Empty(), metadata=fixture_session.GOOD_METADATA)
        theirs = await stub.ReadRefDoc(empty_pb2.Empty(), metadata=OTHER_METADATA)
        return mine, theirs

    mine, theirs = conftest.run(scenario, backend)
    assert mine.document.refs[REF].oid == SHA_A
    assert theirs.generation == 0
    assert not theirs.HasField('document')
    assert conftest.store_for(backend, OTHER_ANALYSIS_ID).read().generation is None


def test_a_pack_another_analysis_stored_is_not_found(backend: sheaf.LocalBackend) -> None:
    conftest.seed(backend, {REF: (None, SHA_A)}, packs=[PACK_1])

    async def scenario(stub: sheaf_pb2_grpc.SheafAsyncStub) -> bytes:
        request = sheaf_pb2.FetchPackRequest(pack_id=sheaf.pack_id(PACK_1))
        return b''.join([chunk.content async for chunk in stub.FetchPack(request, metadata=OTHER_METADATA)])

    with pytest.raises(grpc.aio.AioRpcError, check=conftest.refused(grpc.StatusCode.NOT_FOUND)):
        conftest.run(scenario, backend)


def test_a_publish_lands_under_the_analysis_its_session_names(backend: sheaf.LocalBackend) -> None:
    messages = conftest.stream(conftest.intent(0, {REF: (None, SHA_B)}, packs=[PACK_2]), [PACK_2])
    conftest.run(lambda stub: conftest.publish(stub, messages, metadata=OTHER_METADATA), backend)
    assert conftest.store_for(backend, OTHER_ANALYSIS_ID).read().tip(REF) == SHA_B
    assert conftest.store_for(backend).read().generation is None
    assert conftest.store_for(backend, OTHER_ANALYSIS_ID).fetch_pack(sheaf.pack_id(PACK_2)) == PACK_2


# --- the first publish, and reading it back --------------------------------------------------------


def test_a_first_publish_lands_and_reads_back(backend: sheaf.LocalBackend) -> None:
    async def scenario(
        stub: sheaf_pb2_grpc.SheafAsyncStub,
    ) -> tuple[sheaf_pb2.PublishResponse, sheaf_pb2.RefDocSnapshot]:
        response = await conftest.publish(
            stub, conftest.stream(conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1]), [PACK_1])
        )
        return response, await stub.ReadRefDoc(empty_pb2.Empty(), metadata=fixture_session.GOOD_METADATA)

    response, snapshot = conftest.run(scenario, backend)
    assert response.generation != 0
    assert snapshot.generation == response.generation
    assert snapshot.document.refs[REF].oid == SHA_A
    assert snapshot.document.refs[refdoc.REFLOG_REF].oid == conftest.reflog_entry(None, {REF: (None, SHA_A)})
    assert list(snapshot.document.packs) == [sheaf.pack_id(PACK_1)]
    assert snapshot.document.head.ref == REF, 'HEAD is derived on a first publish that leaves a branch'


def test_packs_arrive_in_order_and_each_is_named(backend: sheaf.LocalBackend) -> None:
    intent = conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1, PACK_2])
    conftest.run(lambda stub: conftest.publish(stub, conftest.stream(intent, [PACK_1, PACK_2])), backend)
    snapshot = conftest.store_for(backend).read()
    assert set(snapshot.packs) == {sheaf.pack_id(PACK_1), sheaf.pack_id(PACK_2)}
    assert conftest.store_for(backend).fetch_pack(sheaf.pack_id(PACK_2)) == PACK_2


def test_an_intent_alone_is_a_complete_publish_when_it_declares_no_packs(backend: sheaf.LocalBackend) -> None:
    conftest.run(
        lambda stub: conftest.publish(stub, conftest.stream(conftest.intent(0, {REF: (None, SHA_A)}))), backend
    )
    assert conftest.store_for(backend).read().tip(REF) == SHA_A


def test_a_set_head_is_recorded_and_an_unset_one_carries_over(backend: sheaf.LocalBackend) -> None:
    async def scenario(stub: sheaf_pb2_grpc.SheafAsyncStub) -> None:
        first = conftest.intent(0, {REF: (None, SHA_A)}, head=refdoc_pb2.RefTarget(oid=SHA_A))
        response = await conftest.publish(stub, conftest.stream(first))
        second = conftest.intent(
            response.generation,
            {REF: (SHA_A, SHA_B)},
            reflog_previous=conftest.reflog_entry(None, {REF: (None, SHA_A)}),
        )
        await conftest.publish(stub, conftest.stream(second))

    conftest.run(scenario, backend)
    assert conftest.store_for(backend).read().doc.head == sheaf.DirectTarget(SHA_A)


def test_a_field_this_build_does_not_model_is_read_back_intact(backend: sheaf.LocalBackend) -> None:
    """The document is carried as a message so a later build's field survives the round trip."""
    published = conftest.seed(backend, {REF: (None, SHA_A)})
    from_the_future = b'\xf8\x06\x2a'  # field 111, varint 42
    store = conftest.store_for(backend)
    backend.cas_mutable(store.ref_key, published.doc.to_bytes() + from_the_future, published.generation)

    snapshot = conftest.run(
        lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=fixture_session.GOOD_METADATA), backend
    )

    assert snapshot.document.SerializeToString().endswith(from_the_future)


# --- FetchPack --------------------------------------------------------------------------------------


def test_fetch_pack_streams_the_bytes_the_document_names(backend: sheaf.LocalBackend) -> None:
    big = bytes(range(256)) * ((servicer_mod._CHUNK_SIZE // 256) + 1)  # more than one chunk
    conftest.seed(backend, {REF: (None, SHA_A)}, packs=[big])
    assert conftest.run(lambda stub: conftest.fetch(stub, sheaf.pack_id(big)), backend) == big


def test_fetch_pack_of_an_unknown_id_is_not_found(backend: sheaf.LocalBackend) -> None:
    with pytest.raises(grpc.aio.AioRpcError, check=conftest.refused(grpc.StatusCode.NOT_FOUND)):
        conftest.run(lambda stub: conftest.fetch(stub, 'f' * 64), backend)


@pytest.mark.parametrize('pack_id', ['', 'F' * 64, 'f' * 63, '../refs.pb', sheaf.pack_id(PACK_1) + '\n'])
def test_fetch_pack_of_a_malformed_id_is_invalid_before_the_store_is_consulted(
    backend: sheaf.LocalBackend, pack_id: str
) -> None:
    with pytest.raises(grpc.aio.AioRpcError, check=conftest.refused(grpc.StatusCode.INVALID_ARGUMENT)):
        conftest.run(lambda stub: conftest.fetch(stub, pack_id), backend)


# --- Publish refusals --------------------------------------------------------------------------------


def _bad_intents() -> dict[str, Callable[[], list[sheaf_pb2.PublishRequest]]]:
    """One publish per cause the contract names that is decided from the intent alone, before any pack byte."""
    zero = refdoc.ZERO_OBJECT_ID
    return {
        'a ref name git cannot hold': lambda: conftest.stream(
            conftest.intent(0, {'refs/heads/two words': (None, SHA_A)})
        ),
        'an unqualified ref name': lambda: conftest.stream(conftest.intent(0, {'main': (None, SHA_A)})),
        'an object id git cannot hold': lambda: conftest.stream(conftest.intent(0, {REF: (None, 'nope')})),
        'the zero id as new (a deletion)': lambda: conftest.stream(conftest.intent(0, {REF: (None, zero)})),
        'the zero id as old': lambda: conftest.stream(conftest.intent(0, {REF: (zero, SHA_A)})),
        'a deletion': lambda: conftest.stream(conftest.intent(0, {REF: (None, None)})),
        'two names that collide as directory and file': lambda: conftest.stream(
            conftest.intent(0, {'refs/heads/a': (None, SHA_A), 'refs/heads/a/b': (None, SHA_B)})
        ),
        'no ref outside refs/sheaf/': lambda: conftest.stream(conftest.intent(0, {refdoc.REFLOG_REF: (None, SHA_A)})),
        'only bookkeeping, several refs': lambda: conftest.stream(
            conftest.intent(0, {refdoc.REFLOG_REF: (None, SHA_A), 'refs/sheaf/x': (None, SHA_B)})
        ),
        'a HEAD naming neither an object nor a ref': lambda: conftest.stream(
            conftest.intent(0, {REF: (None, SHA_A)}, head=refdoc_pb2.RefTarget())
        ),
        'a HEAD naming a ref git cannot hold': lambda: conftest.stream(
            conftest.intent(0, {REF: (None, SHA_A)}, head=refdoc_pb2.RefTarget(ref='heads/main'))
        ),
        'a declared pack id that is not sixty-four hex digits': lambda: conftest.stream(
            conftest.intent(0, {REF: (None, SHA_A)}, descriptors=[conftest.descriptor(PACK_1, pack_id='PACK')]),
            [PACK_1],
        ),
        'a pack declared twice': lambda: conftest.stream(
            conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1, PACK_1]), [PACK_1, PACK_1]
        ),
        'a declared pack of no bytes': lambda: conftest.stream(
            conftest.intent(0, {REF: (None, SHA_A)}, descriptors=[conftest.descriptor(b'')])
        ),
    }


def _bad_streams() -> dict[str, Callable[[], list[sheaf_pb2.PublishRequest]]]:
    """One publish per cause the contract names that only the stream's bytes or shape reveal."""
    return {
        'a pack whose bytes do not match its hash': lambda: conftest.stream(
            conftest.intent(
                0, {REF: (None, SHA_A)}, descriptors=[conftest.descriptor(PACK_1, pack_id=sheaf.pack_id(PACK_2))]
            ),
            [PACK_1],
        ),
        'a pack short of its declared size': lambda: conftest.stream(
            conftest.intent(0, {REF: (None, SHA_A)}, descriptors=[conftest.descriptor(PACK_1, size=len(PACK_1) + 1)]),
            [PACK_1],
        ),
        'a pack over its declared size': lambda: conftest.stream(
            conftest.intent(0, {REF: (None, SHA_A)}, descriptors=[conftest.descriptor(PACK_1, size=len(PACK_1) - 1)]),
            [PACK_1],
        ),
        'a stream with no messages': list,
        'a chunk before the intent': lambda: [
            *conftest.chunks([PACK_1]),
            sheaf_pb2.PublishRequest(intent=conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1])),
        ],
        'a second intent': lambda: [
            sheaf_pb2.PublishRequest(intent=conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1])),
            sheaf_pb2.PublishRequest(intent=conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1])),
            *conftest.chunks([PACK_1]),
        ],
        'an empty message first': lambda: [sheaf_pb2.PublishRequest()],
        'a pack index beyond the declared list': lambda: [
            sheaf_pb2.PublishRequest(intent=conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1])),
            sheaf_pb2.PublishRequest(chunk=sheaf_pb2.PublishChunk(pack=1, content=PACK_1)),
        ],
        'packs out of order': lambda: [
            sheaf_pb2.PublishRequest(intent=conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1, PACK_2])),
            sheaf_pb2.PublishRequest(chunk=sheaf_pb2.PublishChunk(pack=1, content=PACK_2)),
            sheaf_pb2.PublishRequest(chunk=sheaf_pb2.PublishChunk(pack=0, content=PACK_1)),
        ],
        'a chunk of an earlier pack after a later one began': lambda: [
            sheaf_pb2.PublishRequest(intent=conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1, PACK_2])),
            sheaf_pb2.PublishRequest(chunk=sheaf_pb2.PublishChunk(pack=0, content=PACK_1[:10])),
            sheaf_pb2.PublishRequest(chunk=sheaf_pb2.PublishChunk(pack=1, content=PACK_2[:10])),
            sheaf_pb2.PublishRequest(chunk=sheaf_pb2.PublishChunk(pack=0, content=PACK_1[10:])),
        ],
        'a chunk after the last declared pack': lambda: [
            *conftest.stream(conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1]), [PACK_1]),
            sheaf_pb2.PublishRequest(chunk=sheaf_pb2.PublishChunk(pack=0, content=b'more')),
        ],
        'a chunk with no bytes': lambda: [
            sheaf_pb2.PublishRequest(intent=conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1])),
            sheaf_pb2.PublishRequest(chunk=sheaf_pb2.PublishChunk(pack=0, content=b'')),
            *conftest.chunks([PACK_1]),
        ],
    }


_BAD_INTENTS = _bad_intents()
_BAD_STREAMS = _bad_streams()


@pytest.mark.parametrize('build', _BAD_INTENTS.values(), ids=_BAD_INTENTS.keys())
def test_a_malformed_intent_is_invalid_before_any_pack_is_stored(
    backend: sheaf.LocalBackend, build: Callable[[], list[sheaf_pb2.PublishRequest]]
) -> None:
    """Whatever follows the intent, a pack stored for a refused one would be litter for a knowable mistake."""
    outcome = conftest.attempt(backend, [*build(), *conftest.chunks([PACK_2])])
    assert outcome.code is grpc.StatusCode.INVALID_ARGUMENT, outcome.details
    assert outcome.generation is None, 'nothing may be published'
    assert outcome.packs == set()


@pytest.mark.parametrize('build', _BAD_STREAMS.values(), ids=_BAD_STREAMS.keys())
def test_a_malformed_stream_is_invalid_and_moves_no_ref(
    backend: sheaf.LocalBackend, build: Callable[[], list[sheaf_pb2.PublishRequest]]
) -> None:
    outcome = conftest.attempt(backend, build())
    assert outcome.code is grpc.StatusCode.INVALID_ARGUMENT, outcome.details
    assert outcome.generation is None, 'nothing may be published'


def test_a_publish_that_forgets_the_reflog_ref_is_invalid(backend: sheaf.LocalBackend) -> None:
    intent = sheaf_pb2.PublishIntent(base_generation=0)
    intent.ref_updates[REF].new = SHA_A
    outcome = conftest.attempt(backend, conftest.stream(intent))
    assert outcome.code is grpc.StatusCode.INVALID_ARGUMENT
    assert 'reflog' in outcome.details
    assert outcome.generation is None


def test_an_old_the_document_does_not_hold_at_the_base_generation_is_invalid(backend: sheaf.LocalBackend) -> None:
    """The intent disagrees with the document it claims to have read: a caller bug, not a race."""
    seeded = conftest.seed(backend, {REF: (None, SHA_A)})
    assert seeded.generation is not None
    intent = conftest.intent(seeded.generation, {REF: (SHA_B, SHA_C)}, reflog_previous=seeded.tip(refdoc.REFLOG_REF))

    outcome = conftest.attempt(backend, conftest.stream(intent))

    assert outcome.code is grpc.StatusCode.INVALID_ARGUMENT
    assert outcome.generation == seeded.generation


@pytest.mark.parametrize(
    'cause',
    ['a pack whose bytes do not match its hash', 'a pack short of its declared size', 'a pack over its declared size'],
)
def test_a_pack_that_is_not_what_it_declared_is_not_stored(backend: sheaf.LocalBackend, cause: str) -> None:
    outcome = conftest.attempt(backend, _BAD_STREAMS[cause]())
    assert outcome.code is grpc.StatusCode.INVALID_ARGUMENT
    assert outcome.packs == set()
    assert outcome.generation is None


def test_a_stream_that_ends_short_moves_no_ref_and_stores_only_the_completed_pack(backend: sheaf.LocalBackend) -> None:
    """A client that half-closed after a short read: the one damage this store cannot undo, refused."""
    intent = conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1, PACK_2])
    messages = [
        *conftest.stream(intent, [PACK_1]),
        *conftest.chunks([PACK_2])[:1],
    ]  # all of pack 0, a fragment of pack 1
    outcome = conftest.attempt(backend, messages)
    assert outcome.code is grpc.StatusCode.INVALID_ARGUMENT
    assert outcome.generation is None
    assert outcome.packs == {conftest.store_for(backend).pack_key(sheaf.pack_id(PACK_1))}, (
        'the complete pack was stored as it completed; the fragment never was'
    )


# --- ceilings -----------------------------------------------------------------------------------------


def test_declared_bytes_over_the_publish_ceiling_are_refused_before_any_byte(backend: sheaf.LocalBackend) -> None:
    limits = dataclasses.replace(LIMITS, max_publish_bytes=len(PACK_1) + len(PACK_2) - 1)
    intent = conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1, PACK_2])
    outcome = conftest.attempt(backend, conftest.stream(intent, [PACK_1, PACK_2]), limits)
    assert outcome.code is grpc.StatusCode.RESOURCE_EXHAUSTED
    assert outcome.packs == set()
    assert outcome.generation is None


def test_a_ref_set_over_the_ref_ceiling_is_refused(backend: sheaf.LocalBackend) -> None:
    seeded = conftest.seed(backend, {REF: (None, SHA_A), SIDE: (None, SHA_B)})
    assert seeded.generation is not None
    limits = dataclasses.replace(LIMITS, max_refs=len(seeded.refs))  # full: one more ref is one too many
    intent = conftest.intent(
        seeded.generation,
        {'refs/heads/third': (None, SHA_C)},
        packs=[PACK_1],
        reflog_previous=seeded.tip(refdoc.REFLOG_REF),
    )
    outcome = conftest.attempt(backend, conftest.stream(intent, [PACK_1]), limits)
    assert outcome.code is grpc.StatusCode.RESOURCE_EXHAUSTED
    assert outcome.packs == set()
    assert outcome.generation == seeded.generation


def test_a_document_over_the_size_ceiling_is_refused(tmp_path: pathlib.Path) -> None:
    """Measured on the document the publish would leave, declared packs included."""
    intent = conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1])
    unbounded = sheaf.LocalBackend(tmp_path / 'unbounded')
    assert conftest.attempt(unbounded, conftest.stream(intent, [PACK_1])).code is None
    size = len(conftest.store_for(unbounded).read().doc.to_bytes())

    bounded = sheaf.LocalBackend(tmp_path / 'bounded')
    outcome = conftest.attempt(
        bounded, conftest.stream(intent, [PACK_1]), dataclasses.replace(LIMITS, max_document_bytes=size - 1)
    )

    assert outcome.code is grpc.StatusCode.RESOURCE_EXHAUSTED
    assert outcome.packs == set()
    assert outcome.generation is None
    at_the_ceiling = dataclasses.replace(LIMITS, max_document_bytes=size)
    assert conftest.attempt(bounded, conftest.stream(intent, [PACK_1]), at_the_ceiling).code is None, (
        'the ceiling is inclusive'
    )


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match='max_refs'):
        servicer_mod.Limits(max_publish_bytes=1, max_refs=0, max_document_bytes=1)


# --- a moved document ---------------------------------------------------------------------------------


def test_a_publish_that_already_landed_succeeds_again_without_storing_anything(backend: sheaf.LocalBackend) -> None:
    """The receipt: the response was lost, the caller replays the same intent, and it completes."""
    counting = conftest.CountingPuts(backend)
    intent = conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1])

    async def scenario(
        stub: sheaf_pb2_grpc.SheafAsyncStub,
    ) -> tuple[sheaf_pb2.PublishResponse, sheaf_pb2.PublishResponse]:
        first = await conftest.publish(stub, conftest.stream(intent, [PACK_1]))
        second = await conftest.publish(stub, conftest.stream(intent, [PACK_1]))
        return first, second

    first, second = conftest.run(scenario, counting)
    assert first.generation == second.generation
    assert counting.puts == 1, 'the replay stored nothing'
    assert conftest.store_for(backend).read().generation == first.generation


def test_a_moved_document_with_the_caller_refs_unchanged_is_aborted(backend: sheaf.LocalBackend) -> None:
    """An unrelated publish landed first: rebuild against the new document and publish again."""
    conftest.seed(backend, {SIDE: (None, SHA_B)})
    intent = conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1])
    outcome = conftest.attempt(backend, conftest.stream(intent, [PACK_1]))
    assert outcome.code is grpc.StatusCode.ABORTED
    assert REF in outcome.details
    assert outcome.packs == set(), 'classified before any byte was read'
    assert conftest.store_for(backend).read().tip(REF) is None


def test_a_moved_document_where_the_caller_ref_moved_is_a_failed_precondition(backend: sheaf.LocalBackend) -> None:
    """The non-fast-forward, for the caller to merge."""
    seeded = conftest.seed(backend, {REF: (None, SHA_A)})
    assert seeded.generation is not None
    conftest.seed(backend, {REF: (SHA_A, SHA_C)})
    intent = conftest.intent(seeded.generation, {REF: (SHA_A, SHA_B)}, reflog_previous=seeded.tip(refdoc.REFLOG_REF))
    outcome = conftest.attempt(backend, conftest.stream(intent))
    assert outcome.code is grpc.StatusCode.FAILED_PRECONDITION
    assert REF in outcome.details
    assert conftest.store_for(backend).read().tip(REF) == SHA_C


def test_a_moved_document_with_one_ref_landed_and_another_behind_is_a_failed_precondition(
    backend: sheaf.LocalBackend,
) -> None:
    """Landed needs every moved ref at its new; short of that, one ref not at its old decides."""
    conftest.seed(backend, {REF: (None, SHA_A)})
    intent = conftest.intent(0, {REF: (None, SHA_A), SIDE: (None, SHA_B)})
    outcome = conftest.attempt(backend, conftest.stream(intent))
    assert outcome.code is grpc.StatusCode.FAILED_PRECONDITION
    assert REF in outcome.details
    assert SIDE not in outcome.details, 'only the ref that moved is named'


def test_a_base_generation_for_a_repository_that_does_not_exist_is_aborted(backend: sheaf.LocalBackend) -> None:
    """Nothing is deleted, so this is a caller naming a generation it never read; its refs are all absent."""
    outcome = conftest.attempt(backend, conftest.stream(conftest.intent(7, {REF: (None, SHA_A)})))
    assert outcome.code is grpc.StatusCode.ABORTED
    assert outcome.generation is None


def test_a_refusal_reaches_a_client_still_sending_packs(backend: sheaf.LocalBackend) -> None:
    """The status is sent after the client half-closes, not into its in-flight writes."""
    big = bytes(3 << 20)
    intent = conftest.intent(0, {'refs/heads/bad name': (None, SHA_A)}, packs=[big])
    messages = conftest.stream(intent, [big])
    outcomes = [conftest.attempt(backend, messages) for _ in range(5)]
    assert [outcome.code for outcome in outcomes] == [grpc.StatusCode.INVALID_ARGUMENT] * 5, outcomes
    assert conftest.stored_packs(backend) == set()


def test_a_session_refusal_reaches_a_client_still_sending_packs(backend: sheaf.LocalBackend) -> None:
    big = bytes(3 << 20)
    messages = conftest.stream(conftest.intent(0, {REF: (None, SHA_A)}, packs=[big]), [big])
    codes = []
    for _ in range(5):
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            conftest.run(
                lambda stub: conftest.publish(stub, messages, metadata=fixture_session.session_metadata('bad')), backend
            )
        codes.append(caught.value.code())
    assert codes == [grpc.StatusCode.PERMISSION_DENIED] * 5
    assert conftest.stored_packs(backend) == set()


def test_the_drain_charges_a_client_for_every_byte_it_keeps_sending(backend: sheaf.LocalBackend) -> None:
    """A client that never half-closes is cut off once its messages, chunks or not, sum to the budget."""
    bulk = conftest.intent(0, {f'refs/heads/b{i:05d}': (None, SHA_A) for i in range(20_000)})
    assert bulk.ByteSize() > 1 << 20
    # The client runs ahead of the server by its send window, so the bound is loose; the cap on what
    # the client sends is what makes a drain that undercharges fail rather than hang.
    bound = 2 * (2 * LIMITS.max_publish_bytes // bulk.ByteSize() + 1)
    sent = 0

    async def persistent() -> AsyncIterator[sheaf_pb2.PublishRequest]:
        nonlocal sent
        yield sheaf_pb2.PublishRequest(intent=conftest.intent(0, {'refs/heads/bad name': (None, SHA_A)}))
        while sent < 2 * bound:
            sent += 1
            yield sheaf_pb2.PublishRequest(intent=bulk)

    with pytest.raises(grpc.aio.AioRpcError):
        conftest.run(lambda stub: stub.Publish(persistent(), metadata=fixture_session.GOOD_METADATA), backend)
    assert sent <= bound, sent
    assert conftest.stored_packs(backend) == set()


def test_a_race_lost_at_the_swap_is_classified_from_a_fresh_read(backend: sheaf.LocalBackend) -> None:
    """The window between the servicer's read and its compare-and-swap, closed by the swap itself."""
    intent = conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1])
    racing = conftest.RacingCas(backend, lambda: conftest.seed(backend, {SIDE: (None, SHA_B)}))
    outcome = conftest.attempt(racing, conftest.stream(intent, [PACK_1]))
    assert outcome.code is grpc.StatusCode.ABORTED
    assert conftest.store_for(backend).read().tip(SIDE) == SHA_B
    assert outcome.packs == {conftest.store_for(backend).pack_key(sheaf.pack_id(PACK_1))}, (
        'the pack landed; the document did not'
    )


def test_a_race_lost_to_a_move_of_the_same_ref_is_a_failed_precondition(backend: sheaf.LocalBackend) -> None:
    intent = conftest.intent(0, {REF: (None, SHA_A)})
    racing = conftest.RacingCas(backend, lambda: conftest.seed(backend, {REF: (None, SHA_C)}))
    outcome = conftest.attempt(racing, conftest.stream(intent))
    assert outcome.code is grpc.StatusCode.FAILED_PRECONDITION
    assert conftest.store_for(backend).read().tip(REF) == SHA_C


def test_a_race_lost_to_the_same_publish_is_a_success(backend: sheaf.LocalBackend) -> None:
    """Two deliveries of one publish: the second finds every ref at its `new` and returns the receipt."""
    intent = conftest.intent(0, {REF: (None, SHA_A)})
    racing = conftest.RacingCas(backend, lambda: conftest.seed(backend, {REF: (None, SHA_A)}))
    response = conftest.run(lambda stub: conftest.publish(stub, conftest.stream(intent)), racing)
    assert response.generation == conftest.store_for(backend).read().generation


# --- damage ---------------------------------------------------------------------------------------------


def test_a_document_this_code_did_not_write_is_data_loss(backend: sheaf.LocalBackend) -> None:
    published = conftest.seed(backend, {REF: (None, SHA_A)})
    backend.cas_mutable(conftest.store_for(backend).ref_key, b'not a ref document', published.generation)

    with pytest.raises(grpc.aio.AioRpcError, check=conftest.refused(grpc.StatusCode.DATA_LOSS)):
        conftest.run(lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=fixture_session.GOOD_METADATA), backend)
    with pytest.raises(grpc.aio.AioRpcError, check=conftest.refused(grpc.StatusCode.DATA_LOSS)):
        conftest.run(
            lambda stub: conftest.publish(stub, conftest.stream(conftest.intent(0, {SIDE: (None, SHA_B)}))), backend
        )
