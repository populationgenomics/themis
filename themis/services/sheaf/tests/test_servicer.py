"""Behaviour tests for the Sheaf servicer over an in-process server, driven by the synchronous client.

The store is `LocalBackend` over a temporary directory; the session resolver is a fixture map with
two tokens, so two Analyses' repositories can be told apart. A second writer, where a test needs
one, publishes through `themis.sheaf.Store` directly over the same backend — the seam every in-process writer
uses — and a race between the servicer's read and its compare-and-swap is staged by a
backend wrapper that lands a competing publish inside `cas_mutable`.
"""

from __future__ import annotations

import dataclasses
import datetime
import pathlib
from collections.abc import Callable

import grpc
import pytest
from google.protobuf import empty_pb2

from themis import sheaf
from themis.rpc import sandbox_options_pb2, sheaf_pb2, sheaf_pb2_grpc
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
OTHER = conftest.OTHER
LIMITS = conftest.LIMITS


def test_a_call_with_no_verifiable_caller_is_unauthenticated(backend: sheaf.LocalBackend) -> None:
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.UNAUTHENTICATED)):
        conftest.run(lambda stub: stub.ReadRefDoc(empty_pb2.Empty()), backend)


def test_a_worker_whose_session_does_not_resolve_is_permission_denied(backend: sheaf.LocalBackend) -> None:
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.PERMISSION_DENIED)):
        conftest.run(lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=conftest.BAD_SESSION), backend)


def test_the_agent_s_claim_does_not_reach_the_worker_s_rpcs(backend: sheaf.LocalBackend) -> None:
    # One account, two principals: the sheaf contract names the worker, and the guest's forwarded call
    # claims the agent, so the repository is the worker's to publish and never the guest's directly.
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.PERMISSION_DENIED)):
        conftest.run(lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=conftest.AGENT), backend)


def test_the_developer_reaches_the_repository_of_the_session_it_names(backend: sheaf.LocalBackend) -> None:
    conftest.seed(backend, {REF: (None, SHA_A)})
    response = conftest.run(
        lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=conftest.CLU_WITH_SESSION), backend
    )
    assert refdoc.RefDoc.from_bytes(response.document.SerializeToString()).refs[REF] == SHA_A


def test_the_developer_naming_a_session_that_does_not_resolve_is_permission_denied(backend: sheaf.LocalBackend) -> None:
    """A stale token file: admitted as the developer, but the repository it asks for cannot be named."""
    stale = (*conftest.CLU, *conftest.auth_fixture.claim(sandbox_options_pb2.CALLING_AS_SELF, 'bad'))
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.PERMISSION_DENIED)):
        conftest.run(lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=stale), backend)


def test_the_developer_naming_no_session_is_refused_as_an_invalid_call(backend: sheaf.LocalBackend) -> None:
    """Admitted everywhere, but no request names a repository: without a session there is nothing to serve."""
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.INVALID_ARGUMENT)):
        conftest.run(lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=conftest.CLU), backend)


def test_the_web_tier_reaches_the_repository_of_the_session_it_names(backend: sheaf.LocalBackend) -> None:
    conftest.seed(backend, {REF: (None, SHA_A)})
    response = conftest.run(
        lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=conftest.WEB_WITH_SESSION), backend
    )
    assert refdoc.RefDoc.from_bytes(response.document.SerializeToString()).refs[REF] == SHA_A


def test_the_web_tier_naming_no_session_is_refused_as_an_invalid_call(backend: sheaf.LocalBackend) -> None:
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.INVALID_ARGUMENT)):
        conftest.run(lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=conftest.WEB), backend)


def test_the_web_tier_does_not_fetch_packs(backend: sheaf.LocalBackend) -> None:
    """The browser downloads packs by signed URL; streaming them through this service is the worker's path alone."""
    conftest.seed(backend, {REF: (None, SHA_A)}, packs=[PACK_1])

    def scenario(stub: sheaf_pb2_grpc.SheafStub) -> bytes:
        request = sheaf_pb2.FetchPackRequest(pack_id=sheaf.pack_id(PACK_1))
        return b''.join([chunk.content for chunk in stub.FetchPack(request, metadata=conftest.WEB_WITH_SESSION)])

    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.PERMISSION_DENIED)):
        conftest.run(scenario, backend)


