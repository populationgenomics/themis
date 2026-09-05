"""Publish semantics, and the distinction between a lost race and a real conflict."""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from themis import sheaf
from themis.sheaf import refdoc
from themis.sheaf.models import refdoc_pb2
from themis.sheaf.tests import conftest
from themis.sheaf.wire import bare

REF = 'refs/heads/main'
SHA_A = 'a' * 40
SHA_B = 'b' * 40
SHA_C = 'c' * 40


def test_empty_repository_reads_as_absent(backend: sheaf.LocalBackend) -> None:
    snapshot = sheaf.Store(backend, 'p').read()
    assert snapshot.generation is None
    assert snapshot.refs == {}
    assert snapshot.packs == ()


def test_publish_creates_the_repository(backend: sheaf.LocalBackend) -> None:
    store = sheaf.Store(backend, 'p')
    base = store.read()
    after = store.publish(base, conftest.logged(base, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)})))
    assert after.tip(REF) == SHA_A
    assert after.doc.head == sheaf.SymbolicTarget(REF), 'the first publish records where a clone starts'
    assert store.read().tip(REF) == SHA_A


def test_wrong_expected_value_is_a_conflict_not_a_race(backend: sheaf.LocalBackend) -> None:
    """A non-fast-forward must never be retried away: it needs a merge, not a replay."""
    store = sheaf.Store(backend, 'p')
    store.publish(
        store.read(), conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    )
    with pytest.raises(sheaf.RefConflict) as caught:
        store.publish(
            store.read(), conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(SHA_B, SHA_C)}))
        )
    assert caught.value.ref == REF
    assert caught.value.actual == SHA_A


def test_stale_snapshot_is_a_race(backend: sheaf.LocalBackend) -> None:
    store = sheaf.Store(backend, 'p')
    stale = store.read()
    store.publish(stale, conftest.logged(stale, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)})))
    with pytest.raises(sheaf.RaceLost):
        store.publish(
            stale, conftest.logged(stale, sheaf.Intent(ref_updates={'refs/heads/other': sheaf.RefUpdate(None, SHA_B)}))
        )


def test_independent_refs_do_not_starve(backend: sheaf.LocalBackend) -> None:
    """Coarse compare-and-swap on one document must not turn disjoint ref writes into failures."""
    store = sheaf.Store(backend, 'p')
    stale = store.read()
    store.publish(stale, conftest.logged(stale, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)})))

    def build(snapshot: sheaf.Snapshot) -> sheaf.Intent:
        return conftest.logged(
            snapshot,
            sheaf.Intent(ref_updates={'refs/heads/side': sheaf.RefUpdate(snapshot.tip('refs/heads/side'), SHA_B)}),
        )

    after = store.transact(build)
    assert after.tip(REF) == SHA_A
    assert after.tip('refs/heads/side') == SHA_B


def test_transact_propagates_conflicts_without_retrying(backend: sheaf.LocalBackend) -> None:
    store = sheaf.Store(backend, 'p')
    store.publish(
        store.read(), conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    )
    calls = []

    def build(snapshot: sheaf.Snapshot) -> sheaf.Intent:
        calls.append(1)
        return conftest.logged(snapshot, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(SHA_B, SHA_C)}))

    with pytest.raises(sheaf.RefConflict):
        store.transact(build)
    assert len(calls) == 1


def test_transact_gives_up_rather_than_spinning(backend: sheaf.LocalBackend) -> None:
    store = sheaf.Store(backend, 'p')
    noise = iter(range(100))

    def build(snapshot: sheaf.Snapshot) -> sheaf.Intent:
        # Advance the document behind the builder's back, so every attempt is doomed.
        ref = f'refs/heads/noise-{next(noise)}'
        store.publish(
            snapshot, conftest.logged(snapshot, sheaf.Intent(ref_updates={ref: sheaf.RefUpdate(None, SHA_C)}))
        )
        return conftest.logged(snapshot, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(snapshot.tip(REF), SHA_A)}))

    with pytest.raises(sheaf.RetriesExhausted):
        store.transact(build, retries=3)


