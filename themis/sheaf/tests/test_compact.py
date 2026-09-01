"""Compaction: the policy that decides when it is due, and the write half that collapses a manifest.

The properties asserted here are that refs never move, that a superseded pack stays fetchable, and
that a reader hydrating the manifest being replaced is unaffected.
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess

import pytest

from themis import sheaf
from themis.sheaf import compact, gc
from themis.sheaf.tests import conftest
from themis.sheaf.wire import bare, server

REPO, REF = 'projects/case', 'refs/heads/main'
SIDE_REF = 'refs/heads/side'
LOG = 'annotations/assertions.jsonl'
REVIEWER = conftest.Author('Reviewer One', 'reviewer.one@example.org')
APPENDS = 20


def _bulky(seed: str, kb: int) -> str:
    """Distinct, poorly compressible text.

    Repetitive filler would deflate to almost nothing, and identical files would deduplicate to a
    single blob — either way the base pack ends up smaller than the appends and the ratio test
    measures the wrong thing.
    """
    chunks = []
    while sum(len(c) for c in chunks) < kb * 1024:
        chunks.append(hashlib.sha256(f'{seed}:{len(chunks)}'.encode()).hexdigest())
    return '\n'.join(chunks) + '\n'


@pytest.fixture
def seeded(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> conftest.GitRepo:
    """A repository with one sizeable commit and a run of small appends."""
    writer = conftest.GitRepo.open(backend, REPO, tmp_path / 'writer.git')
    writer.write_files(
        ref=REF,
        author=REVIEWER,
        message='seed the report',
        files={f'documents/report-{i}.md': _bulky(f'report-{i}', 50) for i in range(8)},
    )
    for i in range(APPENDS):
        writer.append_line(ref=REF, path=LOG, line=f'{{"code":"C{i}"}}', author=REVIEWER, message=f'review C{i}')
    return writer


def _mirror(backend: sheaf.LocalBackend, tmp_path: pathlib.Path, name: str = 'mirror') -> bare.BareRepo:
    return bare.BareRepo(sheaf.Store(backend, REPO), tmp_path / name)


def test_a_single_pack_is_never_due(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> None:
    writer = conftest.GitRepo.open(backend, REPO, tmp_path / 'writer.git')
    writer.append_line(ref=REF, path=LOG, line='one', author=REVIEWER, message='one')
    store = sheaf.Store(backend, REPO)
    verdict = compact.assess(store, store.read())
    assert not verdict.due
    assert verdict.packs == 1


def test_the_count_cap_triggers_before_the_ratio_does(backend: sheaf.LocalBackend, seeded: conftest.GitRepo) -> None:
    """A long run of tiny appends stays well under the ratio while inflating the manifest."""
    del seeded
    store = sheaf.Store(backend, REPO)
    verdict = compact.assess(store, store.read(), compact.Policy(max_packs=8, ratio=10.0))
    assert verdict.due
    assert 'exceeds cap' in verdict.reason


def test_the_ratio_triggers_when_loose_packs_grow_large(backend: sheaf.LocalBackend, seeded: conftest.GitRepo) -> None:
    del seeded
    store = sheaf.Store(backend, REPO)
    verdict = compact.assess(store, store.read(), compact.Policy(max_packs=1000, ratio=0.01))
    assert verdict.due
    assert 'loose packs exceeds' in verdict.reason


def test_a_big_base_with_small_appends_is_left_alone(backend: sheaf.LocalBackend, seeded: conftest.GitRepo) -> None:
    """Write amplification is the thing being bounded, so a large base must not be rewritten cheaply."""
    del seeded
    store = sheaf.Store(backend, REPO)
    verdict = compact.assess(store, store.read(), compact.Policy(max_packs=1000, ratio=10.0))
    assert not verdict.due
    assert verdict.base_bytes > verdict.loose_bytes


def test_compaction_collapses_the_manifest_and_keeps_the_content(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path, seeded: conftest.GitRepo
) -> None:
    store = sheaf.Store(backend, REPO)
    before = store.read()
    manifest_before = len(backend.get_mutable(store.ref_key).data)
    expected = seeded.read_log(ref=REF, path=LOG)

    result = compact.compact(store, _mirror(backend, tmp_path))

    assert result.outcome is compact.Outcome.REPLACED
    after = result.snapshot
    assert after is not None
    assert len(after.packs) == 1
    assert after.refs == before.refs, 'compaction must not move a ref'
    assert len(backend.get_mutable(store.ref_key).data) < manifest_before // 4

    fresh = conftest.GitRepo.open(backend, REPO, tmp_path / 'verify.git')
    assert fresh.read_log(ref=REF, path=LOG) == expected
    assert len(fresh.history(REF)) == len(before.packs)


def test_superseded_packs_are_left_in_place(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path, seeded: conftest.GitRepo
) -> None:
    """The safe half. Only `themis.sheaf.gc` deletes, and only behind a grace period."""
    del seeded
    store = sheaf.Store(backend, REPO)
    before = set(store.read().packs)

    compact.compact(store, _mirror(backend, tmp_path))

    keys = {info.key for info in backend.list_immutable(store.pack_prefix)}
    assert {store.pack_key(i) for i in before} <= keys
    # Retained transitions still name them, so gc considers them live.
    assert before <= gc.live_packs(store)


def test_a_reader_mid_hydrate_is_unaffected(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path, seeded: conftest.GitRepo
) -> None:
    """The one race compaction introduces: somebody is hydrating the manifest being replaced."""
    del seeded
    store = sheaf.Store(backend, REPO)
    stale = store.read()

    compact.compact(store, _mirror(backend, tmp_path, 'compactor'))

    # The reader now fetches the pack set it read before compaction landed.
    reader = _mirror(backend, tmp_path, 'reader')
    reader.ensure()
    reader.install(stale.packs)
    head = stale.head(REF)
    assert head is not None
    assert bare.git('cat-file', '-t', head, cwd=reader.path).strip() == b'commit'
    assert bare.git('cat-file', '-p', f'{head}:{LOG}', cwd=reader.path).decode().splitlines()[0] == '{"code":"C0"}'


def test_compaction_that_loses_the_race_changes_nothing(
    backend: sheaf.LocalBackend,
    tmp_path: pathlib.Path,
    seeded: conftest.GitRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pack set built from an older snapshot would name commits it does not contain, so it must lose."""
    store = sheaf.Store(backend, REPO)
    mirror = _mirror(backend, tmp_path)
    original = mirror.repack

    def repack_then_get_overtaken() -> list[pathlib.Path]:
        packs = original()
        seeded.append_line(ref=REF, path=LOG, line='{"code":"LATE"}', author=REVIEWER, message='review LATE')
        return packs

    monkeypatch.setattr(mirror, 'repack', repack_then_get_overtaken)
    assert compact.compact(store, mirror).outcome is compact.Outcome.RACED

    after = store.read()
    assert len(after.packs) > 1, 'the manifest must not have been replaced'
    assert seeded.read_log(ref=REF, path=LOG)[-1] == '{"code":"LATE"}'


