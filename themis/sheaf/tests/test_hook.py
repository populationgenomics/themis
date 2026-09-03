"""The pre-receive hook in isolation.

The hook's interesting branch is the narrow window between the mirror's sync and the
compare-and-swap: git has already accepted the push as a fast-forward against what was advertised,
and something landed anyway. Hitting that window from a real client would mean injecting into a
millisecond gap, so it is exercised here directly — while `test_wire.py` covers the case a real
client actually meets, where git does the rejecting.
"""

from __future__ import annotations

import io
import pathlib

import pytest

from themis import sheaf
from themis.sheaf.tests import conftest
from themis.sheaf.wire import bare, hook

REPO = 'projects/demo'
REF = 'refs/heads/main'
AUTHOR = conftest.Author('Reviewer One', 'reviewer.one@example.org')


def _mark(repo: conftest.GitRepo, line: str) -> None:
    repo.append_line(ref=REF, path='annotations/review.jsonl', line=line, author=AUTHOR, message=line)


@pytest.mark.parametrize('width', [40, 64])
def test_parse_stdin_reads_creates_updates_and_deletes(width: int) -> None:
    """Both hash widths: git sends the zero oid at the repository's own hash length."""
    zero = '0' * width
    a, b = 'a' * width, 'b' * width
    updates = hook.parse_stdin(
        [
            f'{zero} {a} refs/heads/new',
            f'{a} {b} refs/heads/moved',
            f'{b} {zero} refs/heads/gone',
        ]
    )
    assert updates['refs/heads/new'].old is None
    assert updates['refs/heads/moved'].old == a
    assert updates['refs/heads/gone'].new is None
    assert len(updates) == 3


def test_a_malformed_push_line_is_refused_rather_than_dropped() -> None:
    """Publishing only the lines that parsed would leave the store missing a ref the mirror has."""
    with pytest.raises(ValueError, match='fields per line'):
        hook.parse_stdin([f'{"a" * 40} {"b" * 40} refs/heads/main', 'garbage'])


def test_a_push_is_rejected_when_the_store_moved_after_the_sync(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    curator = conftest.GitRepo.open(backend, REPO, tmp_path / 'curator.git')
    _mark(curator, 'PM2')

    store = sheaf.Store(backend, REPO)
    mirror = bare.BareRepo(store, tmp_path / 'bare')
    synced = mirror.sync()
    head = synced.tip(REF)

    # A concurrent write lands in the window between the advertisement and the swap. It moves a
    # *different* ref: had it moved `REF`, the non-fast-forward check would refuse the push on its
    # own and the generation check would go untested.
    store.publish(
        store.read(),
        conftest.logged(store.read(), sheaf.Intent(ref_updates={'refs/heads/other': sheaf.RefUpdate(None, head)})),
    )
    assert store.read().generation != synced.generation
    assert store.read().tip(REF) == head

    monkeypatch.setenv(hook.SYNC_STATE_ENV, str(mirror.sync_state_path))
    monkeypatch.setenv(hook.GIT_DIR_ENV, str(mirror.path))
    monkeypatch.setattr('sys.stdin', io.StringIO(f'{head} {head} {REF}\n'))

    before = store.read()
    assert hook.main() == 1
    after = store.read()
    assert after.generation == before.generation, 'a push refused for a lost race must publish nothing'


def test_an_empty_push_is_accepted_without_touching_the_store(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = sheaf.Store(backend, REPO)
    mirror = bare.BareRepo(store, tmp_path / 'bare')
    mirror.sync()
    monkeypatch.setenv(hook.SYNC_STATE_ENV, str(mirror.sync_state_path))
    monkeypatch.setenv(hook.GIT_DIR_ENV, str(mirror.path))
    monkeypatch.setattr('sys.stdin', io.StringIO(''))
    assert hook.main() == 0


def test_a_missing_sync_state_refuses_rather_than_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(hook.SYNC_STATE_ENV, raising=False)
    assert hook.main() == 1
