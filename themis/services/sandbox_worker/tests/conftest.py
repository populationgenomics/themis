"""Shared fixtures for the sandbox_worker tests: the bubblewrap platform gate, and the guest channel's reset."""

from __future__ import annotations

import os
from collections.abc import Iterator

import postern
import pytest

from themis.services.sandbox_worker.guest import channel


@pytest.fixture(autouse=True)
def _fresh_hatch_channel() -> Iterator[None]:
    """The guest channel is memoised per process, so it has to be dropped around every test that dials one."""
    channel.to_hatch.cache_clear()
    yield
    channel.to_hatch.cache_clear()


@pytest.fixture(scope='session')
def bubblewrap() -> None:
    """Gate a bwrap-backed test on a launchable sandbox: skipped off-platform, an error under CI.

    A skip in CI is indistinguishable from a pass, and the job that installs bubblewrap is the only
    place these run. GitHub Actions sets ``CI``, so one fixture serves both: skip where bwrap cannot
    exist, fail where it was meant to.

    To run the gated tests from a machine without bubblewrap (macOS, say), give them a privileged
    Linux container — bubblewrap needs unprivileged user namespaces, which some kernels restrict::

        docker run --rm -it --privileged -v "$PWD":/w -w /w python:3.13-slim bash -c '
          apt-get update && apt-get install -y bubblewrap && pip install uv &&
          sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 || true &&
          uv run --group test pytest themis/services/sandbox_worker/tests'

    Raises:
        RuntimeError: If bubblewrap is absent under CI.
    """
    if postern.available():
        return
    if os.environ.get('CI'):
        raise RuntimeError('bubblewrap is not on PATH: the sandbox integration tests cannot run')
    pytest.skip('needs Linux + bubblewrap')
