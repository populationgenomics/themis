"""Concurrent-modification behaviour of the GCS working-document write.

The store is the version authority: ``GcsStorage`` writes each version create-only
(``if_generation_match=0``) over the store-assigned successor of the latest key and re-lists on a
``PreconditionFailed``, so two racing writers cannot both win nor reorder history. The in-memory
``FixtureStorage`` cannot exercise this — its ``put`` has no ``await`` point, so concurrent asyncio
tasks never interleave inside it — so the race is driven here against a fake bucket that enforces
the generation precondition and can present a stale first listing (a writer that committed after
our list but before our upload).
"""

from __future__ import annotations

import asyncio

import pytest
from google.api_core import exceptions

from themis.services.store import gcs as gcs_mod
from themis.services.store import storage as storage_mod


class _FakeBlob:
    def __init__(self, blobs: dict[str, str], name: str, *, always_contended: bool) -> None:
        self._blobs = blobs
        self.name = name
        self._always_contended = always_contended

    def upload_from_string(self, data: str, *, if_generation_match: int, **_: object) -> None:
        # if_generation_match=0 is a create-only precondition: it fails if the object already exists.
        if if_generation_match == 0 and (self._always_contended or self.name in self._blobs):
            raise exceptions.PreconditionFailed(f'{self.name} already exists')
        self._blobs[self.name] = data


class _FakeBucket:
    def __init__(self, *, always_contended: bool) -> None:
        self.blobs: dict[str, str] = {}
        self._always_contended = always_contended

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self.blobs, name, always_contended=self._always_contended)


class _FakeClient:
    """A minimal GCS client over in-memory buckets.

    ``hide_on_first_list`` is the set of keys omitted from only the *first* ``list_blobs`` — modelling
    a writer whose commit lands after our list but before our upload. ``always_contended`` makes every
    create-only upload fail, modelling unending contention.
    """

    def __init__(self, *, hide_on_first_list: frozenset[str] = frozenset(), always_contended: bool = False) -> None:
        self._buckets: dict[str, _FakeBucket] = {}
        self._hide_on_first_list = hide_on_first_list
        self._always_contended = always_contended
        self._listed = False

    def bucket(self, name: str) -> _FakeBucket:
        return self._buckets.setdefault(name, _FakeBucket(always_contended=self._always_contended))

    def list_blobs(self, bucket: _FakeBucket, *, prefix: str) -> list[_FakeBlob]:
        hidden = self._hide_on_first_list if not self._listed else frozenset()
        self._listed = True
        return [bucket.blob(name) for name in sorted(bucket.blobs) if name.startswith(prefix) and name not in hidden]


def test_racing_writer_loses_the_precondition_then_mints_the_successor(monkeypatch: pytest.MonkeyPatch) -> None:
    v2_key = storage_mod.version_key('ana', 2)
    # A concurrent writer already committed v2, but our first listing does not yet see it.
    client = _FakeClient(hide_on_first_list=frozenset({v2_key}))
    documents = client.bucket('wd').blobs
    documents[storage_mod.version_key('ana', 1)] = 'v1'
    documents[v2_key] = 'theirs'
    monkeypatch.setattr(gcs_mod.gcs, 'Client', lambda: client)

    store = gcs_mod.GcsStorage(working_document_bucket='wd', workspace_bucket='ws')
    version = asyncio.run(store.put_working_document('ana', 'mine'))

    # We computed v2 off the stale list, lost the create-only race, re-listed, and minted v3 —
    # advancing the sequence rather than overwriting the racer's version or duplicating a number.
    assert version == 3
    assert documents[v2_key] == 'theirs'
    assert documents[storage_mod.version_key('ana', 3)] == 'mine'


def test_unending_contention_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every create-only write loses: a pathological, unending race must fail loudly, not spin forever.
    client = _FakeClient(always_contended=True)
    monkeypatch.setattr(gcs_mod.gcs, 'Client', lambda: client)

    store = gcs_mod.GcsStorage(working_document_bucket='wd', workspace_bucket='ws')
    with pytest.raises(RuntimeError, match='ana'):
        asyncio.run(store.put_working_document('ana', 'mine'))