def test_manifest_accumulates_and_dedupes_by_content(backend: sheaf.LocalBackend) -> None:
    store = sheaf.Store(backend, 'p')
    snapshot = store.publish(
        store.read(),
        conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'PACK-1'])),
    )
    assert set(snapshot.packs) == {sheaf.pack_id(b'PACK-1')}
    snapshot = store.publish(
        snapshot,
        conftest.logged(
            snapshot, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(SHA_A, SHA_B)}, packs=[b'PACK-1', b'PACK-2'])
        ),
    )
    assert set(snapshot.packs) == {sheaf.pack_id(b'PACK-1'), sheaf.pack_id(b'PACK-2')}
    assert list(snapshot.packs) == sorted(snapshot.packs), 'a set, so stored in one order'


def test_no_ref_names_an_object_before_its_pack_exists(
    backend: sheaf.LocalBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering invariant: objects are uploaded before the ref that points at them."""
    store = sheaf.Store(backend, 'p')
    seen = []
    original_cas = backend.cas_mutable

    def recording_cas(key: str, data: bytes, expected: sheaf.Generation | None) -> sheaf.Generation:
        seen.append(('cas', sorted(info.key for info in backend.list_immutable(store.pack_prefix))))
        return original_cas(key, data, expected)

    monkeypatch.setattr(backend, 'cas_mutable', recording_cas)
    store.publish(
        store.read(),
        conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'PACK-1'])),
    )
    assert seen == [('cas', [store.pack_key(sheaf.pack_id(b'PACK-1'))])]


def test_a_field_this_build_does_not_model_survives_a_publish(backend: sheaf.LocalBackend) -> None:
    """An older build must not delete a newer one's state by writing over it.

    Every publish is a read-modify-write of the whole document, and retained generations are
    immutable, so a build that dropped what it could not name would corrupt the history it shares
    with every other build. This is the property the at-rest binary encoding is chosen for
    (`docs/design/proto.md`).
    """
    store = sheaf.Store(backend, 'p')
    published = store.publish(
        store.read(), conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    )
    # Field 111, varint, value 42 — what a later build's added field looks like to this one.
    from_the_future = b'\xf8\x06\x2a'
    backend.cas_mutable(store.ref_key, published.doc.to_bytes() + from_the_future, published.generation)

    after = store.publish(
        store.read(), conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(SHA_A, SHA_B)}))
    )

    assert after.tip(REF) == SHA_B, 'the publish itself must still land'
    assert from_the_future in backend.get_mutable(store.ref_key).data


def test_a_damaged_generation_is_not_silently_dropped(backend: sheaf.LocalBackend) -> None:
    """A reader of the ref-state log that quietly skipped a generation would misreport what the refs were."""
    store = sheaf.Store(backend, 'p')
    published = store.publish(
        store.read(), conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    )
    backend.cas_mutable(store.ref_key, b'not a ref document at all', published.generation)

    with pytest.raises(sheaf.CorruptRepository, match='not a RefDoc'):
        store.transitions()


BAD_REF_NAMES = [
    '',
    'main',
    'refs/',
    'refs/heads/has space',
    'refs/heads/has\nnewline',
    'refs/heads/../escape',
    'refs/heads/.hidden',
    'refs/heads/trailing/',
    'refs/heads/name.lock',
    'refs/heads/at@{0}',
    'refs/heads/bell\x07',
    'refs/heads/open[bracket',
]


@pytest.mark.parametrize('ref', BAD_REF_NAMES)
def test_a_ref_name_git_cannot_parse_is_refused(backend: sheaf.LocalBackend, ref: str) -> None:
    """Not cosmetic: a bad name wedges the repository permanently.

    `git update-ref --stdin` is whitespace-delimited and newline-terminated, so a name containing a
    space or a newline makes every later sync fail — and the only way out is to compare-and-swap the
    bad entry back out of the document.
    """
    store = sheaf.Store(backend, 'p')
    with pytest.raises(sheaf.InvalidRefName):
        store.publish(
            store.read(), conftest.logged(store.read(), sheaf.Intent(ref_updates={ref: sheaf.RefUpdate(None, SHA_A)}))
        )
    assert store.read().generation is None, 'nothing may be published'


# The trailing newline is the case a `$`-anchored pattern admits, and the one that wedges
# `update-ref --stdin` — its input is newline-terminated.
# A 64-hex id is well-formed for SHA-256 and refused by the SHA-1 mirror's `update-ref` all the same.
# The zero id is git's "absent" marker, which a relayed deletion carries as `new`.
@pytest.mark.parametrize(
    'oid', ['', 'zz', SHA_A[:39], SHA_A.upper(), f'{SHA_A} extra', f'{SHA_A}\n', 'f' * 64, refdoc.ZERO_OBJECT_ID]
)
def test_a_malformed_object_id_is_refused(backend: sheaf.LocalBackend, oid: str) -> None:
    """A bad object id wedges `update-ref` exactly as a bad name does."""
    store = sheaf.Store(backend, 'p')
    with pytest.raises(sheaf.InvalidRefName):
        store.publish(
            store.read(), conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, oid)}))
        )


def test_validation_happens_before_anything_is_uploaded(backend: sheaf.LocalBackend) -> None:
    """Packs go up before the swap, so validation has to come before the packs."""
    store = sheaf.Store(backend, 'p')
    with pytest.raises(sheaf.InvalidRefName):
        store.publish(
            store.read(),
            conftest.logged(
                store.read(),
                sheaf.Intent(ref_updates={'refs/heads/bad name': sheaf.RefUpdate(None, SHA_A)}, packs=[b'P']),
            ),
        )
    assert list(backend.list_immutable(store.pack_prefix)) == []


def test_a_refused_name_cannot_wedge_the_mirror(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> None:
    """The failure this guards against: a repository that can never be cloned or pushed to again."""
    writer = conftest.GitRepo.open(backend, 'p', tmp_path / 'writer.git')
    writer.append_line(ref=REF, path='f.txt', line='ok', author=conftest.Author('R', 'r@x'), message='ok')

    store = sheaf.Store(backend, 'p')
    with pytest.raises(sheaf.InvalidRefName):
        store.publish(
            store.read(),
            conftest.logged(
                store.read(), sheaf.Intent(ref_updates={'refs/heads/two words': sheaf.RefUpdate(None, SHA_B)})
            ),
        )

    mirror = bare.BareRepo(store, tmp_path / 'mirror')
    mirror.sync()
    assert REF in mirror.local_refs()
    assert not any(' ' in ref for ref in mirror.local_refs())


@pytest.mark.parametrize(
    'ref',
    [
        'refs/heads/main',
        'refs/heads/feature/nested/deep',
        'refs/sheaf/review',
        'refs/tags/v1.0.0',
        'refs/heads/dots.in.name',
        # git creates this one itself, and accepts a single component under `refs/`.
        'refs/stash',
        'refs/main',
        'refs/notes/commits',
        # A trailing dot is illegal only at the end of the whole name, so an interior one is fine.
        'refs/heads/a./b',
        *BAD_REF_NAMES,
        'refs/heads/sub/.hidden',
        'refs/heads/sub/name.lock',
        'refs/heads//double',
        'refs/heads/end.',
        'refs/heads/a.b.',
    ],
)
def test_the_validator_never_accepts_a_name_git_rejects(ref: str) -> None:
    r"""Differential test against `git check-ref-format`, the actual authority.

    One-directional, because the validator is deliberately stricter in one respect
    (`test_only_full_qualification_is_stricter_than_git`). This is the direction that matters: a
    name accepted here and rejected by git is one that wedges the repository.

    A hand-written regex is exactly the kind of thing that looks right and quietly disagrees, in
    both directions — an anchored `^\.` accepts `refs/heads/sub/.hidden`, which git rejects, while a
    per-component trailing-dot check rejects `refs/heads/a./b`, which git accepts.
    """
    checked = subprocess.run(['git', 'check-ref-format', ref], capture_output=True, check=False)
    accepted_by_git = checked.returncode == 0
    try:
        refdoc.validate_ref_name(ref)
        accepted_by_us = True
    except sheaf.InvalidRefName:
        accepted_by_us = False
    if accepted_by_us:
        assert accepted_by_git, f'{ref!r}: accepted here, rejected by git'


@pytest.mark.parametrize(
    ('ref', 'reason'),
    [
        ('foo/bar', 'fully qualified'),
        ('heads/main', 'fully qualified'),
        ('notes/commits', 'fully qualified'),
        ('refs/heads/' + 'x' * 251, 'too long'),
    ],
)
def test_the_two_ways_this_is_stricter_than_git(ref: str, reason: str) -> None:
    """The deliberate divergences, pinned so they cannot widen unnoticed.

    A name outside `refs/` would sit in the ref document where no ordinary ref enumeration looks. A
    component over 250 bytes passes `check-ref-format` but the files backend cannot create its
    `.lock`, so `update-ref` refuses it and the repository is wedged.
    """
    accepted_by_git = subprocess.run(['git', 'check-ref-format', ref], capture_output=True, check=False).returncode
    assert accepted_by_git == 0, f'{ref!r} is meant to be a name git accepts'
    with pytest.raises(sheaf.InvalidRefName, match=reason):
        refdoc.validate_ref_name(ref)


@pytest.mark.parametrize('ref', ['refs/heads/close]bracket', 'refs/heads/nbsp\xa0here', 'refs/heads/ünïcode'])
def test_a_name_git_accepts_is_accepted(ref: str) -> None:
    """The other direction of the differential: refusing a legal name is a bug too, if a smaller one."""
    assert subprocess.run(['git', 'check-ref-format', ref], capture_output=True, check=False).returncode == 0
    assert refdoc.validate_ref_name(ref) == ref


def test_a_ref_that_is_a_directory_of_another_is_refused(backend: sheaf.LocalBackend) -> None:
    """Each name is fine alone; git refuses the pair in the ref transaction, which runs after the hook.

    So a publish that admitted both would be committed before git refused the push, and the mirror
    could never write the ref set again.
    """
    store = sheaf.Store(backend, 'p')
    snapshot = store.publish(
        store.read(),
        conftest.logged(store.read(), sheaf.Intent(ref_updates={'refs/heads/a': sheaf.RefUpdate(None, SHA_A)})),
    )

    with pytest.raises(sheaf.InvalidRefName, match='cannot both exist'):
        store.publish(
            snapshot,
            conftest.logged(snapshot, sheaf.Intent(ref_updates={'refs/heads/a/b': sheaf.RefUpdate(None, SHA_A)})),
        )
    with pytest.raises(sheaf.InvalidRefName, match='cannot both exist'):
        store.publish(
            snapshot,
            conftest.logged(
                snapshot,
                sheaf.Intent(
                    ref_updates={
                        'refs/heads/x/y': sheaf.RefUpdate(None, SHA_A),
                        'refs/heads/x/y/z': sheaf.RefUpdate(None, SHA_A),
                    }
                ),
            ),
        )
    assert store.read().generation == snapshot.generation


def test_deleting_a_ref_is_refused(backend: sheaf.LocalBackend) -> None:
    """History is append-only, and the store is where that holds for a writer that is not git."""
    store = sheaf.Store(backend, 'p')
    snapshot = store.publish(
        store.read(), conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    )

    with pytest.raises(sheaf.RefDeletionRefused, match='append-only'):
        store.publish(
            snapshot, conftest.logged(snapshot, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(SHA_A, None)}))
        )
    with pytest.raises(sheaf.RefDeletionRefused):
        # Refused even where the deletion is of a ref that does not exist.
        store.publish(
            snapshot,
            conftest.logged(snapshot, sheaf.Intent(ref_updates={'refs/heads/never': sheaf.RefUpdate(None, None)})),
        )
    assert store.read().refs[REF] == SHA_A


def test_a_first_push_of_only_a_tag_leaves_head_on_an_unborn_main(backend: sheaf.LocalBackend) -> None:
    """A tag as HEAD clones detached with no branch, and nothing later would move it."""
    store = sheaf.Store(backend, 'p')
    tagged = store.publish(
        store.read(),
        conftest.logged(store.read(), sheaf.Intent(ref_updates={'refs/tags/v1': sheaf.RefUpdate(None, SHA_A)})),
    )
    assert tagged.doc.head == sheaf.SymbolicTarget('refs/heads/main')

    branched = store.publish(
        tagged, conftest.logged(tagged, sheaf.Intent(ref_updates={'refs/heads/develop': sheaf.RefUpdate(None, SHA_A)}))
    )
    assert branched.doc.head == sheaf.SymbolicTarget('refs/heads/develop'), 'the first branch to exist becomes HEAD'

    with_main = store.publish(
        branched, conftest.logged(branched, sheaf.Intent(ref_updates={'refs/heads/main': sheaf.RefUpdate(None, SHA_B)}))
    )
    assert with_main.doc.head == sheaf.SymbolicTarget('refs/heads/develop'), 'a HEAD that resolves is not re-guessed'


@pytest.mark.parametrize('keep', ['nothing', 'everything but HEAD'])
def test_a_document_naming_no_head_is_refused(backend: sheaf.LocalBackend, keep: str) -> None:
    """Both of these are valid encodings, and one is a document naming refs and packs but no HEAD.

    Every stored document names a HEAD, because `publish` will not write one that does not, and HEAD
    carries the highest field number — so a truncation loses it first, leaving exactly the second
    case: a manifest intact and the mark of a complete write gone.
    """
    store = sheaf.Store(backend, 'p')
    published = store.publish(
        store.read(),
        conftest.logged(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'PACK-1'])),
    )
    whole = published.doc.to_bytes()
    headless = refdoc_pb2.RefDoc.FromString(whole)
    headless.ClearField('head')
    boundary = len(headless.SerializeToString(deterministic=True))
    assert whole[:boundary] == headless.SerializeToString(deterministic=True), 'HEAD must be the trailing field'
    damaged = whole[:boundary] if keep == 'everything but HEAD' else b''
    if damaged:
        assert refdoc_pb2.RefDoc.FromString(damaged).packs, 'the manifest must survive, or this tests a decode error'
    backend.cas_mutable(store.ref_key, damaged, published.generation)

    with pytest.raises(sheaf.CorruptRepository, match='names no HEAD'):
        store.transitions()


def test_documents_equal_under_unmodelled_fields_hash_alike() -> None:
    """Message equality compares the unknown-field set as a set; serialising it emits parse order.

    So a hash taken over the bytes disagrees with `==` for two documents that carry the same
    unmodelled fields in different orders — which is the multi-build history this type exists to
    survive, not a contrived case.
    """
    base = refdoc_pb2.RefDoc(head=refdoc_pb2.RefTarget(ref='refs/heads/main')).SerializeToString()
    first, second = b'\xf8\x06\x2a', b'\x80\x07\x2a'

    one = refdoc.RefDoc.from_bytes(base + first + second)
    other = refdoc.RefDoc.from_bytes(base + second + first)

    assert one == other
    assert one.to_bytes() != other.to_bytes(), 'the orders must really differ, or this proves nothing'
    assert hash(one) == hash(other)
    assert len({one, other}) == 1


def test_a_ref_naming_another_ref_is_refused() -> None:
    """The encoding admits it and git allows it; no reader here resolves one.

    Answering with the rest of the refs would drop a ref that exists from a set callers treat as
    complete — a mirror hydrated from it would be missing a branch with nothing raised.
    """
    message = refdoc_pb2.RefDoc(head=refdoc_pb2.RefTarget(ref=REF))
    message.refs[REF].oid = SHA_A
    message.refs['refs/heads/alias'].ref = REF

    with pytest.raises(ValueError, match='names another ref'):
        _ = refdoc.RefDoc.from_bytes(message.SerializeToString()).refs


def test_a_target_naming_neither_an_object_nor_a_ref_is_refused() -> None:
    """A present-but-empty target is what a later build's third arm looks like to this one.

    Both arms unset reads as an empty string in either, so a build that guessed would hand out `''`
    as an object id and wedge the git invocation it reached.
    """
    message = refdoc_pb2.RefDoc(packs=['x'])
    message.head.SetInParent()

    with pytest.raises(ValueError, match='neither an object nor a ref'):
        refdoc.RefDoc.from_bytes(message.SerializeToString())


@pytest.mark.parametrize(
    'head',
    [
        refdoc.SymbolicTarget('heads/main'),
        refdoc.SymbolicTarget('refs/heads/has a space'),
        refdoc.DirectTarget('nonsense'),
    ],
    ids=['unqualified', 'illegal character', 'not an object id'],
)
def test_a_head_git_cannot_parse_is_refused(backend: sheaf.LocalBackend, head: refdoc.Target) -> None:
    """HEAD reaches git as a ref name like any other, so it is validated like one — before uploading.

    A caller's HEAD is checked ahead of the packs because a publish that uploaded first and then
    refused would leave litter for a mistake that was knowable up front.
    """
    store = sheaf.Store(backend, 'p')

    with pytest.raises(sheaf.InvalidRefName):
        store.publish(
            store.read(),
            conftest.logged(
                store.read(),
                sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'PACK-1'], head=head),
            ),
        )

    assert not list(backend.list_immutable(store.pack_prefix))


def test_a_field_this_build_does_not_model_inside_a_target_survives_and_is_seen() -> None:
    """The interesting place for a later build to put a field is on a ref's target, not the document.

    An unknown-field set belongs to the message that carried it, so a top-level check sees nothing
    of a map value's. It has to survive `advance`, which rewrites the map, and it has to count as
    unmodelled state, or a sweep would act on a document it cannot fully read.
    """
    message = refdoc_pb2.RefDoc(head=refdoc_pb2.RefTarget(ref=REF))
    message.refs[REF].oid = SHA_A
    on_target = message.refs[REF].SerializeToString() + b'\xf8\x06\x2a'
    message.refs[REF].ParseFromString(on_target)

    doc = refdoc.RefDoc.from_bytes(message.SerializeToString())
    advanced = doc.advance(refs={REF: SHA_B}, packs=['x'], head=refdoc.SymbolicTarget(REF))

    assert doc.carries_unmodelled_state
    assert advanced.carries_unmodelled_state
    assert refdoc_pb2.RefDoc.FromString(advanced.to_bytes()).refs[REF].SerializeToString().endswith(b'\xf8\x06\x2a')
    assert advanced.refs == {REF: SHA_B}


def test_a_publish_that_moves_a_ref_without_the_reflog_is_refused(backend: sheaf.LocalBackend) -> None:
    """The reflog is a second thing for a writer to remember, and forgetting it fails silently otherwise."""
    store = sheaf.Store(backend, 'p')

    with pytest.raises(sheaf.ReflogRequired, match='refs/heads/main'):
        store.publish(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    assert store.read().generation is None, 'nothing may be published'


def test_a_stale_reflog_old_value_is_a_lost_race_not_a_conflict(backend: sheaf.LocalBackend) -> None:
    """Every publish touches the reflog ref, so its `old` is stale whenever the document is.

    That must stay a lost race and not a conflict, or every race would be an unretryable rejection.
    It does, because the conflict check is against the writer's own snapshot — which its reflog `old`
    was derived from — and only the compare-and-swap sees the live document.
    """
    store = sheaf.Store(backend, 'p')
    stale = store.read()
    store.publish(stale, conftest.logged(stale, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)})))

    with pytest.raises(sheaf.RaceLost):
        store.publish(
            stale, conftest.logged(stale, sheaf.Intent(ref_updates={'refs/heads/other': sheaf.RefUpdate(None, SHA_B)}))
        )


def test_the_zero_id_is_not_an_object_id() -> None:
    """Git's marker for an absent ref; a writer relaying a push maps it to None before the store sees it."""
    with pytest.raises(sheaf.InvalidRefName, match='zero id'):
        refdoc.validate_object_id(refdoc.ZERO_OBJECT_ID)


@pytest.mark.parametrize('ident', ['', 'f' * 63, 'F' * 64, 'f' * 64 + '\n', 'g' * 64, SHA_A])
def test_a_malformed_pack_id_is_refused(ident: str) -> None:
    """A pack id becomes an object key and a manifest entry, so its form is fixed before either."""
    with pytest.raises(sheaf.InvalidPackId):
        refdoc.validate_pack_id(ident)


def test_a_well_formed_pack_id_is_accepted() -> None:
    assert refdoc.validate_pack_id(sheaf.pack_id(b'PACK-1')) == sheaf.pack_id(b'PACK-1')


def test_a_stored_pack_is_named_by_the_publish_that_follows(backend: sheaf.LocalBackend) -> None:
    """Packs arriving one at a time are stored as each completes and named together at the end."""
    store = sheaf.Store(backend, 'p')
    first = store.put_pack(b'PACK-1')
    second = store.put_pack(b'PACK-2')
    assert {first, second} == {sheaf.pack_id(b'PACK-1'), sheaf.pack_id(b'PACK-2')}
    assert store.read().generation is None, 'storing a pack publishes nothing'

    base = store.read()
    after = store.publish(
        base,
        conftest.logged(
            base,
            sheaf.Intent(
                ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'PACK-3'], stored_packs=[first, second]
            ),
        ),
    )

    assert set(after.packs) == {first, second, sheaf.pack_id(b'PACK-3')}
    assert store.fetch_pack(first) == b'PACK-1'


