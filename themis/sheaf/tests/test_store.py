"""Publish semantics, and the distinction between a lost race and a real conflict."""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from themis import sheaf
from themis.sheaf import refdoc
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
    after = store.publish(base, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    assert after.head(REF) == SHA_A
    assert after.doc.sequence == 1
    assert store.read().head(REF) == SHA_A


def test_wrong_expected_value_is_a_conflict_not_a_race(backend: sheaf.LocalBackend) -> None:
    """A non-fast-forward must never be retried away: it needs a merge, not a replay."""
    store = sheaf.Store(backend, 'p')
    store.publish(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    with pytest.raises(sheaf.RefConflict) as caught:
        store.publish(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(SHA_B, SHA_C)}))
    assert caught.value.ref == REF
    assert caught.value.actual == SHA_A


def test_stale_snapshot_is_a_race(backend: sheaf.LocalBackend) -> None:
    store = sheaf.Store(backend, 'p')
    stale = store.read()
    store.publish(stale, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    with pytest.raises(sheaf.RaceLost):
        store.publish(stale, sheaf.Intent(ref_updates={'refs/heads/other': sheaf.RefUpdate(None, SHA_B)}))


def test_independent_refs_do_not_starve(backend: sheaf.LocalBackend) -> None:
    """Coarse compare-and-swap on one document must not turn disjoint ref writes into failures."""
    store = sheaf.Store(backend, 'p')
    stale = store.read()
    store.publish(stale, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))

    def build(snapshot: sheaf.Snapshot) -> sheaf.Intent:
        return sheaf.Intent(ref_updates={'refs/heads/side': sheaf.RefUpdate(snapshot.head('refs/heads/side'), SHA_B)})

    after = store.transact(build)
    assert after.head(REF) == SHA_A
    assert after.head('refs/heads/side') == SHA_B


def test_transact_propagates_conflicts_without_retrying(backend: sheaf.LocalBackend) -> None:
    store = sheaf.Store(backend, 'p')
    store.publish(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    calls = []

    def build(_snapshot: sheaf.Snapshot) -> sheaf.Intent:
        calls.append(1)
        return sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(SHA_B, SHA_C)})

    with pytest.raises(sheaf.RefConflict):
        store.transact(build)
    assert len(calls) == 1


def test_transact_gives_up_rather_than_spinning(backend: sheaf.LocalBackend) -> None:
    store = sheaf.Store(backend, 'p')
    noise = iter(range(100))

    def build(snapshot: sheaf.Snapshot) -> sheaf.Intent:
        # Advance the document behind the builder's back, so every attempt is doomed.
        ref = f'refs/heads/noise-{next(noise)}'
        store.publish(snapshot, sheaf.Intent(ref_updates={ref: sheaf.RefUpdate(None, SHA_C)}))
        return sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(snapshot.head(REF), SHA_A)})

    with pytest.raises(sheaf.RetriesExhausted):
        store.transact(build, retries=3)


def test_manifest_accumulates_and_dedupes_by_content(backend: sheaf.LocalBackend) -> None:
    store = sheaf.Store(backend, 'p')
    snapshot = store.publish(
        store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'PACK-1'])
    )
    assert snapshot.packs == (sheaf.pack_id(b'PACK-1'),)
    snapshot = store.publish(
        snapshot,
        sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(SHA_A, SHA_B)}, packs=[b'PACK-1', b'PACK-2']),
    )
    assert snapshot.packs == (sheaf.pack_id(b'PACK-1'), sheaf.pack_id(b'PACK-2'))


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
    store.publish(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'PACK-1']))
    assert seen == [('cas', [store.pack_key(sheaf.pack_id(b'PACK-1'))])]


