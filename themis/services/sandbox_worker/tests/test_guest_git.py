"""The worker's own guest git: hydration, the first commit, and the teardown push with its stranded fallback."""

from __future__ import annotations

import pathlib

import pytest

from themis import sheaf
from themis.services.sandbox_worker import guest_git
from themis.services.sandbox_worker.tests import conftest, fakes

MAIN = 'refs/heads/main'
SESSION = 'sesn_01ABC'


def _repository(
    analysis: conftest.Analysis, workspace: pathlib.Path
) -> tuple[guest_git.GuestGit, fakes.HostGitSandbox]:
    workspace.mkdir(exist_ok=True)
    guest = fakes.HostGitSandbox(workspace)
    repository = guest_git.GuestGit(
        guest,
        analysis.hatches,
        fetch_url=fakes.HostGitSandbox.url(analysis.upload_pack),
        push_url=fakes.HostGitSandbox.url(analysis.receive_pack),
        hydrate_timeout=60,
        teardown_timeout=60,
    )
    return repository, guest


def _commit(guest: fakes.HostGitSandbox, name: str, content: str) -> str:
    (guest.workspace / name).parent.mkdir(parents=True, exist_ok=True)
    (guest.workspace / name).write_text(content, 'utf-8')
    assert guest.run(['git', 'add', '-f', name]).ok
    assert guest.run(['git', 'commit', '-q', '-m', f'write {name}']).ok
    return guest.run(['git', 'rev-parse', 'HEAD']).stdout.strip()


def _head(guest: fakes.HostGitSandbox) -> str:
    return guest.run(['git', 'rev-parse', 'HEAD']).stdout.strip()