def test_the_worker_does_not_sign_pack_urls(backend: sheaf.LocalBackend) -> None:
    request = sheaf_pb2.SignPackUrlsRequest(pack_ids=[sheaf.pack_id(PACK_1)])
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.PERMISSION_DENIED)):
        conftest.run(lambda stub: stub.SignPackUrls(request, metadata=conftest.WORKER), backend)


def test_a_fetch_by_a_worker_whose_session_does_not_resolve_is_permission_denied(backend: sheaf.LocalBackend) -> None:
    conftest.seed(backend, {REF: (None, SHA_A)}, packs=[PACK_1])

    def scenario(stub: sheaf_pb2_grpc.SheafStub) -> bytes:
        request = sheaf_pb2.FetchPackRequest(pack_id=sheaf.pack_id(PACK_1))
        metadata = conftest.BAD_SESSION
        return b''.join([chunk.content for chunk in stub.FetchPack(request, metadata=metadata)])

    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.PERMISSION_DENIED)):
        conftest.run(scenario, backend)


def test_a_repository_that_does_not_exist_reads_as_generation_zero_and_no_document(
    backend: sheaf.LocalBackend,
) -> None:
    snapshot = conftest.run(lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=conftest.WORKER), backend)
    assert snapshot.generation == 0
    assert not snapshot.HasField('document')


def test_each_analysis_reaches_only_its_own_repository(backend: sheaf.LocalBackend) -> None:
    def scenario(
        stub: sheaf_pb2_grpc.SheafStub,
    ) -> tuple[sheaf_pb2.RefDocSnapshot, sheaf_pb2.RefDocSnapshot]:
        conftest.publish(stub, conftest.stream(conftest.intent(0, {REF: (None, SHA_A)})))
        mine = stub.ReadRefDoc(empty_pb2.Empty(), metadata=conftest.WORKER)
        theirs = stub.ReadRefDoc(empty_pb2.Empty(), metadata=OTHER)
        return mine, theirs

    mine, theirs = conftest.run(scenario, backend)
    assert mine.document.refs[REF].oid == SHA_A
    assert theirs.generation == 0
    assert not theirs.HasField('document')
    assert conftest.store_for(backend, OTHER_ANALYSIS_ID).read().generation is None


def test_a_pack_another_analysis_stored_is_not_found(backend: sheaf.LocalBackend) -> None:
    conftest.seed(backend, {REF: (None, SHA_A)}, packs=[PACK_1])

    def scenario(stub: sheaf_pb2_grpc.SheafStub) -> bytes:
        request = sheaf_pb2.FetchPackRequest(pack_id=sheaf.pack_id(PACK_1))
        return b''.join([chunk.content for chunk in stub.FetchPack(request, metadata=OTHER)])

    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.NOT_FOUND)):
        conftest.run(scenario, backend)


def test_a_publish_lands_under_the_analysis_its_session_names(backend: sheaf.LocalBackend) -> None:
    messages = conftest.stream(conftest.intent(0, {REF: (None, SHA_B)}, packs=[PACK_2]), [PACK_2])
    conftest.run(lambda stub: conftest.publish(stub, messages, metadata=OTHER), backend)
    assert conftest.store_for(backend, OTHER_ANALYSIS_ID).read().tip(REF) == SHA_B
    assert conftest.store_for(backend).read().generation is None
    assert conftest.store_for(backend, OTHER_ANALYSIS_ID).fetch_pack(sheaf.pack_id(PACK_2)) == PACK_2


def test_the_web_tier_publishes_to_the_repository_of_the_session_it_names(backend: sheaf.LocalBackend) -> None:
    messages = conftest.stream(conftest.intent(0, {REF: (None, SHA_B)}, packs=[PACK_2]), [PACK_2])
    conftest.run(lambda stub: conftest.publish(stub, messages, metadata=conftest.WEB_WITH_SESSION), backend)
    assert conftest.store_for(backend).read().tip(REF) == SHA_B
    assert conftest.store_for(backend, OTHER_ANALYSIS_ID).read().generation is None


# --- the first publish, and reading it back --------------------------------------------------------