def test_a_generation_from_a_newer_format_is_not_silently_dropped(backend: sheaf.LocalBackend) -> None:
    """A document this code is too old to parse is unreadable history, not absent history.

    `gc.live_packs` is the only caller, and it treats every retained document as naming live packs,
    so the argument against dropping a damaged one does not depend on why the document was
    unreadable: once the format moves, an older sweep would miss the packs the newer generations
    name and delete them past grace.
    """
    store = sheaf.Store(backend, 'p')
    published = store.publish(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    ahead = json.dumps({'format': sheaf.RefDoc().format + 1, 'refs': {}, 'packs': []}).encode()
    backend.cas_mutable(store.ref_key, ahead, published.generation)

    with pytest.raises(sheaf.UnsupportedFormat):
        store.transitions()


@pytest.mark.parametrize(
    ('damaged', 'match'),
    [
        (b'not json at all', r'JSON|Expecting'),
        # Well-formed JSON of the right format, complete but for the manifest: the variant that
        # would parse rather than raise, into a document naming no packs at all. Every other field
        # is present, so nothing but the absent `packs` can be what raises.
        (
            json.dumps(
                {
                    'format': refdoc.FORMAT_VERSION,
                    'refs': {REF: SHA_A},
                    'sequence': 1,
                    'updated_by': 'test',
                    'updated_at': 0.0,
                }
            ).encode(),
            r"missing 'packs'",
        ),
    ],
    ids=['unparseable', 'well-formed but incomplete'],
)
def test_a_damaged_generation_is_not_silently_dropped(backend: sheaf.LocalBackend, damaged: bytes, match: str) -> None:
    """Garbage collection treats every retained document as naming live packs.

    Skipping a damaged one is how a sweep concludes history needs nothing and deletes a pack an
    older ref state cannot be hydrated without. A document that parses to an *empty* manifest is
    the same loss by a quieter route: it contributes nothing to the live set instead of stopping
    the sweep, and `retention_gap` does not catch it either.
    """
    store = sheaf.Store(backend, 'p')
    published = store.publish(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}))
    backend.cas_mutable(store.ref_key, damaged, published.generation)

    with pytest.raises(ValueError, match=match):
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
        store.publish(store.read(), sheaf.Intent(ref_updates={ref: sheaf.RefUpdate(None, SHA_A)}))
    assert store.read().generation is None, 'nothing may be published'


# The trailing newline is the case a `$`-anchored pattern admits, and the one that wedges
# `update-ref --stdin` — its input is newline-terminated.
@pytest.mark.parametrize('oid', ['', 'zz', SHA_A[:39], SHA_A.upper(), f'{SHA_A} extra', f'{SHA_A}\n'])
def test_a_malformed_object_id_is_refused(backend: sheaf.LocalBackend, oid: str) -> None:
    """A bad object id wedges `update-ref` exactly as a bad name does."""
    store = sheaf.Store(backend, 'p')
    with pytest.raises(sheaf.InvalidRefName):
        store.publish(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, oid)}))


def test_validation_happens_before_anything_is_uploaded(backend: sheaf.LocalBackend) -> None:
    """Packs go up before the swap, so validation has to come before the packs."""
    store = sheaf.Store(backend, 'p')
    with pytest.raises(sheaf.InvalidRefName):
        store.publish(
            store.read(),
            sheaf.Intent(ref_updates={'refs/heads/bad name': sheaf.RefUpdate(None, SHA_A)}, packs=[b'P']),
        )
    assert list(backend.list_immutable(store.pack_prefix)) == []


def test_a_refused_name_cannot_wedge_the_mirror(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> None:
    """The failure this guards against: a repository that can never be cloned or pushed to again."""
    writer = conftest.GitRepo.open(backend, 'p', tmp_path / 'writer.git')
    writer.append_line(ref=REF, path='f.txt', line='ok', author=conftest.Author('R', 'r@x'), message='ok')

    store = sheaf.Store(backend, 'p')
    with pytest.raises(sheaf.InvalidRefName):
        store.publish(store.read(), sheaf.Intent(ref_updates={'refs/heads/two words': sheaf.RefUpdate(None, SHA_B)}))

    mirror = bare.BareRepo(store, tmp_path / 'mirror')
    mirror.sync()
    assert list(mirror.local_refs()) == [REF]


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


@pytest.mark.parametrize('ref', ['foo/bar', 'heads/main', 'notes/commits'])
def test_only_full_qualification_is_stricter_than_git(ref: str) -> None:
    """The one deliberate divergence, pinned so it cannot widen unnoticed.

    A name outside `refs/` would sit in the ref document where no ordinary ref enumeration looks,
    so it is refused even though git's own format check accepts it.
    """
    accepted_by_git = subprocess.run(['git', 'check-ref-format', ref], capture_output=True, check=False).returncode
    assert accepted_by_git == 0, f'{ref!r} is meant to be a name git accepts'
    with pytest.raises(sheaf.InvalidRefName, match='fully qualified'):
        refdoc.validate_ref_name(ref)
