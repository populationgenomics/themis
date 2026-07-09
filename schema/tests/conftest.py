"""Shared fixtures for the schema tests."""

from __future__ import annotations

import functools
import shutil
import subprocess

import pytest


@functools.cache
def _docker_is_responsive() -> bool:
    """Whether a Docker daemon is reachable right now (not just the CLI present).

    Tests that shell out to ``docker run`` need this. A ``docker`` binary on PATH
    does not mean the daemon is up: Docker Desktop's Resource Saver pauses the VM
    when idle, so a call against it blocks while the VM wakes. Probe with a short,
    bounded ``docker info`` — a daemon that answers promptly (CI) runs the tests;
    an absent, down, or asleep daemon (a dev machine) skips them instead of
    hanging. Cached so the probe runs once per session.
    """
    docker = shutil.which('docker')
    if docker is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603
            [docker, 'info'], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@pytest.fixture
def docker_daemon() -> None:
    """Skip the requesting test unless a Docker daemon is reachable.

    The probe runs at test setup, not at import, so its cost is paid only by the
    tests that request it, and only when they actually run.
    """
    if not _docker_is_responsive():
        pytest.skip('this test needs a reachable Docker daemon')
