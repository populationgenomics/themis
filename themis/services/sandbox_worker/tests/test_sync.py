"""Tests for the workspace sync orchestration (restore fail-closed / scratch fail-open, checkpoint)."""

from __future__ import annotations

import asyncio
import io
import pathlib
import tarfile

import postern
import pytest

from themis.services.sandbox_worker import store_client, sync


def _scratch_tar(name: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as tar:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _sync(store: store_client.Store, root: pathlib.Path, *, exclude: tuple[str, ...] = ()) -> sync.WorkspaceSync:
    return sync.WorkspaceSync(store, accessor=postern.Workspace(root), exclude=exclude)


def _restore(archive: bytes, dest: pathlib.Path) -> None:
    with postern.Workspace(dest) as ws:
        ws.restore_tar(io.BytesIO(archive))


def test_restore_writes_document_and_scratch(tmp_path: pathlib.Path) -> None:
    store = store_client.FixtureStore(document='hello doc', workspace=_scratch_tar('note.txt', b'note'))
    asyncio.run(_sync(store, tmp_path).restore())
    assert (tmp_path / 'document.md').read_text() == 'hello doc'
    assert (tmp_path / 'note.txt').read_bytes() == b'note'


def test_restore_first_spawn_boots_empty(tmp_path: pathlib.Path) -> None:
    asyncio.run(_sync(store_client.FixtureStore(document=None, workspace=None), tmp_path).restore())
    assert not (tmp_path / 'document.md').exists()


def test_restore_fails_closed_on_a_document_store_error(tmp_path: pathlib.Path) -> None:
    class _Failing(store_client.FixtureStore):
        async def get_working_document(self) -> str | None:
            raise RuntimeError('store down')

    with pytest.raises(RuntimeError, match='store down'):
        asyncio.run(_sync(_Failing(), tmp_path).restore())


def test_scratch_fails_open_leaving_the_document(tmp_path: pathlib.Path) -> None:
    class _BadScratch(store_client.FixtureStore):
        async def get_workspace(self) -> bytes | None:
            raise RuntimeError('store down')

    asyncio.run(_sync(_BadScratch(document='d'), tmp_path).restore())
    assert (tmp_path / 'document.md').read_text() == 'd'  # the document restored; scratch silently empty


def test_checkpoint_puts_document_then_scratch_excluding_the_document(tmp_path: pathlib.Path) -> None:
    (tmp_path / 'document.md').write_text('v1')
    (tmp_path / 'note.txt').write_bytes(b'note')
    store = store_client.FixtureStore()
    asyncio.run(_sync(store, tmp_path).checkpoint())

    assert store.put_documents == ['v1']
    assert len(store.put_workspaces) == 1
    restored = tmp_path / 'restored'
    restored.mkdir()
    _restore(store.put_workspaces[0], restored)
    assert (restored / 'note.txt').read_bytes() == b'note'
    assert not (restored / 'document.md').exists()  # the durable document is excluded from scratch


def test_checkpoint_skips_an_unchanged_document(tmp_path: pathlib.Path) -> None:
    (tmp_path / 'document.md').write_text('v1')
    store = store_client.FixtureStore()
    workspace_sync = _sync(store, tmp_path)
    asyncio.run(workspace_sync.checkpoint())
    asyncio.run(workspace_sync.checkpoint())  # unchanged since the last write — no second version

    assert store.put_documents == ['v1']
    assert len(store.put_workspaces) == 2  # scratch is overwrite-on-put, written every checkpoint


def test_checkpoint_after_restore_without_an_edit_mints_no_version(tmp_path: pathlib.Path) -> None:
    store = store_client.FixtureStore(document='restored')
    workspace_sync = _sync(store, tmp_path)
    asyncio.run(workspace_sync.restore())
    asyncio.run(workspace_sync.checkpoint())  # the restored document is unchanged

    assert store.put_documents == []


def test_checkpoint_after_an_edit_mints_a_version(tmp_path: pathlib.Path) -> None:
    store = store_client.FixtureStore(document='restored')
    workspace_sync = _sync(store, tmp_path)
    asyncio.run(workspace_sync.restore())
    (tmp_path / 'document.md').write_text('edited')
    asyncio.run(workspace_sync.checkpoint())

    assert store.put_documents == ['edited']


def test_checkpoint_excludes_the_skills_tree(tmp_path: pathlib.Path) -> None:
    (tmp_path / 'note.txt').write_bytes(b'keep')
    (tmp_path / 'skills' / 'foo').mkdir(parents=True)
    (tmp_path / 'skills' / 'foo' / 'script.py').write_bytes(b'print(1)')
    store = store_client.FixtureStore()
    asyncio.run(_sync(store, tmp_path, exclude=('skills',)).checkpoint())

    restored = tmp_path / 'restored'
    restored.mkdir()
    _restore(store.put_workspaces[0], restored)
    assert (restored / 'note.txt').read_bytes() == b'keep'
    assert not (restored / 'skills').exists()  # re-downloaded each spawn; never checkpointed or restored


def test_checkpoint_does_not_dereference_a_symlinked_document(tmp_path: pathlib.Path) -> None:
    # A guest that replaces document.md with a symlink to an out-of-workspace path must not have it read
    # (the confined accessor would ELOOP); the checkpoint skips it and mints no version.
    secret = tmp_path.parent / 'secret'
    secret.write_text('SECRET')
    (tmp_path / 'document.md').symlink_to(secret)
    store = store_client.FixtureStore()
    asyncio.run(_sync(store, tmp_path).checkpoint())

    assert store.put_documents == []  # never dereferenced, never stored
