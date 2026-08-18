"""The platform gate for the sandbox_worker tests that launch a real bubblewrap guest."""

from __future__ import annotations

import os

import postern
import pytest


@pytest.fixture(scope='session')
def bubblewrap() -> None:
    """Gate a bwrap-backed test on a launchable sandbox: skipped off-platform, an error under CI.

    A skip in CI is indistinguishable from a pass, and the job that installs bubblewrap is the only
    place these run. GitHub Actions sets ``CI``, so one fixture serves both: skip where bwrap cannot
    exist, fail where it was meant to.

    Raises:
        RuntimeError: If bubblewrap is absent under CI.
    """
    if postern.available():
        return
    if os.environ.get('CI'):
        raise RuntimeError('bubblewrap is not on PATH: the sandbox integration tests cannot run')
    pytest.skip('needs Linux + bubblewrap')
