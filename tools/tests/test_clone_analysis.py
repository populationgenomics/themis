"""The one-shot clone: what it refuses, what the clone holds, and that the token never outlives it.

The session token and the remote are stood in for at the two tools' seams: `derive_session_token`
answers a fixed token, and `serving` serves a local sheaf store through the real wire server rather
than the deployed service. Everything between them and after them runs as it would against dev,
real `git clone` included.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import pathlib
import signal
import stat
import threading
import time
from collections.abc import Iterator

import pytest

from themis import sheaf
from themis.clients.sheaf import store as remote_mod
from themis.sheaf import refdoc
from themis.sheaf.tests import conftest as sheaf_conftest
from themis.sheaf.wire import protect, server
from tools import clone_analysis, session_token, sheaf_remote

ANALYSIS_ID = 'an_test'
TOKEN = 'derived-session-token'  # noqa: S105 — a stand-in, not a credential


@dataclasses.dataclass
class Seams:
    """What the stand-ins saw: each token file `serving` was handed, and its directory's and its own mode then."""

    token_files: list[pathlib.Path] = dataclasses.field(default_factory=list)
    scratch_modes: list[int] = dataclasses.field(default_factory=list)
    token_modes: list[int] = dataclasses.field(default_factory=list)


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool's `git clone` reads no host configuration, as the sheaf suite's git does not."""
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', os.devnull)


@pytest.fixture
def backend(tmp_path: pathlib.Path) -> sheaf.LocalBackend:
    return sheaf.LocalBackend(tmp_path / 'store')


def _stand_in(
    monkeypatch: pytest.MonkeyPatch,
    backend: sheaf.LocalBackend,
    *,
    served: str = ANALYSIS_ID,
    fail_with: BaseException | None = None,
) -> Seams:
    """Replace the token derivation and the remote; `serving` raises `fail_with` once the token is on disk."""
    seams = Seams()

    def derive(analysis_id: str, *, project: str, service_account: str, key_version: str | None) -> str:
        del project, service_account, key_version
        assert analysis_id == ANALYSIS_ID
        return TOKEN

    @contextlib.contextmanager
    def serving(
        *,
        analysis_id: str,
        token_file: pathlib.Path,
        service_url: str,
        service_account: str,
        port: int,
        root: pathlib.Path,
    ) -> Iterator[server.SheafGitServer]:
        del service_url, service_account
        assert analysis_id == ANALYSIS_ID
        assert port == 0
        seams.token_files.append(token_file)
        seams.scratch_modes.append(stat.S_IMODE(token_file.parent.stat().st_mode))
        seams.token_modes.append(stat.S_IMODE(token_file.stat().st_mode))
        assert remote_mod.read_credentials(token_file).session_token == TOKEN
        if fail_with is not None:
            raise fail_with
        with server.SheafGitServer.over_backend(
            backend, root, repos={served}, protection=protect.Protection.unprotected()
        ) as instance:
            yield instance

    monkeypatch.setattr(session_token, 'derive_session_token', derive)
    monkeypatch.setattr(sheaf_remote, 'serving', serving)
    return seams


def _clone(destination: pathlib.Path) -> None:
    clone_analysis.clone_analysis(
        ANALYSIS_ID,
        destination,
        project='p',
        service_url='https://sheaf.example',
        service_account='clu@example.iam.gserviceaccount.com',
        key_version=None,
    )


def _seed(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> sheaf.Snapshot:
    """Two publishes to main, then a side branch, a tag and a stranded ref, each with its reflog entry."""
    repo = sheaf_conftest.GitRepo.open(backend, ANALYSIS_ID, tmp_path / 'seed')
    for ref, content in [
        ('refs/heads/main', 'one'),
        ('refs/heads/main', 'two'),
        ('refs/heads/side', 'side'),
        ('refs/tags/v1', 'tagged'),
        ('refs/stranded/sesn_1/main', 'lost'),
    ]:
        repo.write_files(ref=ref, files={'notes.md': f'{content}\n'}, author=sheaf_conftest.SERVICE, message=content)
    return sheaf.Store(backend, ANALYSIS_ID).read()


def _local_refs(clone: pathlib.Path) -> dict[str, str]:
    listing = sheaf_conftest.run_git('for-each-ref', '--format=%(objectname) %(refname)', cwd=clone).stdout
    return {name: sha for sha, name in (line.split() for line in listing.splitlines())}


def test_every_ref_a_push_or_sheaf_writes_is_in_the_clone_and_main_is_checked_out(
    monkeypatch: pytest.MonkeyPatch, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    snapshot = _seed(backend, tmp_path)
    _stand_in(monkeypatch, backend)
    destination = tmp_path / 'clone'

    _clone(destination)

    local = _local_refs(destination)
    for ref, tip in snapshot.refs.items():
        name = ref.replace('refs/heads/', 'refs/remotes/origin/', 1) if ref.startswith('refs/heads/') else ref
        assert local.get(name) == tip, f'{ref} is in the clone as {name}'
    assert refdoc.REFLOG_REF in snapshot.refs, 'the seeding published a reflog, so the loop above checked it'
    assert (destination / 'notes.md').read_text('utf-8') == 'two\n'


def test_a_replace_ref_in_the_store_is_not_fetched(
    monkeypatch: pytest.MonkeyPatch, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """A writer without the hook can store one, and the local git would read one object as another."""
    snapshot = _seed(backend, tmp_path)
    replaced = snapshot.refs['refs/heads/main']
    repo = sheaf_conftest.GitRepo.open(backend, ANALYSIS_ID, tmp_path / 'forger')
    repo.write_files(
        ref=f'refs/replace/{replaced}', files={'notes.md': 'forged\n'}, author=sheaf_conftest.SERVICE, message='forged'
    )
    _stand_in(monkeypatch, backend)
    destination = tmp_path / 'clone'

    _clone(destination)

    assert not [ref for ref in _local_refs(destination) if ref.startswith('refs/replace/')]
    assert (destination / 'notes.md').read_text('utf-8') == 'two\n'


def test_an_empty_repository_clones_and_says_nothing_is_checked_out(
    monkeypatch: pytest.MonkeyPatch,
    backend: sheaf.LocalBackend,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stand_in(monkeypatch, backend)
    destination = tmp_path / 'clone'

    _clone(destination)

    assert (destination / '.git').is_dir()
    assert 'Nothing is checked out' in capsys.readouterr().out


def test_an_interrupt_is_passed_on_to_git_and_waited_out(tmp_path: pathlib.Path) -> None:
    """SIGTERM reaches only the tool; git has to be told, and waited for, to remove its half-made clone."""
    started, stopped = tmp_path / 'started', tmp_path / 'stopped'
    git = tmp_path / 'git'
    git.write_text(
        f"#!/bin/sh\ntrap 'sleep 0.5; touch {stopped}; exit 130' INT\ntouch {started}\nwhile :; do sleep 0.05; done\n",
        'utf-8',
    )
    git.chmod(0o755)

    def interrupt_once_git_listens() -> None:
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt_once_git_listens, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        clone_analysis.clone(str(git), 'http://127.0.0.1:1/an_test', tmp_path / 'clone')

    assert stopped.exists(), 'git got the interrupt and finished handling it before the tool went on'


def test_the_token_directory_is_private_and_gone_after_a_clone(
    monkeypatch: pytest.MonkeyPatch, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    _seed(backend, tmp_path)
    seams = _stand_in(monkeypatch, backend)

    _clone(tmp_path / 'clone')

    (token_file,) = seams.token_files
    assert seams.scratch_modes == [0o700]
    assert seams.token_modes == [0o600]
    assert not token_file.parent.exists()


@pytest.mark.parametrize(
    ('served', 'fail_with', 'raised'),
    [
        pytest.param('an_other', None, SystemExit, id='git-clone-fails'),
        pytest.param(ANALYSIS_ID, KeyboardInterrupt(), KeyboardInterrupt, id='interrupted'),
    ],
)
def test_the_token_directory_is_gone_after_a_failed_clone(
    monkeypatch: pytest.MonkeyPatch,
    backend: sheaf.LocalBackend,
    tmp_path: pathlib.Path,
    served: str,
    fail_with: BaseException | None,
    raised: type[BaseException],
) -> None:
    seams = _stand_in(monkeypatch, backend, served=served, fail_with=fail_with)

    with pytest.raises(raised):
        _clone(tmp_path / 'clone')

    (token_file,) = seams.token_files
    assert not token_file.parent.exists()


def test_an_occupied_destination_is_refused_before_anything_is_derived(
    monkeypatch: pytest.MonkeyPatch, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    seams = _stand_in(monkeypatch, backend)
    destination = tmp_path / 'clone'
    destination.mkdir()
    (destination / 'keep.md').write_text('mine\n', 'utf-8')

    with pytest.raises(SystemExit, match='not an empty directory'):
        _clone(destination)

    assert not seams.token_files
    assert (destination / 'keep.md').read_text('utf-8') == 'mine\n'


def test_an_empty_destination_directory_is_cloned_into(
    monkeypatch: pytest.MonkeyPatch, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    _seed(backend, tmp_path)
    _stand_in(monkeypatch, backend)
    destination = tmp_path / 'clone'
    destination.mkdir()

    _clone(destination)

    assert (destination / 'notes.md').read_text('utf-8') == 'two\n'
