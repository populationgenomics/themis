"""Tests for tools.resources.mirror: what is at a destination is skipped, adopted or refused, never replaced."""

from __future__ import annotations

import hashlib
import json

import pytest
from google.cloud import storage

from tests.tools import conftest
from tools.resources import manifest, mirror

_BYTES = b'publisher bytes'


def _pinned_entry(pinned: manifest.Manifest) -> manifest.Entry:
    entry = pinned.table('hgnc-complete-set')
    return manifest.Entry(**{**entry.__dict__, 'md5': hashlib.md5(_BYTES).hexdigest()})  # noqa: S324


def _mirror(entry: manifest.Entry, pinned: manifest.Manifest, bucket: storage.Bucket) -> mirror.Outcome:
    return mirror.mirror(entry, dataset=pinned.dataset, bucket=bucket, client=bucket.client, dry_run=False)


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
