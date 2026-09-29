"""Tests for the workspace sync orchestration: the fail-closed restore and the teardown push."""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from themis import sheaf
from themis.clients.auth.tests import fixture_session
from themis.services.sandbox_worker import guest_git, sync
from themis.services.sandbox_worker.tests import conftest, fakes
from themis.sheaf.wire import protect

MAIN = 'refs/heads/main'
SESSION = 'sesn_01ABC'


def _sync(analysis: conftest.Analysis, root: pathlib.Path) -> tuple[sync.WorkspaceSync, fakes.HostGitSandbox]:
    root.mkdir(exist_ok=True)
    guest = fakes.HostGitSandbox(root)
    repository = guest_git.GuestGit(
        guest,
        analysis.hatches,
        fetch_url=fakes.HostGitSandbox.url(analysis.upload_pack),
        push_url=fakes.HostGitSandbox.url(analysis.receive_pack),
        hydrate_timeout=60,
        teardown_timeout=60,
    )
    return sync.WorkspaceSync(workspace=root, repository=repository), guest


def _commit_and_push(guest: fakes.HostGitSandbox, *paths: str, force: bool = False) -> None:
    assert guest.run(['git', 'add', *(['-f'] if force else []), *paths]).ok
    assert guest.run(['git', 'commit', '-q', '-m', 'work']).ok
    pushed = guest.run(['git', 'push', '-q', 'origin', 'HEAD'])
    assert pushed.ok, pushed.stderr


def test_restore_clones_the_repository(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    workspace_sync, guest = _sync(analysis, tmp_path / 'ws')

    asyncio.run(workspace_sync.restore())

    assert (guest.workspace / '.gitignore').read_text() == guest_git.GITIGNORE
    assert MAIN in analysis.store.read().refs
    # The hook finds the session token by the path in the sync state, never in the state itself.
    state = json.loads(analysis.hatches.mirror.sync_state_path.read_text('utf-8'))
    assert fixture_session.GOOD_TOKEN not in state['store'].values()
    assert state['store']['token_file'] == str(analysis.remote.token_file)


def test_restore_first_spawn_boots_without_a_document(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    workspace_sync, guest = _sync(analysis, tmp_path / 'ws')
    asyncio.run(workspace_sync.restore())
    assert not (guest.workspace / 'working_document.md').exists()
    assert (guest.workspace / '.git').is_dir()


def test_a_later_restore_finds_the_document_an_earlier_session_pushed(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    first, guest = _sync(analysis, tmp_path / 'first')
    asyncio.run(first.restore())
    (guest.workspace / 'working_document.md').write_text('committed')
    _commit_and_push(guest, 'working_document.md')

    later, later_guest = _sync(analysis, tmp_path / 'later')
    asyncio.run(later.restore())

    assert (later_guest.workspace / 'working_document.md').read_text() == 'committed'
    assert later_guest.run(['git', 'status', '--porcelain']).stdout == ''


def test_restore_fails_closed_on_a_repository_store_error(
    analysis: conftest.Analysis, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable() -> object:
        raise sheaf.ServiceFault('UNAVAILABLE', 'the sheaf service is unreachable')

    monkeypatch.setattr(analysis.remote, 'read', unreachable)
    workspace_sync, guest = _sync(analysis, tmp_path / 'ws')

    with pytest.raises(sheaf.SheafError, match='unreachable'):
        asyncio.run(workspace_sync.restore())
    assert not (guest.workspace / '.git').exists()  # nothing served on a half-restored tree


def test_restore_removes_a_non_directory_the_clone_left_at_skills(
    analysis: conftest.Analysis, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hook refuses a push touching `skills`, so only a hook bypass could commit one; the restore still clears
    # it, since the SDK resolves that name before it writes there as the worker.
    monkeypatch.setattr(analysis.hatches, 'protection', protect.Protection.unprotected())
    workspace_sync, guest = _sync(analysis, tmp_path / 'seed')
    asyncio.run(workspace_sync.restore())
    (guest.workspace / 'skills').symlink_to('/etc')
    _commit_and_push(guest, 'skills', force=True)

    later, later_guest = _sync(analysis, tmp_path / 'later')
    asyncio.run(later.restore())

    assert not (later_guest.workspace / 'skills').exists()
    assert not (later_guest.workspace / 'skills').is_symlink()


def test_teardown_pushes_unpushed_commits(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    workspace_sync, guest = _sync(analysis, tmp_path / 'ws')
    asyncio.run(workspace_sync.restore())
    (guest.workspace / 'notes.md').write_text('committed, not pushed')
    assert guest.run(['git', 'add', 'notes.md']).ok
    assert guest.run(['git', 'commit', '-q', '-m', 'notes']).ok
    tip = guest.run(['git', 'rev-parse', 'HEAD']).stdout.strip()

    asyncio.run(workspace_sync.teardown(SESSION))

    assert analysis.store.read().refs[MAIN] == tip
