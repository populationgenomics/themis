"""Shared fixtures for the schema test suite."""

from __future__ import annotations

import os
import shutil

import pytest


@pytest.fixture(scope='session')
def buf_cli() -> None:
    """Gate a buf-backed test on the ``buf`` CLI: skipped locally, an error under CI.

    Raises:
        RuntimeError: If ``buf`` is absent under CI.
    """
    if shutil.which('buf') is not None:
        return
    if os.environ.get('CI', '').lower() not in {'', '0', 'false'}:
        raise RuntimeError('buf is not on PATH: the schema tests cannot run')
    pytest.skip('buf not on PATH')
