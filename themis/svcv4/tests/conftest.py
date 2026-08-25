"""Shared fixtures for the svcv4 tests."""

from __future__ import annotations

import pytest

from themis.svcv4 import reference


@pytest.fixture(scope='session')
def ref() -> reference.Reference:
    """The packaged SVCv4 reference, loaded once per test session."""
    return reference.load_reference()