def test_a_stored_pack_id_is_validated_before_it_enters_the_manifest(backend: sheaf.LocalBackend) -> None:
    store = sheaf.Store(backend, 'p')
    base = store.read()
    with pytest.raises(sheaf.InvalidPackId):
        store.publish(
            base,
            conftest.logged(base, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, stored_packs=['nope'])),
        )
    assert store.read().generation is None


def test_plan_is_the_document_publish_writes_and_uploads_nothing(backend: sheaf.LocalBackend) -> None:
    store = sheaf.Store(backend, 'p')
    base = store.read()
    intent = conftest.logged(base, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'PACK-1']))

    planned = store.plan(base, intent)

    assert not list(backend.list_immutable(store.pack_prefix))
    assert store.read().generation is None
    assert store.publish(base, intent).doc == planned


def test_plan_refuses_what_publish_refuses(backend: sheaf.LocalBackend) -> None:
    store = sheaf.Store(backend, 'p')
    base = store.read()
    with pytest.raises(sheaf.RefConflict):
        store.plan(base, conftest.logged(base, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(SHA_B, SHA_A)})))
    with pytest.raises(sheaf.ReflogRequired):
        store.plan(base, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))


def _logged_updates(base: sheaf.Snapshot, **moves: tuple[str | None, str]) -> dict[str, sheaf.RefUpdate]:
    updates = {ref: sheaf.RefUpdate(*move) for ref, move in moves.items()}
    return dict(conftest.logged(base, sheaf.Intent(ref_updates=updates)).ref_updates)


