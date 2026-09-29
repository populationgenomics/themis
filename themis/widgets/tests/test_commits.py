"""The rule over git history, read off a repository's objects: which versions a commit's check reads, and a missing one.

The rule's decisions over real pushes are held in themis/services/sandbox_worker/tests/test_widget_push.py; these build
commits object by object, which is how a tree naming a blob the repository lacks comes about.
"""

from __future__ import annotations

import functools
import pathlib
import subprocess

import pytest

from themis.widgets import asset, commits, tracking
from themis.widgets.models import checklist_pb2

_Checklist = checklist_pb2.Checklist
# A blob id no object in the repository has.
_ABSENT = '0123456789abcdef0123456789abcdef01234567'


def _git(repo: pathlib.Path, *args: str, stdin: bytes | None = None) -> str:
    return (
        subprocess.run(  # noqa: S603
            ['git', '-C', str(repo), '-c', 'user.name=u', '-c', 'user.email=u@x', *args],  # noqa: S607
            input=stdin,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )


def _blob(repo: pathlib.Path, data: bytes) -> str:
    return _git(repo, 'hash-object', '-w', '--stdin', stdin=data)


def _commit(repo: pathlib.Path, files: dict[str, str], *parents: str) -> str:
    """A commit whose root tree holds each name at the blob id given, present in the repository or not."""
    listing = ''.join(f'100644 blob {blob}\t{name}\n' for name, blob in sorted(files.items()))
    tree = _git(repo, 'mktree', '--missing', stdin=listing.encode())
    return _git(repo, 'commit-tree', tree, *(arg for parent in parents for arg in ('-p', parent)), '-m', 'c')


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    _git(tmp_path, 'init', '--quiet')
    return tmp_path


def _check(repo: pathlib.Path, commit: str, *parents: str) -> list[str]:
    return commits.check(functools.partial(tracking.git, repo), commit, parents)


def _checklist(checked: bool) -> bytes:
    return asset.encode(_Checklist(items=[_Checklist.Item(id='a', label='x', checked=checked)]))


def test_a_version_the_repository_lacks_is_a_fault_named_as_such(repo: pathlib.Path) -> None:
    commit = _commit(repo, {'a.binpb': _ABSENT})
    with pytest.raises(RuntimeError, match=f'holds no object {_ABSENT}'):
        _check(repo, commit)


def test_a_parent_s_version_is_read_where_the_new_one_is_an_asset(repo: pathlib.Path) -> None:
    parent = _commit(repo, {'a.binpb': _ABSENT})
    commit = _commit(repo, {'a.binpb': _blob(repo, _checklist(checked=False))}, parent)
    with pytest.raises(RuntimeError, match=f'holds no object {_ABSENT}'):
        _check(repo, commit, parent)


def test_a_parent_s_version_is_not_read_where_the_new_one_is_no_asset(repo: pathlib.Path) -> None:
    parent = _commit(repo, {'notes.md': _ABSENT})
    commit = _commit(repo, {'notes.md': _blob(repo, b'# notes\n')}, parent)
    assert _check(repo, commit, parent) == []


def test_a_tick_the_agent_sets_on_a_new_asset_is_refused(repo: pathlib.Path) -> None:
    commit = _commit(repo, {'a.binpb': _blob(repo, _checklist(checked=True))})
    [reason] = _check(repo, commit)
    assert reason.startswith("a.binpb: items[a].checked is a user's judgement, and the agent sets it")


def test_a_tree_naming_a_path_twice_is_a_fault(repo: pathlib.Path) -> None:
    first = _blob(repo, _checklist(checked=False))
    second = _blob(repo, _checklist(checked=True))
    listing = b''.join(b'100644 a.binpb\x00' + bytes.fromhex(blob) for blob in (first, second))
    tree = _git(repo, 'hash-object', '-t', 'tree', '-w', '--literally', '--stdin', stdin=listing)
    commit = _git(repo, 'commit-tree', tree, '-m', 'c')
    with pytest.raises(RuntimeError, match=r'lists a\.binpb twice'):
        _check(repo, commit)


def test_a_tree_naming_a_path_twice_beside_a_parent_holding_one_of_them_is_a_fault(repo: pathlib.Path) -> None:
    held = _blob(repo, _checklist(checked=False))
    parent = _commit(repo, {'a.binpb': held})
    added = _blob(repo, _checklist(checked=True))
    listing = b''.join(b'100644 a.binpb\x00' + bytes.fromhex(blob) for blob in (held, added))
    tree = _git(repo, 'hash-object', '-t', 'tree', '-w', '--literally', '--stdin', stdin=listing)
    commit = _git(repo, 'commit-tree', tree, '-p', parent, '-m', 'c')
    with pytest.raises(RuntimeError, match=r'lists a\.binpb twice'):
        _check(repo, commit, parent)


def test_a_commit_writing_more_assets_than_a_command_line_holds_is_checked(repo: pathlib.Path) -> None:
    blob = _blob(repo, _checklist(checked=False))
    # Names long enough that listing them all as arguments passes any platform's argument limit.
    stem = 'n' * 200
    commit = _commit(repo, {f'{stem}-{index}.binpb': blob for index in range(20_000)})
    assert _check(repo, commit) == []
