"""The compare-and-swap contract, which everything else rests on.

Every test here runs against both backends: the local-directory implementation and the real GCS
client against fake-gcs-server.

The one behaviour the emulator cannot cover is retained noncurrent generations — fake-gcs-server
answers `not implemented: fs storage type does not support versioning yet`, and its memory backend
accepts the setting while still retaining nothing. So `history_mutable` is asserted only against the
local backend, and `test_gcs.py` pins that gap rather than letting a green suite imply coverage.
"""

from __future__ import annotations

import time
from concurrent import futures

import pytest

from themis import sheaf


def test_absent_key_raises(any_backend: sheaf.Backend) -> None:
    with pytest.raises(sheaf.NotFound):
        any_backend.get_mutable('missing')


def test_create_requires_absence(any_backend: sheaf.Backend) -> None:
    any_backend.cas_mutable('k', b'one', None)
    with pytest.raises(sheaf.PreconditionFailed):
        any_backend.cas_mutable('k', b'again', None)


def test_round_trip_and_advance(any_backend: sheaf.Backend) -> None:
    first = any_backend.cas_mutable('k', b'one', None)
    assert any_backend.get_mutable('k').data == b'one'
    second = any_backend.cas_mutable('k', b'two', first)
    assert second != first
    assert any_backend.get_mutable('k').generation == second


def test_stale_generation_is_rejected(any_backend: sheaf.Backend) -> None:
    first = any_backend.cas_mutable('k', b'one', None)
    any_backend.cas_mutable('k', b'two', first)
    with pytest.raises(sheaf.PreconditionFailed):
        any_backend.cas_mutable('k', b'three', first)


def test_history_is_the_reflog(backend: sheaf.LocalBackend) -> None:  # local only: see the module docstring
    generation = None
    for value in (b'a', b'b', b'c'):
        generation = backend.cas_mutable('k', value, generation)
    history = backend.history_mutable('k')
    assert [blob.data for blob in history] == [b'c', b'b', b'a']


def test_exactly_one_writer_wins_a_race(any_backend: sheaf.Backend) -> None:
    """The property the whole design depends on: N writers, one winner, no torn value."""
    base = any_backend.cas_mutable('k', b'base', None)
    attempts = 32
    with futures.ThreadPoolExecutor(max_workers=attempts) as pool:
        results = [pool.submit(_try_write, any_backend, base, i) for i in range(attempts)]
        outcomes = [r.result() for r in results]
    winners = [o for o in outcomes if o is not None]
    assert len(winners) == 1
    assert any_backend.get_mutable('k').data == f'writer-{winners[0]}'.encode()


def _try_write(any_backend: sheaf.Backend, base: sheaf.Generation, index: int) -> int | None:
    try:
        any_backend.cas_mutable('k', f'writer-{index}'.encode(), base)
    except sheaf.PreconditionFailed:
        return None
    return index


def test_immutable_put_is_idempotent(any_backend: sheaf.Backend) -> None:
    any_backend.put_immutable('p/one.pack', b'bytes')
    any_backend.put_immutable('p/one.pack', b'bytes')
    assert any_backend.get_immutable('p/one.pack') == b'bytes'
    assert [info.key for info in any_backend.list_immutable('p/')] == ['p/one.pack']
    any_backend.delete_immutable('p/one.pack')
    assert list(any_backend.list_immutable('p/')) == []


def test_a_listing_reports_a_real_size_and_creation_time(any_backend: sheaf.Backend) -> None:
    """Garbage collection prices its grace window on `created_at`, and its report on `size`.

    A backend defaulting `created_at` makes a pack uploaded seconds ago read as 1970, which is past
    every grace window — so the sweep deletes the one thing grace exists to protect, an object an
    in-flight publish is about to name. Pinning the keys alone leaves that free to regress.
    """
    any_backend.put_immutable('p/one.pack', b'0123456789')

    (info,) = list(any_backend.list_immutable('p/'))

    assert info.size == 10
    # Generously wide so a container clock offset cannot fail it; 1970 still cannot pass.
    assert time.time() - info.created_at < 3600


def test_re_putting_an_object_does_not_refresh_its_age(any_backend: sheaf.Backend) -> None:
    """The grace window is measured from the creation time, so a re-put must not reset it.

    Keys are content-addressed, so a publish replaying work already stored writes the same bytes to
    the same key. If that renewed the age, a pack orphaned long enough to lose grace would regain it
    by being named again — and the two backends would sweep differently, since one issues a
    conditional create and the other writes unconditionally.
    """
    any_backend.put_immutable('p/one.pack', b'0123456789')
    ((before),) = list(any_backend.list_immutable('p/'))

    any_backend.put_immutable('p/one.pack', b'0123456789')

    ((after),) = list(any_backend.list_immutable('p/'))
    assert after.created_at == before.created_at