def test_a_moved_document_holding_every_new_tip_means_the_publish_landed(backend: sheaf.LocalBackend) -> None:
    """The receipt arm: only the response was lost, so a retry of the same intent is a success."""
    store = sheaf.Store(backend, 'p')
    base = store.read()
    updates = _logged_updates(base, **{REF: (None, SHA_A), 'refs/heads/side': (None, SHA_B)})
    live = store.publish(base, sheaf.Intent(ref_updates=updates))

    verdict = sheaf.classify(live.refs, updates)

    assert verdict == sheaf.Classification(sheaf.Verdict.LANDED, (REF, 'refs/heads/side'))


def test_a_moved_document_with_every_old_tip_intact_is_a_lost_race(backend: sheaf.LocalBackend) -> None:
    """The reflog ref has moved too, and must not count: it moves on every publish."""
    store = sheaf.Store(backend, 'p')
    base = store.read()
    unrelated = store.publish(
        base, sheaf.Intent(ref_updates=_logged_updates(base, **{'refs/heads/other': (None, SHA_C)}))
    )
    assert unrelated.tip(refdoc.REFLOG_REF) is not None

    verdict = sheaf.classify(unrelated.refs, _logged_updates(base, **{REF: (None, SHA_A)}))

    assert verdict == sheaf.Classification(sheaf.Verdict.LOST_RACE, (REF,))


