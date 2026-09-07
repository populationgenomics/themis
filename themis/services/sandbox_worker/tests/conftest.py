"""Shared fixtures for the sandbox_worker tests.

The bubblewrap platform gates, the guest channel's reset, and an Analysis repository served over real hatches — the
mirror running, as the worker's does, over a `RemoteStore` to the sheaf servicer, here in-process over a local
backend (the harness `themis/clients/sheaf/tests/conftest.py` holds).
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import tempfile
from collections.abc import Iterator

import postern
import pytest
from postern import stream

from themis import sheaf
from themis.clients.auth.tests import fixture_session
from themis.clients.sheaf import store as remote_mod
from themis.clients.sheaf.tests import conftest as remote_conftest
from themis.services.sandbox_worker import git_hatches
from themis.services.sandbox_worker.guest import channel
from themis.sheaf.wire import bare


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """A `sandbox_root` test is a `sandbox` test too, so the two CI marker expressions stay a partition."""
    for item in items:
        if item.get_closest_marker('sandbox_root') is not None:
            item.add_marker(pytest.mark.sandbox)


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


@pytest.fixture(scope='session')
def bubblewrap_root(bubblewrap: None) -> None:
    """Gate a test of the host uid mapping: bwrap drops to ``host_uid`` only from a root worker.

    The ``pytest-sandbox`` CI job runs the ``sandbox_root`` tests under ``sudo``; anywhere else they
    skip, and under CI without root they error, as `bubblewrap` does without bwrap.

    Raises:
        RuntimeError: If the process is not root under CI.
    """
    del bubblewrap
    if os.geteuid() == 0:
        return
    if os.environ.get('CI'):
        raise RuntimeError('not root: the host uid mapping tests must run under sudo')
    pytest.skip('needs root, to drop bwrap to a dedicated uid')


@dataclasses.dataclass(frozen=True)
class Analysis:
    """One Analysis repository and the two hatches serving it.

    `store` is the in-process store over the same backend — the oracle a test reads the truth from; `remote`
    is what the mirror runs over.
    """

    backend: sheaf.LocalBackend
    store: sheaf.Store
    remote: remote_mod.RemoteStore
    hatches: git_hatches.GitHatches

    @property
    def upload_pack(self) -> stream.StreamHatch:
        """The fetch hatch."""
        return self.hatches.hatches[0]

    @property
    def receive_pack(self) -> stream.StreamHatch:
        """The push hatch."""
        return self.hatches.hatches[1]


@pytest.fixture
def analysis(tmp_path: pathlib.Path) -> Iterator[Analysis]:
    """An empty Analysis repository behind the sheaf servicer, mirrored over a `RemoteStore`, on started hatches.

    The token file sits beside the mirror as the worker's does; the hook rebuilds the remote from the
    descriptor in the sync state and reads the same file. The sockets live under their own short temp
    dir: a Unix socket path has to fit ``sun_path``, which pytest's own temp paths do not leave room for.
    """
    backend = sheaf.LocalBackend(tmp_path / 'store')
    mirror_root = tmp_path / 'mirror'
    mirror_root.mkdir()
    socket_dir = pathlib.Path(tempfile.mkdtemp(prefix='hatch-'))
    with (
        remote_conftest.serving(backend) as target,
        remote_mod.RemoteStore(
            target, remote_conftest.write_token_file(mirror_root / 'token.json', fixture_session.GOOD_TOKEN), repo='ws'
        ) as remote,
    ):
        hatches = git_hatches.GitHatches(
            bare.BareRepo(remote, mirror_root / 'mirror.git'), protection=git_hatches.PROTECTION, socket_dir=socket_dir
        )
        for hatch in hatches.hatches:
            hatch.start()
        try:
            yield Analysis(backend, remote_conftest.direct(backend), remote, hatches)
        finally:
            hatches.close()
            shutil.rmtree(socket_dir, ignore_errors=True)
