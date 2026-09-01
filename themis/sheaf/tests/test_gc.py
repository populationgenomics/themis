"""Orphaned packs: the one place git's push quarantine does not translate."""

from __future__ import annotations

import pathlib
from typing import override

import pytest

from themis import sheaf
from themis.sheaf import backend as backend_mod
from themis.sheaf import gc

REF = 'refs/heads/main'
SHA_A = 'a' * 40
SHA_B = 'b' * 40
ORPHAN = b'PACK-that-lost'


def _repo_with_an_orphan(backend: sheaf.LocalBackend) -> sheaf.Store:
    """Produce a pack that was uploaded by a publish which then lost the race."""
    store = sheaf.Store(backend, 'p')
    stale = store.read()
    store.publish(stale, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'PACK-winner']))
    with pytest.raises(sheaf.RaceLost):
        store.publish(stale, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_B)}, packs=[ORPHAN]))
    return store


def test_a_lost_race_leaves_its_pack_behind(backend: sheaf.LocalBackend) -> None:
    """Uploading before the swap is unavoidable, so this litter is by design, not a bug."""
    store = _repo_with_an_orphan(backend)
    keys = {info.key for info in backend.list_immutable(store.pack_prefix)}
    assert store.pack_key(sheaf.pack_id(ORPHAN)) in keys
    assert sheaf.pack_id(ORPHAN) not in store.read().packs


def test_grace_protects_an_in_flight_pack(backend: sheaf.LocalBackend) -> None:
    """Without grace, a sweep would delete a pack a slow publish is about to name."""
    store = _repo_with_an_orphan(backend)
    report = gc.find_orphans(store, grace=3600)
    assert report.orphans == ()
    assert sheaf.pack_id(ORPHAN) in report.protected


def test_an_aged_orphan_is_reclaimed(backend: sheaf.LocalBackend) -> None:
    store = _repo_with_an_orphan(backend)
    report = gc.find_orphans(store, grace=0)
    assert report.orphans == (sheaf.pack_id(ORPHAN),)
    assert report.orphan_bytes == len(ORPHAN)

    gc.collect(store, grace=0, dry_run=False)
    remaining = {info.key for info in backend.list_immutable(store.pack_prefix)}
    assert remaining == {store.pack_key(sheaf.pack_id(b'PACK-winner'))}


def test_dry_run_deletes_nothing(backend: sheaf.LocalBackend) -> None:
    store = _repo_with_an_orphan(backend)
    before = {info.key for info in backend.list_immutable(store.pack_prefix)}
    gc.collect(store, grace=0)
    assert {info.key for info in backend.list_immutable(store.pack_prefix)} == before


def test_history_keeps_its_packs_live(backend: sheaf.LocalBackend) -> None:
    """Compaction replaces the manifest, so a retained transition is what keeps old states readable."""
    store = sheaf.Store(backend, 'p')
    seed = sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'PACK-old'])
    first = store.publish(store.read(), seed)
    store.publish(first, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(SHA_A, SHA_B)}, packs=[b'PACK-new']))
    assert gc.live_packs(store) == {sheaf.pack_id(b'PACK-old'), sheaf.pack_id(b'PACK-new')}


class _UnversionedBackend(sheaf.LocalBackend):
    """A backend retaining only the live generation, as an unversioned bucket does."""

    @override
    def history_mutable(self, key: str) -> list[backend_mod.StoredBlob]:
        return super().history_mutable(key)[:1]


def test_a_sweep_refuses_when_history_is_not_retained(tmp_path: pathlib.Path) -> None:
    """Unretained is not unreachable, and only the retained generations say which packs history needs.

    The one existing exercise of this guard runs against the GCS emulator, so it is skipped wherever
    Docker is absent and the suite still reports green — on the guard for the only operation here
    that destroys data.
    """
    store = sheaf.Store(_UnversionedBackend(tmp_path), 'p')
    snapshot = store.publish(store.read(), sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'P1']))
    store.publish(snapshot, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(SHA_A, SHA_B)}, packs=[b'P2']))

    with pytest.raises(sheaf.RetentionUnavailable):
        gc.find_orphans(store, grace=0)
