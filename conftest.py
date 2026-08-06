"""Shared pytest fixtures for container-backed tests.

One home for the Docker probe and the fake-gcs-server wiring so tests across themis don't
each re-derive the daemon check or stand up their own emulator. Testcontainers is imported
lazily inside the fixture that needs it, so this module stays cheap for tests that don't.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator

import pytest
import requests
from google.auth import credentials
from google.cloud import storage

_READY_TIMEOUT_S = 30


@functools.cache
def _docker_is_responsive() -> bool:
    """Whether a Docker daemon is reachable now — not just the CLI on PATH.

    Docker Desktop's Resource Saver pauses the VM when idle, so a call against it blocks
    while the VM wakes. Probe with a short, bounded ``docker info``: a daemon that answers
    promptly (CI) runs the tests; an absent, down, or asleep daemon (a dev machine) skips
    them instead of hanging. Cached so the probe runs once per session.
    """
    docker = shutil.which('docker')
    if docker is None:
        return False
    try:
        result = subprocess.run([docker, 'info'], capture_output=True, timeout=10, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@pytest.fixture(scope='session')
def docker_daemon() -> None:
    """Skip a Docker-backed test unless a daemon is reachable. The single gate for all of themis.

    Session-scoped so wider container fixtures can depend on it; a function-scoped test can
    still request it (narrower may depend on wider).
    """
    if not _docker_is_responsive():
        pytest.skip('needs a reachable Docker daemon')


def _set_external_url(endpoint: str) -> None:
    """Point the emulator at `endpoint`, once it is reachable there.

    fake-gcs-server builds object mediaLinks from ``-public-host``, which can't know the
    testcontainers-mapped port at start; the resolved endpoint makes a download from a listed
    blob (which carries a mediaLink) reach the mapped port, not :4443.

    Args:
        endpoint: The emulator's GCS JSON-API URL on the host.

    Raises:
        TimeoutError: If the endpoint never accepts a connection.
    """
    # `server started` in the container log precedes the host-side port forward on some Docker
    # backends, so the first request can be refused before the emulator is reachable.
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while True:
        try:
            requests.put(f'{endpoint}/_internal/config', json={'externalUrl': endpoint}, timeout=10).raise_for_status()
            return
        except requests.exceptions.ConnectionError as e:
            if time.monotonic() >= deadline:
                raise TimeoutError(f'fake-gcs-server never accepted a connection at {endpoint}') from e
            time.sleep(0.2)


@pytest.fixture(scope='session')
def gcs_endpoint(docker_daemon: None) -> Iterator[str]:
    """A session-lived ``fake-gcs-server``; yields its GCS JSON-API endpoint URL."""
    del docker_daemon  # depended on to gate on Docker; value unused

    import testcontainers.core.container  # noqa: PLC0415
    import testcontainers.core.waiting_utils  # noqa: PLC0415

    container = (
        testcontainers.core.container.DockerContainer('fsouza/fake-gcs-server:1.52')
        .with_command('-scheme http -public-host 0.0.0.0')
        .with_exposed_ports(4443)
    )
    with container:
        testcontainers.core.waiting_utils.wait_for_logs(container, 'server started')
        endpoint = f'http://{container.get_container_host_ip()}:{container.get_exposed_port(4443)}'
        _set_external_url(endpoint)
        yield endpoint


@pytest.fixture(scope='session')
def gcs_client(gcs_endpoint: str) -> storage.Client:
    """A ``storage.Client`` pointed at the emulator (anonymous credentials)."""
    return storage.Client(
        project='themis-test',
        credentials=credentials.AnonymousCredentials(),
        client_options={'api_endpoint': gcs_endpoint},
    )


@pytest.fixture
def gcs_bucket(gcs_client: storage.Client) -> storage.Bucket:
    """A fresh, uniquely-named bucket per test — isolation without a per-test container."""
    bucket = gcs_client.bucket(f'themis-test-{uuid.uuid4().hex}')
    bucket.create()
    return bucket
