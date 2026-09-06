"""Real git against the loopback server over a `RemoteStore`, whose service is the in-process servicer.

Every read the mirror makes goes through ReadRefDoc and FetchPack; every push the hook subprocess
makes goes through Publish, with the store it rebuilds from the sync state's descriptor. Nothing
here mocks git or the service: the wire protocol is git's own, and the storage protocol is the
servicer's. Git is a hard requirement of the suite, as it is for `themis/sheaf/tests`.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import pytest

from themis import sheaf
from themis.clients.sheaf import store as remote_mod
from themis.clients.sheaf.tests import conftest
from themis.services.sheaf import servicer as servicer_mod
from themis.sheaf.tests import conftest as sheaf_conftest
from themis.sheaf.wire import bare, server

REPO = conftest.ANALYSIS_ID
REF = conftest.REF
SHA_A = 'a' * 40


@pytest.fixture
def git_server(remote: remote_mod.RemoteStore, tmp_path: pathlib.Path) -> Iterator[server.SheafGitServer]:
    """A running loopback git server whose one repository is the remote store."""
    with server.SheafGitServer([remote], tmp_path / 'bare') as instance:
        yield instance


def _clone(instance: server.SheafGitServer, tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    target = tmp_path / name
    sheaf_conftest.run_git('clone', instance.url(REPO), str(target), cwd=tmp_path)
    return target


def _remote_lines(stderr: str) -> list[str]:
    """What the server side said to the pusher: the hook's stderr, as git relays it."""
    return [line.strip() for line in stderr.splitlines() if line.startswith('remote:')]


def _commit(work: pathlib.Path, filename: str, content: str, message: str) -> str:
    (work / filename).write_text(content, 'utf-8')
    sheaf_conftest.run_git('add', filename, cwd=work)
    sheaf_conftest.run_git('commit', '-m', message, cwd=work)
    return sheaf_conftest.run_git('rev-parse', 'HEAD', cwd=work).stdout.strip()


def test_a_push_lands_through_the_service_and_a_second_clone_sees_it(
    git_server: server.SheafGitServer,
    remote: remote_mod.RemoteStore,
    backend: sheaf.LocalBackend,
    tmp_path: pathlib.Path,
) -> None:
    work = _clone(git_server, tmp_path, 'work')
    tip = _commit(work, 'notes.md', 'first\n', 'add notes')

    sheaf_conftest.run_git('push', 'origin', 'main', cwd=work)

    landed = conftest.direct(backend).read()
    assert landed.tip(REF) == tip, 'the hook published through the service and the bucket moved'
    assert landed.packs, 'the push landed a pack'
    assert remote.read() == landed
    other = _clone(git_server, tmp_path, 'other')
    assert (other / 'notes.md').read_text('utf-8') == 'first\n'


