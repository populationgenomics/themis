"""`RemoteStore` against the sheaf servicer: each rpc, each status the contract names, and the snapshot it computes."""

from __future__ import annotations

import pathlib
import stat
from collections.abc import Sequence

import pytest

from themis import sheaf
from themis.clients.auth.tests import fixture_session
from themis.clients.sheaf import store as remote_mod
from themis.clients.sheaf.tests import conftest
from themis.services.sheaf import servicer as servicer_mod
from themis.services.sheaf.tests import conftest as service_conftest
from themis.sheaf import stores
from themis.sheaf.tests import conftest as sheaf_conftest

REF = conftest.REF
SIDE = 'refs/heads/side'
SHA_A, SHA_B, SHA_C = 'a' * 40, 'b' * 40, 'c' * 40
PACK_1, PACK_2 = b'PACK-1 ' * 100, b'PACK-2 ' * 100


def _intent(
    base: sheaf.Snapshot,
    moves: dict[str, tuple[str | None, str | None]],
    *,
    packs: Sequence[bytes] = (),
    stored_packs: Sequence[str] = (),
    head: sheaf.Target | None = None,
) -> sheaf.Intent:
    """An intent moving `moves`, with the reflog update every publish that moves a ref carries."""
    updates = {ref: sheaf.RefUpdate(old, new) for ref, (old, new) in moves.items()}
    intent = sheaf.Intent(ref_updates=updates, packs=packs, stored_packs=stored_packs, head=head)
    return sheaf_conftest.logged(base, intent)


# --- reads ------------------------------------------------------------------------------------------------


def test_a_repository_that_does_not_exist_reads_as_an_empty_snapshot(remote: remote_mod.RemoteStore) -> None:
    snapshot = remote.read()
    assert snapshot.generation is None
    assert snapshot.refs == {}
    assert snapshot.packs == ()
    assert snapshot.doc.head is None


def test_a_read_sees_what_a_direct_writer_published(
    remote: remote_mod.RemoteStore, backend: sheaf.LocalBackend
) -> None:
    published = service_conftest.seed(backend, {REF: (None, SHA_A)}, packs=[PACK_1])
    snapshot = remote.read()
    assert snapshot == published
    assert snapshot.doc.head == sheaf.SymbolicTarget(REF)


def test_a_document_this_code_did_not_write_is_corrupt_not_a_fault(
    remote: remote_mod.RemoteStore, backend: sheaf.LocalBackend
) -> None:
    published = service_conftest.seed(backend, {REF: (None, SHA_A)})
    backend.cas_mutable(conftest.direct(backend).ref_key, b'not a ref document', published.generation)

    with pytest.raises(sheaf.CorruptRepository):
        remote.read()
    with pytest.raises(sheaf.CorruptRepository):
        remote.publish(published, _intent(published, {SIDE: (None, SHA_B)}))


# --- packs ------------------------------------------------------------------------------------------------


def test_fetch_pack_returns_the_bytes_the_document_names(
    remote: remote_mod.RemoteStore, backend: sheaf.LocalBackend
) -> None:
    # Over the chunk size in both directions, so the stream is reassembled from several messages.
    big = bytes(range(256)) * (3 * 4096)
    assert len(big) > remote_mod.CHUNK_SIZE
    service_conftest.seed(backend, {REF: (None, SHA_A)}, packs=[PACK_1, big])

    assert remote.fetch_pack(sheaf.pack_id(PACK_1)) == PACK_1
    assert remote.fetch_pack(sheaf.pack_id(big)) == big


def test_fetch_pack_of_an_unknown_id_is_not_found(remote: remote_mod.RemoteStore) -> None:
    with pytest.raises(sheaf.NotFound):
        remote.fetch_pack('f' * 64)


def test_fetch_pack_of_a_malformed_id_is_refused_by_name(remote: remote_mod.RemoteStore) -> None:
    with pytest.raises(sheaf.InvalidPackId):
        remote.fetch_pack('not-a-pack-id')


# --- publish ----------------------------------------------------------------------------------------------