def test_a_first_publish_lands_and_reads_back(backend: sheaf.LocalBackend) -> None:
    def scenario(
        stub: sheaf_pb2_grpc.SheafStub,
    ) -> tuple[sheaf_pb2.PublishResponse, sheaf_pb2.RefDocSnapshot]:
        response = conftest.publish(
            stub, conftest.stream(conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1]), [PACK_1])
        )
        return response, stub.ReadRefDoc(empty_pb2.Empty(), metadata=conftest.WORKER)

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
    def scenario(stub: sheaf_pb2_grpc.SheafStub) -> None:
        first = conftest.intent(0, {REF: (None, SHA_A)}, head=refdoc_pb2.RefTarget(oid=SHA_A))
        response = conftest.publish(stub, conftest.stream(first))
        second = conftest.intent(
            response.generation,
            {REF: (SHA_A, SHA_B)},
            reflog_previous=conftest.reflog_entry(None, {REF: (None, SHA_A)}),
        )
        conftest.publish(stub, conftest.stream(second))

    conftest.run(scenario, backend)
    assert conftest.store_for(backend).read().doc.head == sheaf.DirectTarget(SHA_A)


def test_a_field_this_build_does_not_model_is_read_back_intact(backend: sheaf.LocalBackend) -> None:
    """The document is carried as a message so a later build's field survives the round trip."""
    published = conftest.seed(backend, {REF: (None, SHA_A)})
    from_the_future = b'\xf8\x06\x2a'  # field 111, varint 42
    store = conftest.store_for(backend)
    backend.cas_mutable(store.ref_key, published.doc.to_bytes() + from_the_future, published.generation)

    snapshot = conftest.run(lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=conftest.WORKER), backend)

    assert snapshot.document.SerializeToString().endswith(from_the_future)


# --- FetchPack --------------------------------------------------------------------------------------


def test_fetch_pack_streams_the_bytes_the_document_names(backend: sheaf.LocalBackend) -> None:
    big = bytes(range(256)) * ((servicer_mod._CHUNK_SIZE // 256) + 1)  # more than one chunk
    conftest.seed(backend, {REF: (None, SHA_A)}, packs=[big])
    assert conftest.run(lambda stub: conftest.fetch(stub, sheaf.pack_id(big)), backend) == big


def test_fetch_pack_of_an_unknown_id_is_not_found(backend: sheaf.LocalBackend) -> None:
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.NOT_FOUND)):
        conftest.run(lambda stub: conftest.fetch(stub, 'f' * 64), backend)


@pytest.mark.parametrize('pack_id', ['', 'F' * 64, 'f' * 63, '../refs.pb', sheaf.pack_id(PACK_1) + '\n'])
def test_fetch_pack_of_a_malformed_id_is_invalid_before_the_store_is_consulted(
    backend: sheaf.LocalBackend, pack_id: str
) -> None:
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.INVALID_ARGUMENT)):
        conftest.run(lambda stub: conftest.fetch(stub, pack_id), backend)


# --- SignPackUrls ---------------------------------------------------------------------------------------


def _sign(stub: sheaf_pb2_grpc.SheafStub, pack_ids: list[str]) -> sheaf_pb2.SignPackUrlsResponse:
    request = sheaf_pb2.SignPackUrlsRequest(pack_ids=pack_ids)
    return stub.SignPackUrls(request, metadata=conftest.WEB_WITH_SESSION)


def test_the_web_tier_is_signed_a_url_and_the_size_of_each_listed_pack_in_request_order(
    backend: sheaf.LocalBackend, signing: conftest.Signing
) -> None:
    small, large = b'small pack', b'large pack ' * 50
    conftest.seed(backend, {REF: (None, SHA_A)}, packs=[small])
    conftest.seed(backend, {REF: (SHA_A, SHA_B)}, packs=[large])
    asked = [sheaf.pack_id(large), sheaf.pack_id(small)]
    before = datetime.datetime.now(datetime.UTC)

    response = conftest.run(lambda stub: _sign(stub, asked), signing)

    assert [pack.pack_id for pack in response.packs] == asked
    assert [pack.size for pack in response.packs] == [len(large), len(small)]
    store = conftest.store_for(backend)
    assert [conftest.Signing.key_of(pack.url) for pack in response.packs] == [store.pack_key(ident) for ident in asked]
    for pack in response.packs:
        expires = pack.expire_time.ToDatetime(datetime.UTC)
        assert before < expires <= datetime.datetime.now(datetime.UTC) + LIMITS.pack_url_lifetime


