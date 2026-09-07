"""The two hatches, driven by a real git over the real ext:: transport, with only the isolation missing.

Nothing here mocks git or the hook: the point of splicing to stock `git upload-pack` / `receive-pack` is that the
wire protocol is not ours to get wrong. `HostGitSandbox` runs the client host-side against the hatches' host
sockets; the same commands run in the guest on a bwrap host (`test_uid_mapping.py`).
"""

from __future__ import annotations

import logging
import pathlib

import pytest

from themis import sheaf
from themis.services.sandbox_worker.tests import conftest, fakes
from themis.sheaf.wire import reflog

MAIN = 'refs/heads/main'


def _clone(analysis: conftest.Analysis, workspace: pathlib.Path, *extra: str) -> fakes.HostGitSandbox:
    workspace.mkdir(exist_ok=True)
    guest = fakes.HostGitSandbox(workspace)
    cloned = guest.run(['git', *extra, 'clone', fakes.HostGitSandbox.url(analysis.upload_pack), '.'])
    assert cloned.ok, cloned.stderr
    guest.run(['git', 'remote', 'set-url', '--push', 'origin', fakes.HostGitSandbox.url(analysis.receive_pack)])
    return guest


def _commit(guest: fakes.HostGitSandbox, name: str, content: str) -> str:
    (guest.workspace / name).parent.mkdir(parents=True, exist_ok=True)
    (guest.workspace / name).write_text(content, 'utf-8')
    assert guest.run(['git', 'add', '-f', name]).ok
    assert guest.run(['git', 'commit', '-q', '-m', f'write {name}']).ok
    return guest.run(['git', 'rev-parse', 'HEAD']).stdout.strip()


def test_a_clone_of_an_empty_repository_lands_on_the_stores_branch(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    # ext:: carries no protocol request from the client, so unless the server side asks for v2 an empty clone
    # would land on the client's own default branch name, and the first push would create that branch instead.
    guest = _clone(analysis, tmp_path / 'ws', '-c', 'init.defaultBranch=trunk')
    assert guest.run(['git', 'symbolic-ref', 'HEAD']).stdout.strip() == MAIN


def test_a_push_reaches_the_store_through_the_hook(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    guest = _clone(analysis, tmp_path / 'ws')
    tip = _commit(guest, 'notes.md', 'first\n')

    pushed = guest.run(['git', 'push', 'origin', 'HEAD'])

    assert pushed.ok, pushed.stderr
    snapshot = analysis.store.read()
    assert snapshot.refs[MAIN] == tip
    assert reflog.REF in snapshot.refs
    assert snapshot.packs
    # The reflog ref is the agent's to read, by the refspec the prompt gives it, and never to write.
    fetched = guest.run(['git', 'fetch', '-q', 'origin', f'{reflog.NAMESPACE}*:{reflog.NAMESPACE}*'])
    assert fetched.ok, fetched.stderr
    assert guest.run(['git', 'rev-parse', reflog.REF]).stdout.strip() == snapshot.refs[reflog.REF]
    forged = guest.run(['git', 'push', '--force', 'origin', f'HEAD:{reflog.REF}'])
    assert not forged.ok
    assert 'written by sheaf' in forged.stderr
    assert analysis.store.read().refs[reflog.REF] == snapshot.refs[reflog.REF]


def test_the_fetch_hatch_cannot_be_talked_into_a_push(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    guest = _clone(analysis, tmp_path / 'ws')
    _commit(guest, 'notes.md', 'first\n')

    pushed = guest.run(['git', 'push', fakes.HostGitSandbox.url(analysis.upload_pack), 'HEAD'])

    assert not pushed.ok
    assert MAIN not in analysis.store.read().refs


def test_a_protected_path_is_refused_with_a_reason_the_client_reads(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    guest = _clone(analysis, tmp_path / 'ws')
    _commit(guest, 'skills/planted/SKILL.md', 'not the platform\n')

    pushed = guest.run(['git', 'push', 'origin', 'HEAD'])

    assert not pushed.ok
    assert 'protected skills/planted/SKILL.md' in pushed.stderr
    assert MAIN not in analysis.store.read().refs


def test_a_fetch_sees_what_another_session_pushed(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    first = _clone(analysis, tmp_path / 'first')
    second = _clone(analysis, tmp_path / 'second')
    tip = _commit(second, 'notes.md', 'from the second\n')
    assert second.run(['git', 'push', 'origin', 'HEAD']).ok

    # The mirror is refreshed from the store before every connection, or the first clone's pull would get its
    # own stale view back.
    pulled = first.run(['git', 'pull', '-q', 'origin', 'main'])

    assert pulled.ok, pulled.stderr
    assert first.run(['git', 'rev-parse', 'HEAD']).stdout.strip() == tip
    assert (first.workspace / 'notes.md').read_text('utf-8') == 'from the second\n'


def test_a_stale_push_is_refused_and_a_pull_then_push_recovers(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    first = _clone(analysis, tmp_path / 'first')
    second = _clone(analysis, tmp_path / 'second')
    _commit(second, 'notes.md', 'from the second\n')
    assert second.run(['git', 'push', 'origin', 'HEAD']).ok
    _commit(first, 'other.md', 'from the first\n')

    refused = first.run(['git', 'push', 'origin', 'HEAD'])
    assert not refused.ok
    assert 'fetch first' in refused.stderr

    forced = first.run(['git', 'push', '--force', 'origin', 'HEAD'])
    assert not forced.ok
    assert 'append-only' in forced.stderr

    assert first.run(['git', 'pull', '-q', '--rebase', 'origin', 'main']).ok
    recovered = first.run(['git', 'push', 'origin', 'HEAD'])
    assert recovered.ok, recovered.stderr
    assert analysis.store.read().refs[MAIN] == first.run(['git', 'rev-parse', 'HEAD']).stdout.strip()


def test_a_service_that_cannot_be_reached_refuses_the_connection_and_says_why(
    analysis: conftest.Analysis,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unreachable() -> object:
        raise sheaf.ServiceFault('UNAVAILABLE', 'the sheaf service is unreachable')

    monkeypatch.setattr(analysis.remote, 'read', unreachable)
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    guest = fakes.HostGitSandbox(workspace)

    with caplog.at_level(logging.ERROR):
        cloned = guest.run(['git', 'clone', fakes.HostGitSandbox.url(analysis.upload_pack), '.'])

    assert not cloned.ok
    assert any('mirror sync failed' in record.message for record in caplog.records)
    assert any('the sheaf service is unreachable' in (record.exc_text or '') for record in caplog.records)


def test_a_symlink_committed_at_a_protected_root_is_refused(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    # The SDK resolves `skills` before it writes there, so a symlink the clone materialised at that name would
    # redirect a root-owned rmtree and extract; the bare name is protected, not only what lies beneath it.
    guest = _clone(analysis, tmp_path / 'ws')
    (guest.workspace / 'skills').symlink_to('/etc')
    assert guest.run(['git', 'add', '-f', 'skills']).ok
    assert guest.run(['git', 'commit', '-q', '-m', 'plant a symlink']).ok

    pushed = guest.run(['git', 'push', 'origin', 'HEAD'])

    assert not pushed.ok
    assert 'protected skills' in pushed.stderr
    assert MAIN not in analysis.store.read().refs