def test_hydrating_an_empty_repository_seeds_it_with_the_gitignore_and_pushes_it(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    repository, guest = _repository(analysis, tmp_path / 'ws')

    repository.hydrate()

    assert (guest.workspace / '.gitignore').read_text('utf-8') == guest_git.GITIGNORE
    snapshot = analysis.store.read()
    assert snapshot.refs[MAIN] == _head(guest)
    assert guest.run(['git', 'status', '--porcelain']).stdout == ''
    # The agent's own git reaches the store: fetches go to one hatch, pushes to the other.
    assert guest.run(['git', 'remote', 'get-url', 'origin']).stdout.strip() == fakes.HostGitSandbox.url(
        analysis.upload_pack
    )
    assert guest.run(['git', 'remote', 'get-url', '--push', 'origin']).stdout.strip() == fakes.HostGitSandbox.url(
        analysis.receive_pack
    )


def test_the_gitignore_keeps_scratch_and_skills_out_of_status(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    repository, guest = _repository(analysis, tmp_path / 'ws')
    repository.hydrate()
    for planted in ('scratch/notes.txt', 'skills/probe/SKILL.md', 'kept.md'):
        (guest.workspace / planted).parent.mkdir(parents=True, exist_ok=True)
        (guest.workspace / planted).write_text('x', 'utf-8')

    status = guest.run(['git', 'status', '--porcelain']).stdout

    assert status.split() == ['??', 'kept.md']


def test_hydrating_an_existing_repository_checks_it_out_and_commits_nothing(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    first, first_guest = _repository(analysis, tmp_path / 'first')
    first.hydrate()
    tip = _commit(first_guest, 'notes.md', 'from the first session\n')
    assert first_guest.run(['git', 'push', '-q', 'origin', 'HEAD']).ok
    before = analysis.store.read()

    second, second_guest = _repository(analysis, tmp_path / 'second')
    second.hydrate()

    assert (second_guest.workspace / 'notes.md').read_text('utf-8') == 'from the first session\n'
    assert _head(second_guest) == tip
    assert analysis.store.read().generation == before.generation


def test_hydrate_fails_closed_when_the_service_cannot_be_reached(
    analysis: conftest.Analysis, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable() -> object:
        raise sheaf.ServiceFault('UNAVAILABLE', 'the sheaf service is unreachable')

    monkeypatch.setattr(analysis.remote, 'read', unreachable)
    repository, guest = _repository(analysis, tmp_path / 'ws')

    with pytest.raises(sheaf.SheafError, match='unreachable'):
        repository.hydrate()
    assert guest.calls == []  # the store is read in the worker's own thread before any guest command


def test_hydrate_fails_closed_on_a_working_tree_that_is_not_empty(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    repository, guest = _repository(analysis, tmp_path / 'ws')
    (guest.workspace / 'working_document.md').write_text('written too early\n', 'utf-8')

    with pytest.raises(guest_git.GitError, match='clone'):
        repository.hydrate()


def test_push_all_publishes_what_the_agent_committed_and_did_not_push(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    repository, guest = _repository(analysis, tmp_path / 'ws')
    repository.hydrate()
    tip = _commit(guest, 'notes.md', 'committed, not pushed\n')
    assert guest.run(['git', 'checkout', '-q', '-b', 'side']).ok
    side = _commit(guest, 'side.md', 'on a branch\n')

    repository.push_all(SESSION)

    refs = analysis.store.read().refs
    assert refs[MAIN] == tip
    assert refs['refs/heads/side'] == side
    assert not any(ref.startswith(guest_git.STRANDED_NAMESPACE) for ref in refs)


def test_push_all_with_nothing_unpushed_is_a_no_op(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    repository, _guest = _repository(analysis, tmp_path / 'ws')
    repository.hydrate()
    before = analysis.store.read()

    repository.push_all(SESSION)

    assert analysis.store.read().generation == before.generation


def test_a_refused_teardown_push_strands_the_tips_instead_of_losing_them(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    first, first_guest = _repository(analysis, tmp_path / 'first')
    first.hydrate()
    second, second_guest = _repository(analysis, tmp_path / 'second')
    second.hydrate()
    published = _commit(second_guest, 'notes.md', 'from the second\n')
    assert second_guest.run(['git', 'push', '-q', 'origin', 'HEAD']).ok
    stale = _commit(first_guest, 'other.md', 'from the first\n')

    first.push_all(SESSION)

    refs = analysis.store.read().refs
    assert refs[MAIN] == published  # never rewritten
    assert refs[f'{guest_git.STRANDED_NAMESPACE}/{SESSION}/main'] == stale


def test_a_stranded_push_that_is_refused_too_raises(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    repository, guest = _repository(analysis, tmp_path / 'ws')
    repository.hydrate()
    _commit(guest, 'skills/planted/SKILL.md', 'a protected path refuses every ref, stranded ones included\n')

    with pytest.raises(guest_git.GitError, match='refused twice'):
        repository.push_all(SESSION)


def test_a_session_id_that_cannot_name_a_ref_is_refused(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    repository, _guest = _repository(analysis, tmp_path / 'ws')
    with pytest.raises(ValueError, match='ref'):
        repository.push_all('../refs/heads/main')


def test_a_branch_that_merely_fell_behind_is_not_stranded(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    first, _first_guest = _repository(analysis, tmp_path / 'first')
    first.hydrate()
    second, second_guest = _repository(analysis, tmp_path / 'second')
    second.hydrate()
    _commit(second_guest, 'notes.md', 'from the second\n')
    assert second_guest.run(['git', 'push', '-q', 'origin', 'HEAD']).ok

    first.push_all(SESSION)  # nothing of its own to publish: refs are for ever, so no junk may be left

    assert not any(ref.startswith(guest_git.STRANDED_NAMESPACE) for ref in analysis.store.read().refs)


def test_commits_on_a_detached_head_are_stranded_rather_than_lost(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    repository, guest = _repository(analysis, tmp_path / 'ws')
    repository.hydrate()
    assert guest.run(['git', 'checkout', '-q', '--detach']).ok
    tip = _commit(guest, 'notes.md', 'on no branch\n')

    repository.push_all(SESSION)

    assert analysis.store.read().refs[f'{guest_git.STRANDED_NAMESPACE}/{SESSION}/HEAD'] == tip


def test_a_clean_branch_lands_even_when_another_is_refused(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    first, first_guest = _repository(analysis, tmp_path / 'first')
    first.hydrate()
    second, second_guest = _repository(analysis, tmp_path / 'second')
    second.hydrate()
    _commit(second_guest, 'notes.md', 'from the second\n')
    assert second_guest.run(['git', 'push', '-q', 'origin', 'HEAD']).ok
    stale = _commit(first_guest, 'other.md', 'behind on main\n')
    assert first_guest.run(['git', 'checkout', '-q', '-b', 'side', 'origin/main']).ok
    side = _commit(first_guest, 'side.md', 'clean branch\n')

    first.push_all(SESSION)

    refs = analysis.store.read().refs
    assert refs['refs/heads/side'] == side
    assert refs[f'{guest_git.STRANDED_NAMESPACE}/{SESSION}/main'] == stale
    assert f'{guest_git.STRANDED_NAMESPACE}/{SESSION}/side' not in refs


def test_one_refused_stranded_tip_does_not_take_the_others_down(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    # The hook refuses a push whole, so the stranded tips go one push each: the clean one lands, the one writing
    # a protected path is reported lost.
    repository, guest = _repository(analysis, tmp_path / 'ws')
    repository.hydrate()
    assert guest.run(['git', 'checkout', '-q', '-b', 'side']).ok
    _commit(guest, 'side.md', 'first on side\n')
    assert guest.run(['git', 'push', '-q', 'origin', 'side']).ok
    other, other_guest = _repository(analysis, tmp_path / 'other')
    other.hydrate()
    assert other_guest.run(['git', 'checkout', '-q', 'side']).ok
    _commit(other_guest, 'side.md', 'the store moved on side\n')
    assert other_guest.run(['git', 'push', '-q', 'origin', 'HEAD']).ok
    assert other_guest.run(['git', 'checkout', '-q', 'main']).ok
    _commit(other_guest, 'notes.md', 'and on main\n')
    assert other_guest.run(['git', 'push', '-q', 'origin', 'HEAD']).ok
    stale_side = _commit(guest, 'more.md', 'clean, behind on side\n')
    assert guest.run(['git', 'checkout', '-q', 'main']).ok
    _commit(guest, 'skills/planted/SKILL.md', 'refused by the hook, behind on main\n')

    with pytest.raises(guest_git.GitError, match='refs/heads/main'):
        repository.push_all(SESSION)

    refs = analysis.store.read().refs
    assert refs[f'{guest_git.STRANDED_NAMESPACE}/{SESSION}/side'] == stale_side
    assert f'{guest_git.STRANDED_NAMESPACE}/{SESSION}/main' not in refs


def test_a_hook_refused_sibling_does_not_demote_a_clean_branch(
    analysis: conftest.Analysis, tmp_path: pathlib.Path
) -> None:
    # The hook refuses `--all` whole when one branch writes a protected path; the clean branch is then pushed on
    # its own and lands as a branch, not as a stranded ref.
    repository, guest = _repository(analysis, tmp_path / 'ws')
    repository.hydrate()
    assert guest.run(['git', 'checkout', '-q', '-b', 'side']).ok
    side = _commit(guest, 'side.md', 'clean\n')
    assert guest.run(['git', 'checkout', '-q', 'main']).ok
    _commit(guest, 'skills/planted/SKILL.md', 'refused by the hook\n')

    before = analysis.store.read().refs['refs/heads/main']

    with pytest.raises(guest_git.GitError, match='refs/heads/main'):
        repository.push_all(SESSION)

    refs = analysis.store.read().refs
    assert refs['refs/heads/side'] == side
    assert refs['refs/heads/main'] == before
    assert not any(ref.startswith(guest_git.STRANDED_NAMESPACE) for ref in refs)