def _bad_sign_requests() -> dict[str, list[str]]:
    listed = sheaf.pack_id(PACK_1)
    return {
        'empty': [],
        'over the per-call maximum': [f'{index:064x}' for index in range(servicer_mod._MAX_SIGNED_PACKS + 1)],
        'not a pack id': [listed, 'F' * 64],
        'a path, not an id': ['../refs.pb'],
        'named twice': [listed, listed],
    }


@pytest.mark.parametrize('pack_ids', list(_bad_sign_requests().values()), ids=list(_bad_sign_requests()))
def test_a_malformed_signing_request_is_invalid_before_the_store_is_read(
    backend: sheaf.LocalBackend, signing: conftest.Signing, pack_ids: list[str]
) -> None:
    # A document the store cannot parse: a request decided after the read would be DATA_LOSS instead.
    published = conftest.seed(backend, {REF: (None, SHA_A)}, packs=[PACK_1])
    backend.cas_mutable(conftest.store_for(backend).ref_key, b'not a ref document', published.generation)
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.INVALID_ARGUMENT)):
        conftest.run(lambda stub: _sign(stub, pack_ids), signing)


def test_the_per_call_maximum_is_signed_whole(backend: sheaf.LocalBackend, signing: conftest.Signing) -> None:
    packs = [f'PACK-{index} '.encode() for index in range(servicer_mod._MAX_SIGNED_PACKS)]
    conftest.seed(backend, {REF: (None, SHA_A)}, packs=packs)
    asked = [sheaf.pack_id(pack) for pack in packs]
    assert [pack.pack_id for pack in conftest.run(lambda stub: _sign(stub, asked), signing).packs] == asked


def test_a_stored_pack_the_document_does_not_list_is_not_found(
    backend: sheaf.LocalBackend, signing: conftest.Signing
) -> None:
    """Another Analysis's pack, or one a refused publish left behind: stored, but no reader here can name it."""
    conftest.seed(backend, {REF: (None, SHA_A)}, packs=[PACK_1])
    unlisted = conftest.store_for(backend).put_pack(PACK_2)
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.NOT_FOUND)):
        conftest.run(lambda stub: _sign(stub, [sheaf.pack_id(PACK_1), unlisted]), signing)


def test_a_full_call_of_unlisted_packs_arrives_as_not_found(signing: conftest.Signing) -> None:
    """The refusal's message rides in a size-capped trailer, so it cannot name all 256 ids and still arrive."""
    asked = [f'{index:064x}' for index in range(servicer_mod._MAX_SIGNED_PACKS)]
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.NOT_FOUND)) as refused:
        conftest.run(lambda stub: _sign(stub, asked), signing)
    assert f'{len(asked)} of the packs named' in (refused.value.details() or '')  # pyright: ignore[reportAttributeAccessIssue]


def test_a_full_call_of_listed_packs_the_store_lacks_arrives_as_data_loss(
    backend: sheaf.LocalBackend, signing: conftest.Signing
) -> None:
    absent = [f'{index:064x}' for index in range(servicer_mod._MAX_SIGNED_PACKS)]
    store = conftest.store_for(backend)
    updates = {REF: sheaf.RefUpdate(None, SHA_A), refdoc.REFLOG_REF: sheaf.RefUpdate(None, SHA_B)}
    store.publish(store.read(), sheaf.Intent(ref_updates=updates, stored_packs=tuple(absent)))
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.DATA_LOSS)) as refused:
        conftest.run(lambda stub: _sign(stub, absent), signing)
    assert f'lists {len(absent)} packs' in (refused.value.details() or '')  # pyright: ignore[reportAttributeAccessIssue]


def test_every_size_comes_from_one_listing(backend: sheaf.LocalBackend) -> None:
    packs = [f'PACK-{index} '.encode() for index in range(16)]
    conftest.seed(backend, {REF: (None, SHA_A)}, packs=packs)
    counting = conftest.CountingLists(backend)
    conftest.run(lambda stub: _sign(stub, [sheaf.pack_id(pack) for pack in packs]), counting)
    assert counting.lists == 1


