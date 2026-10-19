"""The host uid mapping: bwrap dropped to a dedicated uid, from a root worker, on a bubblewrap host.

`SandboxProfile.host_uid` is the structural half of the guard that keeps the worker's git out of `/workspace`
(sandbox-worker.md §"The guest runs as a uid that owns nothing"): every file the guest creates is owned by a uid
that owns nothing else on the host, so a host-side `git` in the working tree fails git's ownership check. The file
tools run in the guest as that uid too, so what they make or rewrite stays the guest's, and the tests here hold both
directions. Root is needed because bwrap drops only from a root worker; the `pytest-sandbox` CI job runs these under
`sudo`.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator

import postern
import pytest
from anthropic.lib import tools
from postern import stream

from themis import sheaf
from themis.services.sandbox_worker import file_tools, git_hatches
from themis.services.sandbox_worker.guest import file_tool
from themis.sheaf.wire import bare

pytestmark = [pytest.mark.sandbox_root, pytest.mark.usefixtures('bubblewrap_root')]

# A uid and gid that own nothing on any host this runs on; the deploy chooses its own pair.
HOST_UID = 61000
HOST_GID = 61000
GUEST_GIT = [
    'git',
    '-c',
    'protocol.ext.allow=always',
    '-c',
    f'user.name={git_hatches.AGENT_IDENTITY.name}',
    '-c',
    f'user.email={git_hatches.AGENT_IDENTITY.email}',
]


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


@pytest.fixture
def runner(workspace: pathlib.Path, root: pathlib.Path) -> Iterator[file_tools.GuestRunner]:
    """Runs a file tool as the session does under the mapping: the entry point, in a guest launch as `HOST_UID`.

    The guest here has the host's userland rather than the guest rootfs, so the entry point runs on this test's own
    interpreter and virtualenv, bound read-only at their own paths.
    """
    requests = root / 'requests'
    requests.mkdir(mode=0o711)
    entry_point = pathlib.Path(file_tool.__file__)
    # uv links a venv's interpreter through a version-alias directory, a symlink beside `sys.base_prefix`.
    linked = pathlib.Path(sys.prefix, 'bin', os.readlink(sys.executable)).parent.parent
    interpreter = {sys.prefix, sys.base_prefix, str(linked), str(entry_point.parent)}
    binds = [(path, path) for path in sorted(interpreter)]
    profile = postern.SandboxProfile(
        workspace=workspace,
        host_uid=HOST_UID,
        host_gid=HOST_GID,
        required_ro_binds=[*binds, (str(requests), file_tools.GUEST_REQUESTS)],
    )
    sandbox = postern.Sandbox(profile)
    try:
        yield file_tools.GuestRunner(sandbox, requests, (sys.executable, '-I', str(entry_point)), 60)
    finally:
        sandbox.close()


def _call(runner: file_tools.GuestRunner, name: str, tool_input: dict[str, str]) -> object:
    return asyncio.run(runner.call(name, tool_input))


@pytest.mark.parametrize(
    ('name', 'tool_input'),
    [
        ('write', {'file_path': 'guest.txt', 'content': 'from the file tool'}),
        ('edit', {'file_path': 'guest.txt', 'old_string': 'from the guest', 'new_string': 'from the file tool'}),
    ],
)
def test_a_guest_file_the_file_tools_rewrote_stays_the_guests(
    workspace: pathlib.Path, runner: file_tools.GuestRunner, name: str, tool_input: dict[str, str]
) -> None:
    """The case a root worker failed: `protected_regular` refuses root's write to the guest's file in /workspace."""
    sandbox = postern.Sandbox(_profile(workspace))
    created = sandbox.run_python("open('guest.txt', 'w').write('from the guest')")
    assert created.ok, created.stderr

    _call(runner, name, tool_input)
    changed = sandbox.run_python(
        "import os\nopen('guest.txt', 'a').write(' and the guest')\nprint(open('guest.txt').read())\n"
        "os.unlink('guest.txt')"
    )

    assert changed.ok, changed.stderr
    assert changed.stdout.strip() == 'from the file tool and the guest'


def test_what_the_file_tools_make_is_the_guests(workspace: pathlib.Path, runner: file_tools.GuestRunner) -> None:
    _call(runner, 'write', {'file_path': 'a/b/note.md', 'content': 'from the file tool'})

    for path in ('a', 'a/b', 'a/b/note.md'):
        made = (workspace / path).stat()
        assert (made.st_uid, made.st_gid) == (HOST_UID, HOST_GID), path
    # A checkout replaces a file by unlinking it and creating another.
    replaced = postern.Sandbox(_profile(workspace)).run_python(
        "import os\nos.unlink('a/b/note.md')\nopen('a/b/note.md', 'w').write('replaced')"
    )
    assert replaced.ok, replaced.stderr


def test_a_file_tool_reads_what_the_guest_wrote(workspace: pathlib.Path, runner: file_tools.GuestRunner) -> None:
    created = postern.Sandbox(_profile(workspace)).run_python("open('guest.txt', 'w').write('from the guest')")
    assert created.ok, created.stderr

    assert 'from the guest' in str(_call(runner, 'read', {'file_path': 'guest.txt'}))


def test_skills_opened_to_reading_are_readable_by_the_guest_and_still_the_workers(
    workspace: pathlib.Path, runner: file_tools.GuestRunner
) -> None:
    skill = workspace / 'skills' / 'one'
    skill.mkdir(parents=True, mode=0o700)  # as the SDK downloads it: owner-only
    (workspace / 'skills').chmod(0o700)
    (skill / 'SKILL.md').write_text('the skill')
    (skill / 'SKILL.md').chmod(0o600)

    file_tools.open_to_reading(workspace / 'skills')
    sandbox = postern.Sandbox(_profile(workspace))
    read = sandbox.run_python("print(open('skills/one/SKILL.md').read())")
    overwritten = sandbox.run_python("open('skills/one/SKILL.md', 'w').write('changed')")

    assert read.ok, read.stderr
    assert read.stdout.strip() == 'the skill'
    assert not overwritten.ok
    assert 'the skill' in str(_call(runner, 'read', {'file_path': 'skills/one/SKILL.md'}))
    with pytest.raises(tools.ToolError, match='permission denied'):
        _call(runner, 'write', {'file_path': 'skills/one/SKILL.md', 'content': 'changed'})
    assert (skill / 'SKILL.md').read_text() == 'the skill'


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
