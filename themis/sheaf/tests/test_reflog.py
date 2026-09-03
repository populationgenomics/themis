"""The reflog chain as a data structure: what `record` writes and what `read` will and will not accept."""

from __future__ import annotations

import pathlib

import pytest

from themis.sheaf.tests import conftest
from themis.sheaf.wire import bare, reflog

SHA = 'a' * 40


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> bare.BareRepo:
    """An object database with one commit to point transitions at."""
    path = tmp_path / 'db.git'
    conftest.run_git('init', '-q', '--bare', str(path), cwd=tmp_path)
    return bare.BareRepo(None, path)  # type: ignore[arg-type]  # the store is never touched here


def _commit(repo: bare.BareRepo, message: str, *parents: str) -> str:
    tree = repo.git('hash-object', '-w', '-t', 'tree', '--stdin', stdin=b'').decode().strip()
    args = ['commit-tree', tree]
    for parent in parents:
        args += ['-p', parent]
    env = {'GIT_AUTHOR_NAME': 'x', 'GIT_AUTHOR_EMAIL': 'x@x', 'GIT_COMMITTER_NAME': 'x', 'GIT_COMMITTER_EMAIL': 'x@x'}
    return repo.git(*args, '-m', message, env=env).decode().strip()


def test_the_first_entry_is_rooted_on_a_parentless_commit_sheaf_wrote(repo: bare.BareRepo) -> None:
    tip = _commit(repo, 'user commit')
    entry = reflog.record(repo.git, None, [reflog.Transition('refs/heads/main', None, tip)])

    parents = repo.git('rev-list', '--parents', '-n', '1', entry).decode().split()[1:]
    root = parents[0]
    assert repo.git('rev-list', '--parents', '-n', '1', root).decode().split()[1:] == [], 'the root has no parents'
    assert repo.git('log', '-1', '--format=%s', root).decode().strip() == 'sheaf: init'
    assert tip in parents
    assert reflog.read(repo.git, entry) == [[reflog.Transition('refs/heads/main', None, tip)]]


def test_a_chain_ending_on_a_commit_sheaf_did_not_write_is_refused(repo: bare.BareRepo) -> None:
    """Damage, not something to skip: the chain is sheaf's own from tip to root."""
    tip = _commit(repo, 'user commit')
    entry = reflog.record(repo.git, tip, [reflog.Transition('refs/heads/main', None, tip)])

    with pytest.raises(ValueError, match='did not write'):
        reflog.read(repo.git, entry)


def test_an_entry_records_nothing_when_nothing_moved(repo: bare.BareRepo) -> None:
    with pytest.raises(ValueError, match='at least one'):
        reflog.record(repo.git, None, [])
