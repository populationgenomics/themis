"""Tests for the shared GCS blob helpers (``themis.storage``) against ``fake-gcs-server``."""

from __future__ import annotations

import hashlib

import pytest
from google.cloud import storage as gcs

from themis import storage


def test_put_is_content_addressed(gcs_bucket: gcs.Bucket) -> None:
    name = storage.put_content_addressed(gcs_bucket, b'hello', 'renderings', 'txt')
    assert name == f'renderings/{hashlib.sha256(b"hello").hexdigest()}.txt'
    assert gcs_bucket.blob(name).download_as_bytes() == b'hello'


def test_put_is_idempotent(gcs_bucket: gcs.Bucket) -> None:
    first = storage.put_content_addressed(gcs_bucket, b'x', 'sources', 'pdf')
    second = storage.put_content_addressed(gcs_bucket, b'x', 'sources', 'pdf')
    assert first == second
    assert len(list(gcs_bucket.list_blobs(prefix='sources/'))) == 1  # no duplicate write


def test_leading_dot_in_ext_normalized(gcs_bucket: gcs.Bucket) -> None:
    name = storage.put_content_addressed(gcs_bucket, b'y', 'figures', '.jpg')
    assert name.endswith('.jpg')
    assert not name.endswith('..jpg')


def test_rmw_applies_the_mutation(gcs_bucket: gcs.Bucket) -> None:
    gcs_bucket.blob('manifest.pb').upload_from_string(b'v1')
    storage.read_modify_write(gcs_bucket, 'manifest.pb', lambda d: d + b'+2')
    assert gcs_bucket.blob('manifest.pb').download_as_bytes() == b'v1+2'


def test_rmw_retries_on_a_concurrent_write(gcs_bucket: gcs.Bucket) -> None:
    gcs_bucket.blob('manifest.pb').upload_from_string(b'0')
    calls: list[bytes] = []

    def mutate(data: bytes) -> bytes:
        calls.append(data)
        if len(calls) == 1:
            gcs_bucket.blob('manifest.pb').upload_from_string(b'concurrent')  # bump generation mid-RMW
        return data + b'!'

    storage.read_modify_write(gcs_bucket, 'manifest.pb', mutate)
    assert len(calls) == 2  # first write-back was stale; re-read and retried
    assert gcs_bucket.blob('manifest.pb').download_as_bytes() == b'concurrent!'


def test_rmw_raises_after_exhausting_attempts(gcs_bucket: gcs.Bucket) -> None:
    gcs_bucket.blob('manifest.pb').upload_from_string(b'0')

    def always_conflict(data: bytes) -> bytes:
        gcs_bucket.blob('manifest.pb').upload_from_string(b'again')  # every attempt is pre-empted
        return data + b'!'

    with pytest.raises(storage.StaleGenerationError):
        storage.read_modify_write(gcs_bucket, 'manifest.pb', always_conflict, attempts=2)