def test_a_moved_document_where_a_moved_ref_moved_names_that_ref(backend: sheaf.LocalBackend) -> None:
    """The non-fast-forward arm, naming only the refs that moved under the caller."""
    store = sheaf.Store(backend, 'p')
    base = store.read()
    seeded = store.publish(base, sheaf.Intent(ref_updates=_logged_updates(base, **{REF: (None, SHA_A)})))
    updates = _logged_updates(seeded, **{REF: (SHA_A, SHA_B), 'refs/heads/side': (None, SHA_C)})
    winner = store.publish(seeded, sheaf.Intent(ref_updates=_logged_updates(seeded, **{REF: (SHA_A, SHA_C)})))

    verdict = sheaf.classify(winner.refs, updates)

    assert verdict == sheaf.Classification(sheaf.Verdict.REF_MOVED, (REF,))


def test_a_publish_moving_only_bookkeeping_cannot_be_classified() -> None:
    """Over no refs, every arm would hold vacuously and every stale publish would read as landed."""
    updates = {refdoc.REFLOG_REF: sheaf.RefUpdate(None, SHA_A), 'refs/sheaf/other': sheaf.RefUpdate(None, SHA_B)}
    with pytest.raises(sheaf.BookkeepingOnly, match='refs/sheaf/'):
        sheaf.classify({}, updates)