def test_a_stale_push_is_refused_with_git_own_wording_and_the_retry_converges(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    first = _clone(git_server, tmp_path, 'first')
    second = _clone(git_server, tmp_path, 'second')
    _commit(first, 'a.md', 'a\n', 'add a')
    sheaf_conftest.run_git('push', 'origin', 'main', cwd=first)
    accepted = conftest.direct(backend).read()

    _commit(second, 'b.md', 'b\n', 'add b')
    rejected = sheaf_conftest.run_git('push', 'origin', 'main', cwd=second, check=False)

    assert rejected.returncode != 0
    assert 'rejected' in rejected.stderr.lower()
    assert conftest.direct(backend).read() == accepted, 'a refused push changes nothing'
    sheaf_conftest.run_git('pull', '--rebase', 'origin', 'main', cwd=second)
    sheaf_conftest.run_git('push', 'origin', 'main', cwd=second)
    final = _clone(git_server, tmp_path, 'final')
    assert (final / 'a.md').exists()
    assert (final / 'b.md').exists()


def test_a_force_push_is_refused_by_the_hook_and_the_service_is_never_asked(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    first = _clone(git_server, tmp_path, 'first')
    second = _clone(git_server, tmp_path, 'second')
    _commit(first, 'a.md', 'a\n', 'add a')
    sheaf_conftest.run_git('push', 'origin', 'main', cwd=first)
    before = conftest.direct(backend).read()

    _commit(second, 'b.md', 'b\n', 'add b')
    pushed = sheaf_conftest.run_git('push', '--force', 'origin', 'main', cwd=second, check=False)

    assert pushed.returncode != 0
    assert 'may only fast-forward' in pushed.stderr
    assert conftest.direct(backend).read() == before


def test_a_write_landing_between_the_sync_and_the_hook_is_refused_as_the_remote_moving(
    git_server: server.SheafGitServer,
    backend: sheaf.LocalBackend,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook's generation check, reached through the service, in the words the pusher reads.

    A `git push` over smart HTTP is two requests — the ref advertisement, then receive-pack — and
    the server syncs the mirror before each. A competing publish is landed after the second sync,
    which is the window git's own fast-forward check cannot see and the hook's read of the document
    through the service has to.
    """
    work = _clone(git_server, tmp_path, 'work')
    _commit(work, 'a.md', 'a\n', 'add a')
    sheaf_conftest.run_git('push', 'origin', 'main', cwd=work)
    direct = conftest.direct(backend)
    before = direct.read()

    original = server.SheafGitServer.cgi_env
    remaining = [2]

    def compete_after_the_second_sync(self: server.SheafGitServer, mirror: bare.BareRepo) -> dict[str, str]:
        remaining[0] -= 1
        if remaining[0] == 0:
            base = direct.read()
            intent = sheaf.Intent({'refs/heads/other': sheaf.RefUpdate(None, SHA_A)})
            direct.publish(base, sheaf_conftest.logged(base, intent))
        return original(self, mirror)

    monkeypatch.setattr(server.SheafGitServer, 'cgi_env', compete_after_the_second_sync)
    _commit(work, 'b.md', 'b\n', 'add b')
    pushed = sheaf_conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert pushed.returncode != 0
    assert 'moved while your push was in flight' in pushed.stderr
    assert all(line.startswith('remote: sheaf:') for line in _remote_lines(pushed.stderr)), pushed.stderr
    after = direct.read()
    assert after.tip(REF) == before.tip(REF), 'the refused push published nothing'
    assert after.tip('refs/heads/other') == SHA_A


def test_a_service_fault_during_the_push_is_reported_as_a_deployment_fault(
    git_server: server.SheafGitServer,
    backend: sheaf.LocalBackend,
    token_file: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook's read through the service fails: a message in git's stream, never a traceback."""
    work = _clone(git_server, tmp_path, 'work')
    _commit(work, 'a.md', 'a\n', 'add a')
    sheaf_conftest.run_git('push', 'origin', 'main', cwd=work)
    before = conftest.direct(backend).read()

    original = server.SheafGitServer.cgi_env
    remaining = [2]

    def revoke_after_the_second_sync(self: server.SheafGitServer, mirror: bare.BareRepo) -> dict[str, str]:
        remaining[0] -= 1
        if remaining[0] == 0:
            conftest.write_token_file(token_file, 'revoked')
        return original(self, mirror)

    monkeypatch.setattr(server.SheafGitServer, 'cgi_env', revoke_after_the_second_sync)
    _commit(work, 'b.md', 'b\n', 'add b')
    pushed = sheaf_conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert pushed.returncode != 0
    assert 'deployment fault' in pushed.stderr
    assert 'PERMISSION_DENIED' in pushed.stderr
    assert all(line.startswith('remote: sheaf:') for line in _remote_lines(pushed.stderr)), pushed.stderr
    assert conftest.direct(backend).read() == before


def test_a_token_file_gone_by_the_push_is_reported_as_a_deployment_fault(
    git_server: server.SheafGitServer,
    backend: sheaf.LocalBackend,
    token_file: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook cannot read its credentials: a message in git's stream, never a traceback with a path in it."""
    work = _clone(git_server, tmp_path, 'work')
    _commit(work, 'a.md', 'a\n', 'add a')
    sheaf_conftest.run_git('push', 'origin', 'main', cwd=work)
    before = conftest.direct(backend).read()

    original = server.SheafGitServer.cgi_env
    remaining = [2]

    def remove_after_the_second_sync(self: server.SheafGitServer, mirror: bare.BareRepo) -> dict[str, str]:
        remaining[0] -= 1
        if remaining[0] == 0:
            token_file.unlink()
        return original(self, mirror)

    monkeypatch.setattr(server.SheafGitServer, 'cgi_env', remove_after_the_second_sync)
    _commit(work, 'b.md', 'b\n', 'add b')
    pushed = sheaf_conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert pushed.returncode != 0
    assert 'deployment fault' in pushed.stderr
    assert 'Traceback' not in pushed.stderr
    assert all(line.startswith('remote: sheaf:') for line in _remote_lines(pushed.stderr)), pushed.stderr
    assert conftest.direct(backend).read() == before


def test_a_publish_the_service_refuses_reaches_the_pusher_as_a_refusal(
    backend: sheaf.LocalBackend, token_file: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """What the service refuses on the intent alone is a message to the pusher, not a traceback."""
    limits = servicer_mod.Limits(max_publish_bytes=1, max_refs=64, max_document_bytes=1 << 16)
    with (
        conftest.serving(backend, limits) as target,
        remote_mod.RemoteStore(target, token_file, repo=REPO) as remote,
        server.SheafGitServer([remote], tmp_path / 'bare') as instance,
    ):
        work = _clone(instance, tmp_path, 'work')
        _commit(work, 'a.md', 'a\n', 'add a')
        pushed = sheaf_conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert pushed.returncode != 0
    assert 'ceiling' in pushed.stderr
    assert 'sheaf: refused' in pushed.stderr
    # Only the hook's own lines: a client library logging into the hook's stderr would land here too.
    assert all(line.startswith('remote: sheaf:') for line in _remote_lines(pushed.stderr)), pushed.stderr
    assert conftest.direct(backend).read().generation is None


def test_a_service_that_goes_away_under_the_publish_is_a_deployment_fault(
    backend: sheaf.LocalBackend, token_file: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    with (
        conftest.serving(backend, servicer_class=conftest.PublishDown) as target,
        remote_mod.RemoteStore(target, token_file, repo=REPO) as remote,
        server.SheafGitServer([remote], tmp_path / 'bare') as instance,
    ):
        work = _clone(instance, tmp_path, 'work')
        _commit(work, 'a.md', 'a\n', 'add a')
        pushed = sheaf_conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert pushed.returncode != 0
    assert 'deployment fault' in pushed.stderr
    assert 'UNAVAILABLE' in pushed.stderr
    assert all(line.startswith('remote: sheaf:') for line in _remote_lines(pushed.stderr)), pushed.stderr
    assert conftest.direct(backend).read().generation is None
