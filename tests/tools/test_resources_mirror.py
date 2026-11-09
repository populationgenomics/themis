"""Tests for tools.resources.mirror: what is at a destination is skipped, adopted or refused, never replaced."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from typing import Self

import pytest
from google.cloud import storage

from tests.tools import conftest
from tools.resources import manifest, mirror

_BYTES = b'publisher bytes'


def _pinned_entry(pinned: manifest.Manifest) -> manifest.Entry:
    entry = pinned.table('hgnc-complete-set')
    return manifest.Entry(**{**entry.__dict__, 'md5': hashlib.md5(_BYTES).hexdigest()})  # noqa: S324


def _mirror(entry: manifest.Entry, pinned: manifest.Manifest, bucket: storage.Bucket) -> mirror.Outcome:
    return mirror.mirror(
        entry, dataset=pinned.dataset, bucket=bucket, client=bucket.client, dry_run=False, abandoned=threading.Event()
    )


def test_an_object_mirrored_as_pinned_is_skipped(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored
) -> None:
    entry = pinned.table('hgnc-complete-set')
    put_mirrored(entry, _BYTES)
    assert _mirror(entry, pinned, gcs_bucket).status is mirror.Status.SKIPPED


def test_an_object_whose_sidecar_never_landed_is_adopted_when_its_md5_is_the_publishers(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest
) -> None:
    entry = _pinned_entry(pinned)
    gcs_bucket.blob(f'{pinned.dataset}/{entry.object}').upload_from_string(_BYTES)
    assert _mirror(entry, pinned, gcs_bucket).status is mirror.Status.ADOPTED
    assert mirror.mirrored(entry, dataset=pinned.dataset, bucket=gcs_bucket).digest == entry.md5


def test_an_object_without_a_sidecar_and_no_publisher_digest_is_refused_not_replaced(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest
) -> None:
    entry = pinned.table('hgnc-complete-set')
    blob = gcs_bucket.blob(f'{pinned.dataset}/{entry.object}')
    blob.upload_from_string(b'whatever a stopped run left')
    blob.reload()
    with pytest.raises(mirror.MirrorError, match='never replaces an object'):
        _mirror(entry, pinned, gcs_bucket)
    blob.reload()
    assert blob.download_as_bytes() == b'whatever a stopped run left'


def test_an_object_mirrored_from_a_url_no_longer_pinned_is_refused(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, put_mirrored: conftest.PutMirrored
) -> None:
    entry = pinned.table('hgnc-complete-set')
    put_mirrored(entry, _BYTES)
    sidecar = gcs_bucket.blob(f'{pinned.dataset}/{entry.object}{mirror.PROVENANCE_SUFFIX}')
    sidecar.upload_from_string(
        json.dumps({**json.loads(sidecar.download_as_bytes()), 'url': 'https://example.org/old'})
    )
    with pytest.raises(mirror.MirrorError, match='delete both'):
        _mirror(entry, pinned, gcs_bucket)


def _break(bucket: storage.Bucket, pinned: manifest.Manifest, entry: manifest.Entry) -> None:
    bucket.blob(f'{pinned.dataset}/{entry.object}{mirror.PROVENANCE_SUFFIX}').upload_from_string(
        json.dumps({'url': 'https://example.org/old'})
    )


def test_every_entry_is_reported_once_and_the_failures_come_back_in_manifest_order(
    gcs_bucket: storage.Bucket,
    pinned: manifest.Manifest,
    put_mirrored: conftest.PutMirrored,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = pinned.entries
    for entry in entries:
        put_mirrored(entry, entry.id.encode())
    broken = [entries[-1], entries[1]]
    for entry in broken:
        _break(gcs_bucket, pinned, entry)
    failed = mirror.mirror_all(
        entries, dataset=pinned.dataset, bucket=gcs_bucket, client=gcs_bucket.client, dry_run=True
    )
    assert failed == [entries[1].id, entries[-1].id]
    reported = [line.split()[-1] for line in capsys.readouterr().err.splitlines() if line.startswith('  skip')]
    assert sorted(reported) == sorted(entry.id for entry in entries if entry not in broken)


def test_entries_from_different_hosts_are_mirrored_at_once(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosts = [f'https://host-{n}.example.org/file' for n in range(4)]
    entries = [
        manifest.Entry(**{**entry.__dict__, 'url': url}) for entry, url in zip(pinned.entries, hosts, strict=False)
    ]
    together = threading.Barrier(len(entries), timeout=10)

    def meet(entry: manifest.Entry, **_: object) -> mirror.Outcome:
        together.wait()
        return mirror.Outcome(entry, mirror.Status.SKIPPED)

    monkeypatch.setattr(mirror, 'mirror', meet)
    assert (
        mirror.mirror_all(entries, dataset=pinned.dataset, bucket=gcs_bucket, client=gcs_bucket.client, dry_run=True)
        == []
    )


def test_one_host_has_at_most_its_slots_in_flight(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_flight = 0
    most = 0
    lock = threading.Lock()

    def count(entry: manifest.Entry, **_: object) -> mirror.Outcome:
        nonlocal in_flight, most
        with lock:
            in_flight += 1
            most = max(most, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return mirror.Outcome(entry, mirror.Status.SKIPPED)

    monkeypatch.setattr(mirror, 'mirror', count)
    entries = [
        manifest.Entry(**{**entry.__dict__, 'url': f'https://one.example.org/{entry.id}'}) for entry in pinned.entries
    ]
    mirror.mirror_all(entries, dataset=pinned.dataset, bucket=gcs_bucket, client=gcs_bucket.client, dry_run=True)
    assert most == mirror._PER_HOST


def test_an_error_no_entry_absorbs_abandons_the_pass_at_once_and_waits_out_the_writes_begun(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = pinned.entries[:2]
    seen: list[bool] = []
    reached = threading.Event()

    def run(entry: manifest.Entry, *, abandoned: threading.Event, **_: object) -> mirror.Outcome:
        if entry is first:  # first in the manifest: mid-write when a later entry raises
            reached.set()
            abandoned_in_time = abandoned.wait(timeout=10)
            time.sleep(0.2)  # the write already begun, finishing
            seen.append(abandoned_in_time)
            return mirror.Outcome(entry, mirror.Status.SKIPPED)
        reached.wait(timeout=10)
        raise RuntimeError(entry.id)

    monkeypatch.setattr(mirror, 'mirror', run)
    with pytest.raises(RuntimeError):
        mirror.mirror_all(
            [first, second], dataset=pinned.dataset, bucket=gcs_bucket, client=gcs_bucket.client, dry_run=True
        )
    assert seen == [True]


class _Stop(BaseException):
    """Stands in for the job's SIGTERM."""