def test_a_first_publish_lands_and_the_computed_snapshot_is_the_one_the_service_holds(
    remote: remote_mod.RemoteStore, backend: sheaf.LocalBackend
) -> None:
    base = remote.read()
    big = bytes(range(256)) * (3 * 4096)
    intent = _intent(base, {REF: (None, SHA_A), SIDE: (None, SHA_B)}, packs=[PACK_1, big])

    published = remote.publish(base, intent)

    again = remote.read()
    assert published == again, 'the client computes exactly the document the service wrote'
    assert conftest.direct(backend).read() == again
    assert published.refs[REF] == SHA_A
    assert published.refs[SIDE] == SHA_B
    assert set(published.packs) == {sheaf.pack_id(PACK_1), sheaf.pack_id(big)}
    assert published.doc.head == sheaf.SymbolicTarget(REF)
    assert remote.fetch_pack(sheaf.pack_id(big)) == big


def test_a_second_publish_carries_the_document_forward(remote: remote_mod.RemoteStore) -> None:
    first = remote.publish(remote.read(), _intent(remote.read(), {REF: (None, SHA_A)}, packs=[PACK_1]))
    second = remote.publish(first, _intent(first, {REF: (SHA_A, SHA_B)}, packs=[PACK_2]))

    assert second.generation != first.generation
    assert second == remote.read()
    assert set(second.packs) == {sheaf.pack_id(PACK_1), sheaf.pack_id(PACK_2)}


def test_a_set_head_is_recorded(remote: remote_mod.RemoteStore) -> None:
    base = remote.read()
    published = remote.publish(base, _intent(base, {SIDE: (None, SHA_B)}, head=sheaf.SymbolicTarget(SIDE)))
    assert published.doc.head == sheaf.SymbolicTarget(SIDE)
    assert remote.read().doc.head == sheaf.SymbolicTarget(SIDE)


def test_a_publish_with_no_packs_is_an_intent_alone(remote: remote_mod.RemoteStore) -> None:
    base = remote.read()
    published = remote.publish(base, _intent(base, {REF: (None, SHA_A)}))
    assert published == remote.read()
    assert published.packs == ()


def test_an_unrelated_publish_landing_first_is_a_lost_race(
    remote: remote_mod.RemoteStore, backend: sheaf.LocalBackend
) -> None:
    base = remote.read()
    service_conftest.seed(backend, {SIDE: (None, SHA_B)})

    with pytest.raises(sheaf.RaceLost):
        remote.publish(base, _intent(base, {REF: (None, SHA_A)}, packs=[PACK_1]))
    assert conftest.direct(backend).read().tip(REF) is None


def test_a_ref_moved_under_the_publish_is_a_conflict_naming_what_it_holds(
    remote: remote_mod.RemoteStore, backend: sheaf.LocalBackend
) -> None:
    service_conftest.seed(backend, {REF: (None, SHA_A)})
    base = remote.read()
    service_conftest.seed(backend, {REF: (SHA_A, SHA_C)})

    with pytest.raises(sheaf.RefConflict) as caught:
        remote.publish(base, _intent(base, {REF: (SHA_A, SHA_B)}))

    assert caught.value.ref == REF
    assert caught.value.expected == SHA_A
    assert caught.value.actual == SHA_C
    assert conftest.direct(backend).read().tip(REF) == SHA_C


def test_what_the_service_refuses_on_the_intent_is_a_publish_refusal(remote: remote_mod.RemoteStore) -> None:
    """A pack of no bytes passes the store's own checks and is the service's to refuse."""
    base = remote.read()
    with pytest.raises(sheaf.PublishRefused, match='no bytes'):
        remote.publish(base, _intent(base, {REF: (None, SHA_A)}, packs=[b'']))
    assert remote.read().generation is None


def test_a_publish_over_the_ceiling_is_a_publish_refusal(backend: sheaf.LocalBackend, token_file: pathlib.Path) -> None:
    limits = servicer_mod.Limits(max_publish_bytes=len(PACK_1) - 1, max_refs=64, max_document_bytes=1 << 16)
    with (
        conftest.serving(backend, limits) as target,
        remote_mod.RemoteStore(target, token_file, repo=conftest.ANALYSIS_ID) as remote,
    ):
        base = remote.read()
        with pytest.raises(sheaf.PublishRefused, match='ceiling'):
            remote.publish(base, _intent(base, {REF: (None, SHA_A)}, packs=[PACK_1]))
    assert conftest.direct(backend).read().generation is None


