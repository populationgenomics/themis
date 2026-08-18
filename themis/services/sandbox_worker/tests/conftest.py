"""The platform gate for the sandbox_worker tests that launch a real bubblewrap guest."""

from __future__ import annotations

import os

import postern
import pytest


@pytest.fixture(scope='session')
def bubblewrap() -> None:
    """Gate a bwrap-backed test on a launchable sandbox: skipped off-platform, an error under CI.

    Every developer machine here is macOS, so CI is the only place these tests ever run and a skip
    there is indistinguishable from a pass. GitHub Actions sets ``CI``, so the one fixture both skips
    off-platform and fails the job that is meant to have installed bubblewrap.

    Raises:
        RuntimeError: If bubblewrap is absent under CI.
    """
    if postern.available():
        return
    if os.environ.get('CI'):
        raise RuntimeError('bubblewrap is not on PATH: the sandbox integration tests cannot run')
    pytest.skip('needs Linux + bubblewrap')
