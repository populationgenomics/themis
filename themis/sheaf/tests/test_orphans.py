"""The orphan meter: what nothing references, counted and never deleted."""

from __future__ import annotations

import pytest

from themis import sheaf
from themis.sheaf import orphans
from themis.sheaf.tests import conftest

REF = 'refs/heads/main'
SHA_A = 'a' * 40
SHA_B = 'b' * 40
ORPHAN = b'PACK-that-lost'


def _repo_with_an_orphan(backend: sheaf.LocalBackend) -> sheaf.Store:
    """Produce a pack that was uploaded by a publish which then lost the race."""
    store = sheaf.Store(backend, 'p')
    stale = store.read()
    store.publish(
        stale,
        conftest.logged(stale, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_A)}, packs=[b'PACK-winner'])),
    )
    with pytest.raises(sheaf.RaceLost):
        store.publish(
            stale, conftest.logged(stale, sheaf.Intent(ref_updates={REF: sheaf.RefUpdate(None, SHA_B)}, packs=[ORPHAN]))
        )
    return store


def test_a_lost_race_leaves_its_pack_behind_and_the_meter_counts_it(backend: sheaf.LocalBackend) -> None:
    """Uploading before the swap is unavoidable, so this litter is by design; the meter is the bill."""
    store = _repo_with_an_orphan(backend)

    report = orphans.measure(store)

    assert report.orphans == (sheaf.pack_id(ORPHAN),)
    assert report.orphan_bytes == len(ORPHAN)
    assert report.live == (sheaf.pack_id(b'PACK-winner'),)
    assert report.live_bytes == len(b'PACK-winner')
    assert store.fetch_pack(sheaf.pack_id(ORPHAN)) == ORPHAN, 'measured, not removed'


def test_a_repository_that_does_not_exist_measures_empty(backend: sheaf.LocalBackend) -> None:
    report = orphans.measure(sheaf.Store(backend, 'p'))
    assert report == orphans.Report(live=(), orphans=(), live_bytes=0, orphan_bytes=0)


@pytest.mark.parametrize('stray', ['README', 'sub/x.pack', '.pack', 'X' * 64 + '.pack'])
def test_an_object_that_is_not_a_pack_is_not_counted(backend: sheaf.LocalBackend, stray: str) -> None:
    store = _repo_with_an_orphan(backend)
    backend.put_immutable(f'{store.pack_prefix}{stray}', b'not a pack')

    assert orphans.measure(store).orphans == (sheaf.pack_id(ORPHAN),)


def test_a_manifest_naming_a_missing_pack_is_corruption(backend: sheaf.LocalBackend) -> None:
    """Nothing deletes, so a named pack that is absent is damage, and a meter must not report around it."""
    store = _repo_with_an_orphan(backend)
    conftest.remove_object(backend, store.pack_key(sheaf.pack_id(b'PACK-winner')))

    with pytest.raises(sheaf.CorruptRepository):
        orphans.measure(store)


def test_a_publish_landing_mid_measurement_is_not_corruption(
    backend: sheaf.LocalBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publish uploads before it swaps, so a manifest read *after* the listing can name a pack the listing lacks."""
    store = _repo_with_an_orphan(backend)
    original = store.read

    def publish_then_read() -> sheaf.Snapshot:
        monkeypatch.setattr(store, 'read', original)
        store.publish(
            original(),
            conftest.logged(
                original(), sheaf.Intent(ref_updates={'refs/heads/b': sheaf.RefUpdate(None, SHA_B)}, packs=[b'P2'])
            ),
        )
        return original()

    monkeypatch.setattr(store, 'read', publish_then_read)

    report = orphans.measure(store)

    assert sheaf.pack_id(b'P2') in report.live