def test_the_store_level_refusals_are_raised_before_anything_is_sent(
    remote: remote_mod.RemoteStore, backend: sheaf.LocalBackend
) -> None:
    base = remote.read()
    with pytest.raises(sheaf.RefDeletionRefused):
        remote.publish(base, _intent(base, {REF: (SHA_A, None)}))
    with pytest.raises(sheaf.ReflogRequired):
        remote.publish(base, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    with pytest.raises(sheaf.InvalidRefName):
        remote.publish(base, _intent(base, {'refs/heads/bad name': (None, SHA_A)}))
    assert service_conftest.stored_packs(backend) == set()


def test_a_remote_publish_cannot_name_stored_packs(remote: remote_mod.RemoteStore) -> None:
    base = remote.read()
    with pytest.raises(ValueError, match='stored'):
        remote.publish(base, _intent(base, {REF: (None, SHA_A)}, stored_packs=[sheaf.pack_id(PACK_1)]))


# --- credentials ------------------------------------------------------------------------------------------


def test_a_token_the_service_does_not_resolve_is_a_service_fault_not_a_protocol_outcome(
    target: str, tmp_path: pathlib.Path
) -> None:
    token_file = conftest.write_token_file(tmp_path / 'token.json', 'nobody')
    empty = sheaf.Snapshot(doc=sheaf.RefDoc(), generation=None)
    with remote_mod.RemoteStore(target, token_file, repo=conftest.ANALYSIS_ID) as remote:
        with pytest.raises(sheaf.ServiceFault) as on_read:
            remote.read()
        with pytest.raises(sheaf.ServiceFault) as on_fetch:
            remote.fetch_pack('f' * 64)
        with pytest.raises(sheaf.ServiceFault) as on_publish:
            remote.publish(empty, _intent(empty, {REF: (None, SHA_A)}))
    assert {on_read.value.code, on_fetch.value.code, on_publish.value.code} == {'PERMISSION_DENIED'}
    assert 'nobody' not in str(on_read.value), 'the token is not in the message'


def test_a_service_that_cannot_be_reached_is_a_service_fault(token_file: pathlib.Path) -> None:
    with conftest.serving(sheaf.LocalBackend(token_file.parent / 'store')) as target:
        pass
    # The server is gone; the channel's retries on UNAVAILABLE run out and the fault is typed.
    with (
        remote_mod.RemoteStore(target, token_file, repo=conftest.ANALYSIS_ID) as remote,
        pytest.raises(sheaf.ServiceFault) as caught,
    ):
        remote.read()
    assert caught.value.code == 'UNAVAILABLE'


def test_a_read_rides_out_a_passing_unavailable(backend: sheaf.LocalBackend, token_file: pathlib.Path) -> None:
    """The channel's retry policy is what makes an instance recycling invisible to a clone."""
    service_conftest.seed(backend, {REF: (None, SHA_A)})
    with (
        conftest.serving(backend, servicer_class=conftest.Flaky) as target,
        remote_mod.RemoteStore(target, token_file, repo=conftest.ANALYSIS_ID) as remote,
    ):
        assert remote.read().tip(REF) == SHA_A


def test_a_relative_token_path_is_made_absolute_so_the_hook_finds_it(
    target: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conftest.write_token_file(tmp_path / 'token.json', fixture_session.GOOD_TOKEN)
    monkeypatch.chdir(tmp_path)
    with remote_mod.RemoteStore(target, pathlib.Path('token.json'), repo=conftest.ANALYSIS_ID) as remote:
        monkeypatch.chdir(tmp_path.parent)
        rebuilt = stores.from_descriptor(remote.descriptor())
        assert isinstance(rebuilt, remote_mod.RemoteStore)
        with rebuilt:
            assert rebuilt.read() == remote.read()


def test_the_token_file_is_read_on_every_call(
    remote: remote_mod.RemoteStore, backend: sheaf.LocalBackend, token_file: pathlib.Path
) -> None:
    """Which repository a call reaches is the session's, and the session is whatever the file holds now."""
    service_conftest.seed(backend, {REF: (None, SHA_A)})
    other = conftest.direct(backend, service_conftest.OTHER_ANALYSIS_ID)
    other.publish(other.read(), _intent(other.read(), {REF: (None, SHA_B)}))
    assert remote.read().tip(REF) == SHA_A

    conftest.write_token_file(token_file, service_conftest.OTHER_TOKEN)

    assert remote.read().tip(REF) == SHA_B


def test_the_descriptor_rebuilds_the_store_and_carries_no_token(
    remote: remote_mod.RemoteStore, backend: sheaf.LocalBackend
) -> None:
    service_conftest.seed(backend, {REF: (None, SHA_A)})
    descriptor = remote.descriptor()

    assert fixture_session.GOOD_TOKEN not in ' '.join(descriptor.values())
    assert fixture_session.GOOD_TOKEN not in repr(remote_mod.read_credentials(remote.token_file))
    rebuilt = stores.from_descriptor(descriptor)
    assert isinstance(rebuilt, remote_mod.RemoteStore)
    with rebuilt:
        assert rebuilt.repo == remote.repo
        assert rebuilt.read() == remote.read()


@pytest.mark.parametrize(
    ('payload', 'cause'),
    [
        ('[]', 'JSON object'),
        ('{"session_token": ""}', 'non-empty'),
        ('{"bearer": "x"}', 'session_token'),
        ('{"session_token": "t", "beaerer": "x"}', 'unexpected keys'),
        ('{"session_token": "t", "bearer": 1}', 'bearer'),
        ('not json', 'not JSON'),
    ],
)
def test_a_malformed_token_file_is_refused(target: str, tmp_path: pathlib.Path, payload: str, cause: str) -> None:
    token_file = tmp_path / 'token.json'
    token_file.write_text(payload, 'utf-8')
    token_file.chmod(0o600)
    with pytest.raises(sheaf.CredentialsUnusable, match=cause):
        remote_mod.RemoteStore(target, token_file, repo=conftest.ANALYSIS_ID)


def test_a_token_file_others_can_read_is_refused(target: str, tmp_path: pathlib.Path) -> None:
    token_file = conftest.write_token_file(tmp_path / 'token.json', fixture_session.GOOD_TOKEN)
    token_file.chmod(0o640)
    with pytest.raises(sheaf.CredentialsUnusable, match='chmod 600'):
        remote_mod.RemoteStore(target, token_file, repo=conftest.ANALYSIS_ID)


def test_the_token_file_is_written_for_its_owner_alone_and_reads_back(tmp_path: pathlib.Path) -> None:
    path = tmp_path / 'token.json'
    path.write_text('{"session_token": "stale", "bearer": "old"}', 'utf-8')
    path.chmod(0o644)
    credentials = remote_mod.Credentials(session_token='fresh', bearer=None)

    remote_mod.write_credentials(path, credentials)

    assert remote_mod.read_credentials(path) == credentials
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [entry.name for entry in tmp_path.iterdir()] == ['token.json'], 'no staging file is left behind'


def test_a_bearer_is_never_sent_in_the_clear(target: str, tmp_path: pathlib.Path) -> None:
    token_file = conftest.write_token_file(tmp_path / 'token.json', fixture_session.GOOD_TOKEN, bearer='id-token')
    with pytest.raises(ValueError, match='TLS'):
        remote_mod.RemoteStore(target, token_file, repo=conftest.ANALYSIS_ID)


@pytest.mark.parametrize('target', ['http://127.0.0.1:1', 'grpc://127.0.0.1:1', 'sheaf.example:443', '10.0.0.7:50051'])
def test_the_session_token_goes_over_tls_or_to_loopback_and_nowhere_else(token_file: pathlib.Path, target: str) -> None:
    with pytest.raises(ValueError, match='https'):
        remote_mod.RemoteStore(target, token_file, repo=conftest.ANALYSIS_ID)


def test_a_bearer_that_appears_after_the_store_opened_is_refused(
    remote: remote_mod.RemoteStore, token_file: pathlib.Path
) -> None:
    conftest.write_token_file(token_file, fixture_session.GOOD_TOKEN, bearer='id-token')
    with pytest.raises(sheaf.CredentialsUnusable, match='bearer'):
        remote.read()


def test_a_token_file_that_vanishes_is_a_credentials_fault(
    remote: remote_mod.RemoteStore, token_file: pathlib.Path
) -> None:
    token_file.unlink()
    with pytest.raises(sheaf.CredentialsUnusable, match='No such file'):
        remote.read()
