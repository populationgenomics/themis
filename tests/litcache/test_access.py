"""Tests for the flat ``Access`` boundary validator (``themis.litcache.access``)."""

from __future__ import annotations

import pytest

from themis.litcache import access
from themis.litcache.models import litcache_pb2


@pytest.mark.parametrize(
    'value',
    ['free-to-read', 'licensed', 'institution-captured', 'unknown'],
)
def test_valid_access_passes(value: str) -> None:
    publisher = 'Elsevier' if value == 'licensed' else None
    msg = litcache_pb2.Access(access=value, publisher=publisher)
    access.validate_access(msg)  # does not raise


def test_licensed_without_publisher_fails() -> None:
    with pytest.raises(ValueError, match='publisher must be set iff'):
        access.validate_access(litcache_pb2.Access(access='licensed'))


def test_licensed_with_empty_publisher_fails() -> None:
    # An empty-string publisher is not a publisher: an optional string reads as '' when unset.
    with pytest.raises(ValueError, match='publisher must be set iff'):
        access.validate_access(litcache_pb2.Access(access='licensed', publisher=''))


def test_non_licensed_with_empty_publisher_passes() -> None:
    access.validate_access(litcache_pb2.Access(access='free-to-read', publisher=''))  # '' == absent


def test_publisher_without_licensed_fails() -> None:
    with pytest.raises(ValueError, match='publisher must be set iff'):
        access.validate_access(litcache_pb2.Access(access='free-to-read', publisher='Elsevier'))


def test_unknown_access_value_fails() -> None:
    with pytest.raises(ValueError, match='unknown access value'):
        access.validate_access(litcache_pb2.Access(access='open-sesame'))
