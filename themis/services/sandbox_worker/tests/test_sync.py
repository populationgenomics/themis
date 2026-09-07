"""Tests for the workspace sync orchestration: the fail-closed restore, the document checkpoint, the teardown push."""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import override

import postern
import pytest

from themis import sheaf
from themis.clients.auth.tests import fixture_session
from themis.services.sandbox_worker import guest_git, store_client, sync
from themis.services.sandbox_worker.tests import conftest, fakes

MAIN = 'refs/heads/main'
SESSION = 'sesn_01ABC'


def _sync(
    analysis: conftest.Analysis, store: store_client.Store, root: pathlib.Path
) -> tuple[sync.WorkspaceSync, fakes.HostGitSandbox]:
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
    return sync.WorkspaceSync(store, accessor=postern.Workspace(root), repository=repository), guest


def test_restore_clones_the_repository_then_writes_the_document(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    workspace_sync, guest = _sync(analysis, store_client.FixtureStore(document='hello doc'), tmp_path / 'ws')

    asyncio.run(workspace_sync.restore())

    assert (guest.workspace / 'working_document.md').read_text() == 'hello doc'
    assert (guest.workspace / '.gitignore').read_text() == guest_git.GITIGNORE
    assert MAIN in analysis.store.read().refs
    # The hook finds the session token by the path in the sync state, never in the state itself.
    state = json.loads(analysis.hatches.mirror.sync_state_path.read_text('utf-8'))
    assert fixture_session.GOOD_TOKEN not in state['store'].values()
    assert state['store']['token_file'] == str(analysis.remote.token_file)


def test_the_restored_checkpoint_is_an_untracked_file_the_agent_commits_like_any_other(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    workspace_sync, guest = _sync(analysis, store_client.FixtureStore(document='hello doc'), tmp_path / 'ws')
    asyncio.run(workspace_sync.restore())

    assert guest.run(['git', 'status', '--porcelain']).stdout.split() == ['??', 'working_document.md']
    assert guest.run(['git', 'add', 'working_document.md']).ok
    assert guest.run(['git', 'commit', '-q', '-m', 'the document']).ok
    pushed = guest.run(['git', 'push', '-q', 'origin', 'HEAD'])
    assert pushed.ok, pushed.stderr
    assert analysis.store.read().refs[MAIN] == guest.run(['git', 'rev-parse', 'HEAD']).stdout.strip()


def test_restore_keeps_the_repositorys_document_over_the_checkpoint(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    # Once the repository tracks the document, it is the one the session works on; the checkpoint is a copy of it,
    # so a store that has fallen behind the repository is brought up to it on the first command, not the reverse.
    first, guest = _sync(analysis, store_client.FixtureStore(document='checkpointed'), tmp_path / 'first')
    asyncio.run(first.restore())
    (guest.workspace / 'working_document.md').write_text('committed')
    assert guest.run(['git', 'add', 'working_document.md']).ok
    assert guest.run(['git', 'commit', '-q', '-m', 'the document']).ok
    assert guest.run(['git', 'push', '-q', 'origin', 'HEAD']).ok

    store = store_client.FixtureStore(document='checkpointed')
    later, later_guest = _sync(analysis, store, tmp_path / 'later')
    asyncio.run(later.restore())

    assert (later_guest.workspace / 'working_document.md').read_text() == 'committed'
    assert later_guest.run(['git', 'status', '--porcelain']).stdout == ''
    asyncio.run(later.checkpoint())
    assert store.put_documents == ['committed']


def test_restore_first_spawn_boots_without_a_document(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    workspace_sync, guest = _sync(analysis, store_client.FixtureStore(document=None), tmp_path / 'ws')
    asyncio.run(workspace_sync.restore())
    assert not (guest.workspace / 'working_document.md').exists()
    assert (guest.workspace / '.git').is_dir()


def test_restore_fails_closed_on_a_document_store_error(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    class _Failing(store_client.FixtureStore):
        @override
        async def get_working_document(self) -> str | None:
            raise RuntimeError('store down')

    workspace_sync, _guest = _sync(analysis, _Failing(), tmp_path / 'ws')
    with pytest.raises(RuntimeError, match='store down'):
        asyncio.run(workspace_sync.restore())


def test_restore_fails_closed_on_a_repository_store_error(
    analysis: conftest.Analysis, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable() -> object:
        raise sheaf.ServiceFault('UNAVAILABLE', 'the sheaf service is unreachable')

    monkeypatch.setattr(analysis.remote, 'read', unreachable)
    workspace_sync, guest = _sync(analysis, store_client.FixtureStore(document='d'), tmp_path / 'ws')

    with pytest.raises(sheaf.SheafError, match='unreachable'):
        asyncio.run(workspace_sync.restore())
    assert not (guest.workspace / 'working_document.md').exists()  # nothing served on a half-restored tree


def test_checkpoint_puts_the_document_and_touches_no_ref(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    store = store_client.FixtureStore()
    workspace_sync, guest = _sync(analysis, store, tmp_path / 'ws')
    asyncio.run(workspace_sync.restore())
    before = analysis.store.read()
    (guest.workspace / 'working_document.md').write_text('v1')
    (guest.workspace / 'note.txt').write_text('uncommitted')

    asyncio.run(workspace_sync.checkpoint())

    assert store.put_documents == ['v1']
    assert analysis.store.read().generation == before.generation  # the agent controls its snapshots


def test_checkpoint_skips_an_unchanged_document(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    store = store_client.FixtureStore()
    workspace_sync, guest = _sync(analysis, store, tmp_path / 'ws')
    (guest.workspace / 'working_document.md').write_text('v1')
    asyncio.run(workspace_sync.checkpoint())
    asyncio.run(workspace_sync.checkpoint())  # unchanged since the last write — no second version

    assert store.put_documents == ['v1']


def test_checkpoint_after_restore_without_an_edit_mints_no_version(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    store = store_client.FixtureStore(document='restored')
    workspace_sync, _guest = _sync(analysis, store, tmp_path / 'ws')
    asyncio.run(workspace_sync.restore())
    asyncio.run(workspace_sync.checkpoint())  # the restored document is unchanged

    assert store.put_documents == []


def test_checkpoint_after_an_edit_mints_a_version(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    store = store_client.FixtureStore(document='restored')
    workspace_sync, guest = _sync(analysis, store, tmp_path / 'ws')
    asyncio.run(workspace_sync.restore())
    (guest.workspace / 'working_document.md').write_text('edited')
    asyncio.run(workspace_sync.checkpoint())

    assert store.put_documents == ['edited']


def test_checkpoint_does_not_dereference_a_symlinked_document(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    # A guest that replaces working_document.md with a symlink to an out-of-workspace path must not have it read
    # (the confined accessor would ELOOP); the checkpoint skips it and mints no version.
    secret = tmp_path / 'secret'
    secret.write_text('SECRET')
    store = store_client.FixtureStore()
    workspace_sync, guest = _sync(analysis, store, tmp_path / 'ws')
    (guest.workspace / 'working_document.md').symlink_to(secret)

    asyncio.run(workspace_sync.checkpoint())

    assert store.put_documents == []  # never dereferenced, never stored


def test_restore_replaces_a_committed_symlink_at_the_document_path(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    # A previous session could have committed a symlink named working_document.md; the clone materialises it, and
    # the confined write would then ELOOP and wedge every later spawn. Restore clears it and writes the store's.
    workspace_sync, guest = _sync(analysis, store_client.FixtureStore(document='the real document'), tmp_path / 'seed')
    asyncio.run(workspace_sync.restore())
    (guest.workspace / 'working_document.md').unlink()
    (guest.workspace / 'working_document.md').symlink_to('/etc/hostname')
    assert guest.run(['git', 'add', '-A']).ok
    assert guest.run(['git', 'commit', '-q', '-m', 'plant a symlink']).ok
    assert guest.run(['git', 'push', '-q', 'origin', 'HEAD']).ok

    later, later_guest = _sync(analysis, store_client.FixtureStore(document='the real document'), tmp_path / 'later')
    asyncio.run(later.restore())

    document = later_guest.workspace / 'working_document.md'
    assert not document.is_symlink()
    assert document.read_text() == 'the real document'


def test_restore_removes_a_non_directory_the_clone_left_at_skills(
    analysis: conftest.Analysis, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hook refuses a push touching `skills`, so only a hook bypass could commit one; the restore still clears
    # it, since the SDK resolves that name before it writes there as the worker.
    monkeypatch.setattr(analysis.hatches, 'protection', analysis.hatches.protection.__class__())
    workspace_sync, guest = _sync(analysis, store_client.FixtureStore(document='d'), tmp_path / 'seed')
    asyncio.run(workspace_sync.restore())
    (guest.workspace / 'skills').symlink_to('/etc')
    assert guest.run(['git', 'add', '-f', 'skills']).ok
    assert guest.run(['git', 'commit', '-q', '-m', 'plant a symlink']).ok
    assert guest.run(['git', 'push', '-q', 'origin', 'HEAD']).ok

    later, later_guest = _sync(analysis, store_client.FixtureStore(document='d'), tmp_path / 'later')
    asyncio.run(later.restore())

    assert not (later_guest.workspace / 'skills').exists()
    assert not (later_guest.workspace / 'skills').is_symlink()


def test_teardown_pushes_unpushed_commits_even_when_the_checkpoint_fails(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    """A store outage at the end of a session must not cost the commits the teardown push exists to save."""

    class _Failing(store_client.FixtureStore):
        @override
        async def put_working_document(self, markdown: str) -> int:
            raise RuntimeError('store down')

    workspace_sync, guest = _sync(analysis, _Failing(), tmp_path / 'ws')
    asyncio.run(workspace_sync.restore())
    (guest.workspace / 'working_document.md').write_text('final')
    (guest.workspace / 'notes.md').write_text('committed, not pushed')
    assert guest.run(['git', 'add', 'notes.md']).ok
    assert guest.run(['git', 'commit', '-q', '-m', 'notes']).ok
    tip = guest.run(['git', 'rev-parse', 'HEAD']).stdout.strip()

    with pytest.raises(RuntimeError, match='store down'):
        asyncio.run(workspace_sync.teardown(SESSION))

    assert analysis.store.read().refs[MAIN] == tip, 'the push has to land regardless of the checkpoint'


def test_teardown_checkpoints_the_document_and_pushes_unpushed_commits(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    store = store_client.FixtureStore()
    workspace_sync, guest = _sync(analysis, store, tmp_path / 'ws')
    asyncio.run(workspace_sync.restore())
    (guest.workspace / 'working_document.md').write_text('final')
    (guest.workspace / 'notes.md').write_text('committed, not pushed')
    assert guest.run(['git', 'add', 'notes.md']).ok
    assert guest.run(['git', 'commit', '-q', '-m', 'notes']).ok
    tip = guest.run(['git', 'rev-parse', 'HEAD']).stdout.strip()

    asyncio.run(workspace_sync.teardown(SESSION))

    assert store.put_documents == ['final']
    assert analysis.store.read().refs[MAIN] == tip
