"""The compare-and-swap contract, which everything else rests on.

Every test here runs against both backends: the local-directory implementation and the real GCS
client against fake-gcs-server.

The one behaviour the emulator cannot cover is retained noncurrent generations — fake-gcs-server
answers `not implemented: fs storage type does not support versioning yet`, and its memory backend
accepts the setting while still retaining nothing. So `history_mutable` is asserted only against the
local backend, and `test_gcs.py` pins that gap rather than letting a green suite imply coverage.
"""

from __future__ import annotations

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


def test_immutable_put_is_create_if_absent(any_backend: sheaf.Backend) -> None:
    """A second put of the same key is a no-op, and the listing reports the object once with its size.

    Keys are content-addressed, so the second put carries the same bytes by construction; the test
    sends different ones to show the first write is the one that stands — which is what makes a
    replayed compaction cheap rather than a repository-sized re-upload.
    """
    any_backend.put_immutable('p/one.pack', b'0123456789')
    any_backend.put_immutable('p/one.pack', b'not the same bytes')

    assert any_backend.get_immutable('p/one.pack') == b'0123456789'
    (info,) = list(any_backend.list_immutable('p/'))
    assert info.key == 'p/one.pack'
    assert info.size == 10


def test_a_backend_has_no_way_to_delete_an_immutable_object() -> None:
    """Nothing in a sheaf store is ever removed, and the seam says so by having no verb for it."""
    assert not any(name.startswith('delete') for name in dir(sheaf.Backend))