def test_compact_if_due_does_nothing_when_it_is_not(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path, seeded: conftest.GitRepo
) -> None:
    del seeded
    store = sheaf.Store(backend, REPO)
    before = store.read()
    policy = compact.Policy(max_packs=1000, ratio=10.0)
    assert compact.compact_if_due(store, _mirror(backend, tmp_path), policy=policy).outcome is compact.Outcome.NOT_DUE
    assert store.read().generation == before.generation


def test_a_sync_with_nothing_to_collapse_is_not_a_replacement(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path, seeded: conftest.GitRepo
) -> None:
    """Finding one pack is a distinct outcome from rewriting the manifest.

    Both leave the caller a usable snapshot, so a result that carried only the snapshot could not
    tell them apart — and a caller metering compaction would count every no-op sync as work done.
    """
    del seeded
    store = sheaf.Store(backend, REPO)
    assert compact.compact(store, _mirror(backend, tmp_path)).outcome is compact.Outcome.REPLACED

    second = compact.compact(store, _mirror(backend, tmp_path, 'again'))

    assert second.outcome is compact.Outcome.NOTHING_TO_COLLAPSE
    assert not second.replaced
    assert second.snapshot is not None, 'the synced state is still what the caller should use'


def test_a_raced_compaction_leaves_the_mirror_hydratable(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path, seeded: conftest.GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`repack` discards what the mirror's refs do not reach, so a lost race must not keep the markers.

    A ref deleted in the store goes unreachable in the mirror, and `git repack -a -d` drops its
    objects for good. The store still holds them — a manifest only ever gains packs — so the mirror
    can heal by re-fetching. It only does that if the markers stop claiming those packs are already
    installed, and the marker update used to sit on the success path alone. Restoring the ref is
    what needs them back: it names a commit the store already published, so no new pack arrives.
    """
    del seeded
    store = sheaf.Store(backend, REPO)
    writer = conftest.GitRepo.open(backend, REPO, tmp_path / 'writer.git')
    side = writer.append_line(ref=SIDE_REF, path=LOG, line='{"code":"SIDE"}', author=REVIEWER, message='review SIDE')
    tip = side.head(SIDE_REF)
    assert tip is not None
    mirror = _mirror(backend, tmp_path, 'compactor')
    mirror.sync()
    store.publish(store.read(), sheaf.Intent(ref_updates={SIDE_REF: sheaf.RefUpdate(tip, None)}))

    original = mirror.repack

    def repack_then_get_overtaken() -> list[pathlib.Path]:
        packs = original()
        writer.append_line(ref=REF, path=LOG, line='{"code":"LATE"}', author=REVIEWER, message='review LATE')
        return packs

    monkeypatch.setattr(mirror, 'repack', repack_then_get_overtaken)
    assert compact.compact(store, mirror).outcome is compact.Outcome.RACED

    # The store never lost the objects, so restoring the ref publishes no pack of its own.
    store.publish(store.read(), sheaf.Intent(ref_updates={SIDE_REF: sheaf.RefUpdate(None, tip)}))
    assert mirror.sync().head(SIDE_REF) == tip


def test_a_raced_compaction_repairs_the_mirror_without_the_store(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path, seeded: conftest.GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The markers are the packs, so what a repack discarded is still on disk to re-index.

    The assertion is narrow on purpose: the winner's own pack is new and has to be fetched, so
    what must not be fetched is the set the mirror was already holding when the repack ran.
    """
    del seeded
    store = sheaf.Store(backend, REPO)
    writer = conftest.GitRepo.open(backend, REPO, tmp_path / 'writer.git')
    side = writer.append_line(ref=SIDE_REF, path=LOG, line='{"code":"SIDE"}', author=REVIEWER, message='review SIDE')
    tip = side.head(SIDE_REF)
    assert tip is not None
    mirror = _mirror(backend, tmp_path, 'compactor')
    mirror.sync()
    store.publish(store.read(), sheaf.Intent(ref_updates={SIDE_REF: sheaf.RefUpdate(tip, None)}))

    original = mirror.repack

    def repack_then_get_overtaken() -> list[pathlib.Path]:
        packs = original()
        writer.append_line(ref=REF, path=LOG, line='{"code":"LATE"}', author=REVIEWER, message='review LATE')
        return packs

    held = set(store.read().packs)
    monkeypatch.setattr(mirror, 'repack', repack_then_get_overtaken)
    assert compact.compact(store, mirror).outcome is compact.Outcome.RACED
    store.publish(store.read(), sheaf.Intent(ref_updates={SIDE_REF: sheaf.RefUpdate(None, tip)}))

    fetched: list[str] = []
    original_fetch = mirror.store.fetch_pack

    def recording_fetch(ident: str) -> bytes:
        fetched.append(ident)
        return original_fetch(ident)

    monkeypatch.setattr(mirror.store, 'fetch_pack', recording_fetch)

    assert mirror.sync().head(SIDE_REF) == tip
    assert held, 'the mirror must have been holding something for this to prove anything'
    assert not held & set(fetched), 'the repack discarded these, and the markers still held them'


def test_a_clone_still_works_after_compaction(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path, seeded: conftest.GitRepo
) -> None:
    del seeded
    store = sheaf.Store(backend, REPO)
    compact.compact(store, _mirror(backend, tmp_path))
    with server.SheafGitServer(backend, tmp_path / 'bare', repos={REPO}) as instance:
        work = tmp_path / 'work'
        subprocess.run(
            ['git', 'clone', '-q', instance.url(REPO), str(work)],
            capture_output=True,
            check=True,
            timeout=120,
        )
    assert (work / LOG).read_text('utf-8').splitlines()[-1] == f'{{"code":"C{APPENDS - 1}"}}'
    assert (work / 'documents' / 'report-0.md').exists()


def test_concurrency_does_not_change_the_result(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path, seeded: conftest.GitRepo
) -> None:
    del seeded
    store = sheaf.Store(backend, REPO)
    serial = bare.BareRepo(store, tmp_path / 'serial', concurrency=1)
    parallel = bare.BareRepo(store, tmp_path / 'parallel', concurrency=16)
    assert serial.sync().refs == parallel.sync().refs
    assert serial.local_refs() == parallel.local_refs()
    for mirror in (serial, parallel):
        head = mirror.local_refs()[REF]
        assert bare.git('cat-file', '-p', f'{head}:{LOG}', cwd=mirror.path).decode().count('\n') == APPENDS


def test_sync_is_idempotent(
    backend: sheaf.LocalBackend,
    tmp_path: pathlib.Path,
    seeded: conftest.GitRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del seeded
    mirror = _mirror(backend, tmp_path)
    mirror.sync()
    installed = mirror.installed()
    fetched: list[str] = []
    original = mirror.store.fetch_pack

    def recording_fetch(ident: str) -> bytes:
        fetched.append(ident)
        return original(ident)

    monkeypatch.setattr(mirror.store, 'fetch_pack', recording_fetch)
    mirror.sync()
    assert fetched == [], 'a second sync must not re-download anything'
    assert mirror.installed() == installed


def test_head_tracks_the_store_not_the_host(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path, seeded: conftest.GitRepo
) -> None:
    """`git init --bare` takes HEAD from the host's `init.defaultBranch`.

    On a host defaulting to `master`, a mirror hydrated from a store whose only branch is `main`
    would advertise a HEAD pointing at nothing — and a client would clone the refs, check out an
    empty tree, and report no error at all.
    """
    del seeded
    mirror = _mirror(backend, tmp_path)
    mirror.ensure()
    bare.git('symbolic-ref', 'HEAD', 'refs/heads/master', cwd=mirror.path)
    snapshot = mirror.sync()
    assert bare.git('symbolic-ref', 'HEAD', cwd=mirror.path).decode().strip() == REF
    assert REF in snapshot.refs