def test_a_stopped_pass_does_not_wait_out_the_writes_begun(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = pinned.entries[0]
    reached, release = threading.Event(), threading.Event()
    seen: list[bool] = []

    def run(entry: manifest.Entry, *, abandoned: threading.Event, **_: object) -> mirror.Outcome:
        reached.set()
        release.wait(timeout=10)
        seen.append(abandoned.is_set())
        return mirror.Outcome(entry, mirror.Status.SKIPPED)

    def stop(*_: object, **__: object) -> None:
        reached.wait(timeout=10)
        raise _Stop

    monkeypatch.setattr(mirror, 'mirror', run)
    monkeypatch.setattr(mirror.concurrent.futures, 'wait', stop)
    try:
        with pytest.raises(_Stop):
            mirror.mirror_all(
                [entry], dataset=pinned.dataset, bucket=gcs_bucket, client=gcs_bucket.client, dry_run=True
            )
        assert seen == []  # returned with the write still in progress
    finally:
        release.set()
    deadline = time.monotonic() + 10
    while not seen and time.monotonic() < deadline:
        time.sleep(0.05)
    assert seen == [True]


class _Response:
    """A streamed response that abandons the pass once its first chunk is read."""

    def __init__(self, abandoned: threading.Event) -> None:
        self._abandoned = abandoned

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield b'x' * chunk_size
        self._abandoned.set()
        yield b'x' * chunk_size


def _https_entry(pinned: manifest.Manifest) -> manifest.Entry:
    entry = pinned.table('hgnc-complete-set')
    return manifest.Entry(**{**entry.__dict__, 'url': 'https://publisher.example.org/file', 'md5': None})


def test_an_abandoned_download_stops_between_chunks_and_writes_nothing(
    gcs_bucket: storage.Bucket, pinned: manifest.Manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    abandoned = threading.Event()
    monkeypatch.setattr(mirror.requests, 'get', lambda *_, **__: _Response(abandoned))
    entry = _https_entry(pinned)
    with pytest.raises(mirror.MirrorError, match='abandoned'):
        mirror.mirror(
            entry,
            dataset=pinned.dataset,
            bucket=gcs_bucket,
            client=gcs_bucket.client,
            dry_run=False,
            abandoned=abandoned,
        )
    assert not list(gcs_bucket.list_blobs(prefix=f'{pinned.dataset}/{entry.object}'))


def test_an_abandoned_copy_within_gcs_writes_nothing(gcs_bucket: storage.Bucket, pinned: manifest.Manifest) -> None:
    gcs_bucket.blob('upstream/file').upload_from_string(b'upstream bytes')
    entry = pinned.table('hgnc-complete-set')
    entry = manifest.Entry(**{**entry.__dict__, 'url': f'gs://{gcs_bucket.name}/upstream/file', 'md5': None})
    abandoned = threading.Event()
    abandoned.set()
    with pytest.raises(mirror.MirrorError, match='abandoned'):
        mirror.mirror(
            entry,
            dataset=pinned.dataset,
            bucket=gcs_bucket,
            client=gcs_bucket.client,
            dry_run=False,
            abandoned=abandoned,
        )
    assert not list(gcs_bucket.list_blobs(prefix=f'{pinned.dataset}/{entry.object}'))
