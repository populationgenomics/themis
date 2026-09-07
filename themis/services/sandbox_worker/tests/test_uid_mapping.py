"""The host uid mapping: bwrap dropped to a dedicated uid, from a root worker, on a bubblewrap host.

`SandboxProfile.host_uid` is the structural half of the guard that keeps the worker's git out of `/workspace`
(sheaf-changeover.md, step 1): every file the guest creates is owned by a uid that owns nothing else on the host, so
a host-side `git` in the working tree fails git's ownership check. Its cost is the other direction — a file the
worker writes, as the SDK's file tools do, is not the guest's to change in place — and that cost is measured here
rather than argued about. Root is needed because bwrap drops only from a root worker; the `pytest-sandbox` CI job
runs these under `sudo`.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
from collections.abc import Iterator

import postern
import pytest
from postern import stream

from themis import sheaf
from themis.services.sandbox_worker import git_hatches
from themis.sheaf.wire import bare

pytestmark = [pytest.mark.sandbox_root, pytest.mark.usefixtures('bubblewrap_root')]

# A uid and gid that own nothing on any host this runs on; the deploy chooses its own pair.
HOST_UID = 61000
HOST_GID = 61000
GUEST_GIT = ['git', '-c', 'protocol.ext.allow=always', '-c', 'user.name=agent', '-c', 'user.email=agent@localhost']


@pytest.fixture
def root() -> Iterator[pathlib.Path]:
    """A parent every bind source can live under: bwrap runs as `HOST_UID` and has to traverse it."""
    path = pathlib.Path(tempfile.mkdtemp(prefix='uid-'))
    path.chmod(0o755)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def workspace(root: pathlib.Path) -> pathlib.Path:
    path = root / 'workspace'
    path.mkdir()
    return path


def _profile(workspace: pathlib.Path) -> postern.SandboxProfile:
    return postern.SandboxProfile(workspace=workspace, host_uid=HOST_UID, host_gid=HOST_GID)


def _host_git(*args: str, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    env = {'PATH': os.environ['PATH'], 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}
    # The host's own git, run to prove the working tree refuses it.
    argv = ['git', *args]
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)  # noqa: S603


def test_a_file_the_guest_creates_is_owned_by_the_dedicated_uid(workspace: pathlib.Path) -> None:
    result = postern.Sandbox(_profile(workspace)).run_python("open('created.txt', 'w').write('from the guest')")

    assert result.ok, result.stderr
    created = (workspace / 'created.txt').stat()
    assert (created.st_uid, created.st_gid) == (HOST_UID, HOST_GID)
    assert created.st_uid != os.geteuid()


def test_a_repository_the_guest_cloned_over_the_hatch_refuses_the_hosts_git(
    workspace: pathlib.Path, root: pathlib.Path
) -> None:
    """The production path under the mapping: clone, commit and push from the dropped guest, then try from the host."""
    store = sheaf.Store(sheaf.LocalBackend(root / 'store'), 'analysis-1')
    socket_dir = root / 'sockets'
    socket_dir.mkdir(mode=0o711)
    hatches = git_hatches.GitHatches(
        bare.BareRepo(store, root / 'mirror.git'), protection=git_hatches.PROTECTION, socket_dir=socket_dir
    )
    profile = _profile(workspace)
    sandbox = postern.Sandbox(profile, hatch=hatches.hatches)
    fetch_url, push_url = hatches.guest_urls(profile)
    try:
        for argv in (
            [*GUEST_GIT, 'clone', fetch_url, '.'],
            [*GUEST_GIT, 'remote', 'set-url', '--push', 'origin', push_url],
            [*GUEST_GIT, 'commit', '-q', '--allow-empty', '-m', 'from the guest'],
            [*GUEST_GIT, 'push', '-q', 'origin', 'HEAD'],
        ):
            result = sandbox.run(argv, timeout=120)
            assert result.ok, f'{argv[-2:]}: {result.stderr}'
    finally:
        hatches.close()

    assert 'refs/heads/main' in store.read().refs
    assert (workspace / '.git').stat().st_uid == HOST_UID
    # git's ownership check does not exempt root, so an accidental host-side git in /workspace fails here.
    status = _host_git('status', cwd=workspace)
    assert status.returncode != 0
    assert 'dubious ownership' in status.stderr


def test_a_file_the_worker_wrote_is_readable_by_the_guest(workspace: pathlib.Path) -> None:
    (workspace / 'from_host.txt').write_text('written by the worker')  # as the SDK's write tool does: 0644

    result = postern.Sandbox(_profile(workspace)).run_python("print(open('from_host.txt').read())")

    assert result.ok, result.stderr
    assert result.stdout.strip() == 'written by the worker'


@pytest.mark.xfail(
    strict=True,
    reason='a file the worker writes is root-owned and 0644, and the sticky workspace stops the guest replacing it; '
    'this is the cost of the mapping the plan names, and the reason the deploy leaves it off',
)
def test_a_file_the_worker_wrote_can_be_modified_by_the_guest(workspace: pathlib.Path) -> None:
    (workspace / 'from_host.txt').write_text('written by the worker')

    appended = postern.Sandbox(_profile(workspace)).run_python("open('from_host.txt', 'a').write(' and the guest')")

    assert appended.ok, appended.stderr
    assert (workspace / 'from_host.txt').read_text() == 'written by the worker and the guest'


def test_a_hatch_bound_under_a_directory_the_dedicated_uid_can_traverse_is_reachable(
    workspace: pathlib.Path, root: pathlib.Path
) -> None:
    """Binding a socket into the guest means bwrap opens it: the directory must let `HOST_UID` through, not own it."""
    socket_dir = root / 'sockets'
    socket_dir.mkdir(mode=0o711)
    hatch = stream.StreamHatch(stream.splice_subprocess(['cat']), name='echo', socket_path=socket_dir / 'echo.sock')
    code = (
        'import os, socket\n'
        's = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n'
        "s.connect(os.environ['POSTERN_HATCH_ECHO'])\n"
        "s.sendall(b'ping')\n"
        's.shutdown(socket.SHUT_WR)\n'
        'print(s.recv(16).decode())\n'
    )
    try:
        result = postern.Sandbox(_profile(workspace), hatch=hatch).run_python(code, timeout=30)
    finally:
        hatch.close()

    assert result.ok, result.stderr
    assert result.stdout.strip() == 'ping'