def test_packs_are_signed_concurrently(backend: sheaf.LocalBackend) -> None:
    """Each signature is a round trip, so a full call signed one at a time would spend its URLs' lifetime waiting."""
    conftest.seed(backend, {REF: (None, SHA_A)}, packs=[PACK_1, PACK_2])
    meeting = conftest.MeetingSigners(backend, parties=2)
    response = conftest.run(lambda stub: _sign(stub, [sheaf.pack_id(PACK_1), sheaf.pack_id(PACK_2)]), meeting)
    assert len(response.packs) == 2


def test_a_repository_that_does_not_exist_lists_no_pack_to_sign(signing: conftest.Signing) -> None:
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.NOT_FOUND)):
        conftest.run(lambda stub: _sign(stub, [sheaf.pack_id(PACK_1)]), signing)


def test_a_listed_pack_the_store_does_not_hold_is_data_loss(
    backend: sheaf.LocalBackend, signing: conftest.Signing
) -> None:
    store = conftest.store_for(backend)
    absent = 'f' * 64
    updates = {REF: sheaf.RefUpdate(None, SHA_A), refdoc.REFLOG_REF: sheaf.RefUpdate(None, SHA_B)}
    store.publish(store.read(), sheaf.Intent(ref_updates=updates, stored_packs=(absent,)))
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.DATA_LOSS)):
        conftest.run(lambda stub: _sign(stub, [absent]), signing)


def test_a_store_that_cannot_sign_answers_unimplemented(backend: sheaf.LocalBackend) -> None:
    """The local-directory store has no URL to issue; it says so rather than handing out one nothing serves."""
    conftest.seed(backend, {REF: (None, SHA_A)}, packs=[PACK_1])
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.UNIMPLEMENTED)):
        conftest.run(lambda stub: _sign(stub, [sheaf.pack_id(PACK_1)]), backend)


def test_the_web_tier_is_signed_only_its_own_analysis_s_packs(
    backend: sheaf.LocalBackend, signing: conftest.Signing
) -> None:
    other = conftest.store_for(backend, OTHER_ANALYSIS_ID)
    other_base = other.read()
    updates = {REF: sheaf.RefUpdate(None, SHA_A), refdoc.REFLOG_REF: sheaf.RefUpdate(None, SHA_B)}
    other.publish(other_base, sheaf.Intent(ref_updates=updates, packs=[PACK_1]))
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.NOT_FOUND)):
        conftest.run(lambda stub: _sign(stub, [sheaf.pack_id(PACK_1)]), signing)


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
        dataclasses.replace(LIMITS, max_refs=0)


def test_a_url_lifetime_past_what_a_v4_signature_carries_is_refused() -> None:
    with pytest.raises(ValueError, match='pack_url_lifetime_seconds'):
        dataclasses.replace(LIMITS, pack_url_lifetime_seconds=servicer_mod._MAX_URL_LIFETIME_SECONDS + 1)


# --- a moved document ---------------------------------------------------------------------------------


def test_a_publish_that_already_landed_succeeds_again_without_storing_anything(backend: sheaf.LocalBackend) -> None:
    """The receipt: the response was lost, the caller replays the same intent, and it completes."""
    counting = conftest.CountingPuts(backend)
    intent = conftest.intent(0, {REF: (None, SHA_A)}, packs=[PACK_1])

    def scenario(
        stub: sheaf_pb2_grpc.SheafStub,
    ) -> tuple[sheaf_pb2.PublishResponse, sheaf_pb2.PublishResponse]:
        first = conftest.publish(stub, conftest.stream(intent, [PACK_1]))
        second = conftest.publish(stub, conftest.stream(intent, [PACK_1]))
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

    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.DATA_LOSS)):
        conftest.run(lambda stub: stub.ReadRefDoc(empty_pb2.Empty(), metadata=conftest.WORKER), backend)
    with pytest.raises(grpc.RpcError, check=conftest.refused(grpc.StatusCode.DATA_LOSS)):
        conftest.run(
            lambda stub: conftest.publish(stub, conftest.stream(conftest.intent(0, {SIDE: (None, SHA_B)}))), backend
        )
