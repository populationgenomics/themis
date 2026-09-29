"""An agent's push through the worker's hatches keeps the fields a widget's schema gives to a user.

The hook runs the content check the worker names (`git_hatches.PROTECTION`), so these pushes are refused or landed by
the same code a session's are. A user's tick reaches the store the way a curator's edit does, published straight to
it, never through the hook; the agent then pulls it, rebasing as the guest's gitconfig makes it, and pushes on top.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Callable

import pytest
from google.protobuf import any_pb2, message, timestamp_pb2

from themis import sheaf
from themis.services.sandbox_worker.guest import widgets
from themis.services.sandbox_worker.tests import conftest, fakes
from themis.sheaf.tests import conftest as sheaf_conftest
from themis.sheaf.wire import reflog
from themis.widgets import asset
from themis.widgets.models import checklist_pb2

MAIN = 'refs/heads/main'
ASSET = 'assets/checklist.binpb'
CURATOR = sheaf_conftest.Author('Curator One', 'curator.one@example.org')
REBASE = 'rebase onto the user'

_Checklist = checklist_pb2.Checklist


def _checklist(*items: tuple[str, bool], label: str = 'confirm') -> _Checklist:
    return _Checklist(
        items=[_Checklist.Item(id=key, label=f'{label} {key}', checked=checked) for key, checked in items]
    )


def _run(guest: fakes.HostGitSandbox, *argv: str) -> str:
    result = guest.run(['git', *argv])
    assert result.ok, result.stderr
    return result.stdout


def _commit(guest: fakes.HostGitSandbox, message: str) -> str:
    _run(guest, 'add', '-A')
    _run(guest, 'commit', '-q', '-m', message)
    return _run(guest, 'rev-parse', 'HEAD').strip()


def _push(guest: fakes.HostGitSandbox) -> tuple[bool, str]:
    pushed = guest.run(['git', 'push', 'origin', 'HEAD:main'])
    return pushed.ok, pushed.stderr


def _write_bytes(guest: fakes.HostGitSandbox, payload: _Checklist, path: str = ASSET) -> None:
    """Write an asset past the helper, as an agent writing the file by other means would."""
    target = guest.workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    target.write_bytes(asset.encode(payload))


def _clone(analysis: conftest.Analysis, workspace: pathlib.Path) -> fakes.HostGitSandbox:
    guest = fakes.HostGitSandbox(workspace)
    guest.workspace.mkdir()
    _run(guest, 'clone', fakes.HostGitSandbox.url(analysis.upload_pack), '.')
    _run(guest, 'remote', 'set-url', '--push', 'origin', fakes.HostGitSandbox.url(analysis.receive_pack))
    return guest


@pytest.fixture
def user_ticks(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> Callable[[_Checklist], None]:
    """Publish a user's version of the checklist straight to the store, as a curator's edit lands."""
    user = sheaf_conftest.GitRepo(analysis.store, tmp_path / 'user.git')

    def tick(payload: _Checklist) -> None:
        user.write_files(ref=MAIN, files={ASSET: asset.encode(payload)}, author=CURATOR, message='tick')

    return tick


@pytest.fixture
def ticked(
    analysis: conftest.Analysis, tmp_path: pathlib.Path, user_ticks: Callable[[_Checklist], None]
) -> fakes.HostGitSandbox:
    """A clone holding a checklist whose item `a` a user ticked after the agent pushed it."""
    guest = _clone(analysis, tmp_path / 'ws')
    widgets.write(ASSET, _checklist(('a', False), ('b', False)), workspace=guest.workspace)
    _commit(guest, 'write the checklist')
    assert _push(guest)[0]
    user_ticks(_checklist(('a', True), ('b', False)))
    _run(guest, 'pull', '-q')
    return guest


def test_flipping_a_user_s_tick_is_refused_with_what_to_do(ticked: fakes.HostGitSandbox) -> None:
    _write_bytes(ticked, _checklist(('a', False), ('b', False)))
    _commit(ticked, 'rebuild the checklist from my notes')

    ok, stderr = _push(ticked)

    assert not ok
    assert f"{ASSET}: items[a].checked is a user's judgement, and the agent's change alters it" in stderr
    assert 'keep the value the user set; to clear a judgement, change what it is about' in stderr


def test_adding_an_item_already_ticked_is_refused(ticked: fakes.HostGitSandbox) -> None:
    _write_bytes(ticked, _checklist(('a', True), ('b', False), ('c', True)))
    _commit(ticked, 'add c')

    ok, stderr = _push(ticked)

    assert not ok
    assert "items[c].checked is a user's judgement, and the agent sets it" in stderr


def test_a_root_commit_carrying_a_tick_is_refused(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    guest = _clone(analysis, tmp_path / 'ws')
    _write_bytes(guest, _checklist(('a', True)))
    _commit(guest, 'a checklist, already ticked')

    ok, stderr = _push(guest)

    assert not ok
    assert "items[a].checked is a user's judgement, and the agent sets it" in stderr


def test_removing_a_ticked_item_then_adding_it_again_unticked_lands(ticked: fakes.HostGitSandbox) -> None:
    _write_bytes(ticked, _checklist(('b', False)))
    _commit(ticked, 'drop a')
    assert _push(ticked)[0]

    _write_bytes(ticked, _checklist(('a', False), ('b', False)))
    _commit(ticked, 'a applies after all')
    ok, stderr = _push(ticked)

    assert ok, stderr


def test_a_version_failing_its_rules_is_still_held_to_the_user_s_judgements(ticked: fakes.HostGitSandbox) -> None:
    wrapped = any_pb2.Any()
    wrapped.Pack(_Checklist(items=[_Checklist.Item(id='a', label='confirm a'), _Checklist.Item(id='b', label='')]))
    (ticked.workspace / ASSET).write_bytes(wrapped.SerializeToString())
    _commit(ticked, 'an invalid checklist')

    ok, stderr = _push(ticked)

    assert not ok
    assert "items[a].checked is a user's judgement" in stderr


def test_moving_a_ticked_asset_starts_it_anew(ticked: fakes.HostGitSandbox) -> None:
    _run(ticked, 'mv', ASSET, 'assets/moved.binpb')
    _commit(ticked, 'move the checklist')

    ok, stderr = _push(ticked)

    assert not ok
    assert "assets/moved.binpb: items[a].checked is a user's judgement, and the agent sets it" in stderr


def test_a_symlink_over_a_ticked_asset_removes_it_and_a_file_back_is_new(ticked: fakes.HostGitSandbox) -> None:
    (ticked.workspace / ASSET).unlink()
    os.symlink('elsewhere.binpb', ticked.workspace / ASSET)
    _commit(ticked, 'point elsewhere')
    ok, stderr = _push(ticked)
    assert ok, stderr

    _write_bytes(ticked, _checklist(('a', True), ('b', False)))
    _commit(ticked, 'the checklist again, as it was')
    ok, stderr = _push(ticked)

    assert not ok
    assert "items[a].checked is a user's judgement, and the agent sets it" in stderr


def test_an_asset_turned_into_another_payload_is_removed_and_an_unknown_widget_refused(
    ticked: fakes.HostGitSandbox,
) -> None:
    wrapped = any_pb2.Any()
    wrapped.Pack(timestamp_pb2.Timestamp(seconds=1))
    (ticked.workspace / ASSET).write_bytes(wrapped.SerializeToString())
    _commit(ticked, 'not a widget any more')
    ok, stderr = _push(ticked)
    assert ok, stderr

    unknown = any_pb2.Any(type_url='type.googleapis.com/themis.widgets.models.Pedigree', value=b'\x08\x01')
    (ticked.workspace / ASSET).write_bytes(unknown.SerializeToString())
    _commit(ticked, 'a widget from another build')
    ok, stderr = _push(ticked)

    assert not ok
    assert 'themis.widgets.models.Pedigree is a widget type this build does not know' in stderr


def _landed(guest: fakes.HostGitSandbox) -> list[tuple[str, str, bool]]:
    landed = asset.decode((guest.workspace / ASSET).read_bytes())
    assert isinstance(landed, _Checklist)
    return [(item.id, item.label, item.checked) for item in landed.items]


def test_the_helper_s_rewrite_keeps_the_tick_on_an_item_it_leaves_alone_and_lands(
    ticked: fakes.HostGitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    widgets.update(
        ASSET,
        lambda _before: _Checklist(
            items=[_Checklist.Item(id='a', label='confirm a'), _Checklist.Item(id='d', label='n')]
        ),
        workspace=ticked.workspace,
    )
    _commit(ticked, 'revise the checklist')

    ok, stderr = _push(ticked)

    assert ok, stderr
    assert _landed(ticked) == [('a', 'confirm a', True), ('d', 'n', False)]
    assert 'cleared' not in capsys.readouterr().err


def test_the_helper_s_rewording_clears_the_tick_says_so_and_lands(
    ticked: fakes.HostGitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    widgets.update(
        ASSET, lambda _before: _checklist(('a', False), ('b', False), label='reworded'), workspace=ticked.workspace
    )
    _commit(ticked, 'reword the checklist')

    ok, stderr = _push(ticked)

    assert ok, stderr
    assert _landed(ticked) == [('a', 'reworded a', False), ('b', 'reworded b', False)]
    assert f'{ASSET}: items[a].checked cleared: its label changed; the user reviews it again' in capsys.readouterr().err


def test_the_helper_s_build_rewording_the_committed_checklist_in_place_clears_the_tick_says_so_and_lands(
    ticked: fakes.HostGitSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    def reword_in_place(before: message.Message | None) -> message.Message:
        assert isinstance(before, _Checklist)
        before.items[0].label = 'reworded a'
        return before

    widgets.update(ASSET, reword_in_place, workspace=ticked.workspace)
    _commit(ticked, 'reword a')

    ok, stderr = _push(ticked)

    assert ok, stderr
    assert _landed(ticked) == [('a', 'reworded a', False), ('b', 'confirm b', False)]
    assert f'{ASSET}: items[a].checked cleared: its label changed; the user reviews it again' in capsys.readouterr().err


def test_the_helper_s_build_unticking_everything_in_place_keeps_the_ticks_it_may_not_clear(
    analysis: conftest.Analysis,
    tmp_path: pathlib.Path,
    user_ticks: Callable[[_Checklist], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    guest = _clone(analysis, tmp_path / 'ws')
    widgets.write(ASSET, _checklist(('a', False), ('b', False)), workspace=guest.workspace)
    _commit(guest, 'write the checklist')
    assert _push(guest)[0]
    user_ticks(_checklist(('a', True), ('b', True)))
    _run(guest, 'pull', '-q')

    def build(before: message.Message | None) -> message.Message:
        assert isinstance(before, _Checklist)
        for item in before.items:
            item.checked = False
        before.items[0].label = 'reworded a'
        return before

    widgets.update(ASSET, build, workspace=guest.workspace)
    _commit(guest, 'reword a')

    ok, stderr = _push(guest)

    assert ok, stderr
    assert _landed(guest) == [('a', 'reworded a', False), ('b', 'confirm b', True)]
    assert capsys.readouterr().err.count('cleared') == 1


def test_a_reworded_item_keeping_its_tick_is_refused(ticked: fakes.HostGitSandbox) -> None:
    _write_bytes(ticked, _checklist(('a', True), ('b', False), label='reworded'))
    _commit(ticked, 'reword the checklist')

    ok, stderr = _push(ticked)

    assert not ok
    assert f"{ASSET}: items[a].checked is a user's judgement on what the agent's change alters" in stderr
    assert "the change to label clears the user's judgement; leave the guard at its default" in stderr


def test_a_reworded_item_whose_tick_is_cleared_lands(ticked: fakes.HostGitSandbox) -> None:
    _write_bytes(ticked, _checklist(('a', False), ('b', False), label='reworded'))
    _commit(ticked, 'reword the checklist')

    ok, stderr = _push(ticked)

    assert ok, stderr
    assert _landed(ticked) == [('a', 'reworded a', False), ('b', 'reworded b', False)]


def test_removing_a_ticked_item_then_adding_it_again_through_the_helper_before_pushing_lands(
    ticked: fakes.HostGitSandbox,
) -> None:
    widgets.write(ASSET, _checklist(('b', False)), workspace=ticked.workspace)
    _commit(ticked, 'drop a')
    widgets.write(ASSET, _checklist(('a', False), ('b', False)), workspace=ticked.workspace)
    _commit(ticked, 'a applies after all')

    ok, stderr = _push(ticked)

    assert ok, stderr


def test_the_helper_refuses_while_a_commit_the_push_carries_undoes_a_tick(ticked: fakes.HostGitSandbox) -> None:
    _write_bytes(ticked, _checklist(('a', False), ('b', False)))
    flipped = _commit(ticked, 'rebuild the checklist from my notes')

    with pytest.raises(asset.AssetError, match=rf'(?s)in commit {flipped[:12]}, items\[a\]\.checked.*git reset'):
        widgets.update(ASSET, lambda before: before or _Checklist(), workspace=ticked.workspace)


def _relabelled_b(*, checked_a: bool) -> _Checklist:
    return _Checklist(
        items=[
            _Checklist.Item(id='a', label='confirm a', checked=checked_a),
            _Checklist.Item(id='b', label='relabelled b'),
        ]
    )


@pytest.fixture
def diverged(
    analysis: conftest.Analysis, tmp_path: pathlib.Path, user_ticks: Callable[[_Checklist], None]
) -> fakes.HostGitSandbox:
    """The agent relabelled item `b` locally while the user ticked `a` on the version it started from."""
    guest = _clone(analysis, tmp_path / 'ws')
    widgets.write(ASSET, _checklist(('a', False), ('b', False)), workspace=guest.workspace)
    _commit(guest, 'write the checklist')
    assert _push(guest)[0]
    widgets.write(ASSET, _relabelled_b(checked_a=False), workspace=guest.workspace)
    _commit(guest, 'relabel')
    user_ticks(_checklist(('a', True), ('b', False)))
    _run(guest, 'fetch', '-q')
    return guest


def test_a_rebase_onto_the_user_s_tick_lands(diverged: fakes.HostGitSandbox) -> None:
    rebased = diverged.run(['git', 'pull'])
    assert not rebased.ok  # both sides changed the binary asset
    widgets.update(ASSET, lambda _before: _relabelled_b(checked_a=False), workspace=diverged.workspace)
    _run(diverged, 'add', ASSET)
    _run(diverged, '-c', 'core.editor=true', 'rebase', '--continue')

    ok, stderr = _push(diverged)

    assert ok, stderr
    assert _landed(diverged) == [('a', 'confirm a', True), ('b', 'relabelled b', False)]


@pytest.mark.parametrize('side', ['--theirs', '--ours'])
def test_a_merge_that_takes_in_the_user_s_edit_is_refused_whichever_side_it_keeps(
    diverged: fakes.HostGitSandbox, side: str
) -> None:
    merged = diverged.run(['git', 'merge', 'origin/main'])
    assert not merged.ok
    with pytest.raises(asset.AssetError, match='rebase onto the user'):
        widgets.update(ASSET, lambda before: before or _Checklist(), workspace=diverged.workspace)
    _run(diverged, 'checkout', side, ASSET)
    _run(diverged, 'add', ASSET)
    _run(diverged, 'commit', '-q', '--no-edit')

    ok, stderr = _push(diverged)

    assert not ok
    assert REBASE in stderr


def test_a_merge_keeping_the_agent_s_side_by_strategy_is_refused(diverged: fakes.HostGitSandbox) -> None:
    _run(diverged, 'merge', '-q', '-s', 'ours', '--no-edit', 'origin/main')

    ok, stderr = _push(diverged)

    assert not ok
    assert "items[a].checked is a user's judgement" in stderr
    assert REBASE in stderr


def test_a_crafted_merge_restoring_the_user_s_earlier_tick_is_refused(
    ticked: fakes.HostGitSandbox, user_ticks: Callable[[_Checklist], None]
) -> None:
    ticked_once = _run(ticked, 'rev-parse', 'HEAD').strip()
    user_ticks(_checklist(('a', False), ('b', False)))
    _run(ticked, 'pull', '-q')
    unticked = _run(ticked, 'rev-parse', 'HEAD').strip()
    tree = _run(ticked, 'rev-parse', f'{ticked_once}^{{tree}}').strip()
    merge = _run(ticked, 'commit-tree', tree, '-p', unticked, '-p', ticked_once, '-m', 'merge').strip()
    _run(ticked, 'reset', '-q', '--hard', merge)

    ok, stderr = _push(ticked)

    assert not ok
    assert "items[a].checked is a user's judgement" in stderr


def _relabelled_tree(guest: fakes.HostGitSandbox, commit: str) -> str:
    """The tree of `commit` with its checklist relabelled and its ticks as they are, built off the branch."""
    branch = _run(guest, 'symbolic-ref', '--short', 'HEAD').strip()
    _run(guest, 'checkout', '-q', '--detach', commit)
    _write_bytes(guest, _checklist(('a', True), ('b', False), label='relabelled'))
    _run(guest, 'add', ASSET)
    tree = _run(guest, 'write-tree').strip()
    _run(guest, 'checkout', '-q', '-f', branch)
    return tree


@pytest.fixture
def removed(ticked: fakes.HostGitSandbox) -> tuple[fakes.HostGitSandbox, str, str]:
    """The agent removed the checklist the user ticked, and pushed: the clone, the ticked commit, the removal."""
    had_it = _run(ticked, 'rev-parse', 'HEAD').strip()
    _run(ticked, 'rm', '-q', ASSET)
    gone = _commit(ticked, 'remove the checklist')
    assert _push(ticked)[0]
    return ticked, had_it, gone


def test_a_crafted_merge_restoring_a_removed_ticked_asset_is_refused(
    removed: tuple[fakes.HostGitSandbox, str, str],
) -> None:
    guest, had_it, gone = removed
    tree = _run(guest, 'rev-parse', f'{had_it}^{{tree}}').strip()
    merge = _run(guest, 'commit-tree', tree, '-p', gone, '-p', had_it, '-m', 'bring it back').strip()
    _run(guest, 'reset', '-q', '--hard', merge)

    ok, stderr = _push(guest)

    assert not ok
    assert "items[a].checked is a user's judgement, and the agent sets it" in stderr


def test_a_crafted_merge_bringing_back_a_removed_ticked_asset_rewritten_is_refused(
    removed: tuple[fakes.HostGitSandbox, str, str],
) -> None:
    guest, had_it, gone = removed
    tree = _relabelled_tree(guest, had_it)
    merge = _run(guest, 'commit-tree', tree, '-p', gone, '-p', had_it, '-m', 'bring it back').strip()
    _run(guest, 'reset', '-q', '--hard', merge)

    ok, stderr = _push(guest)

    assert not ok
    assert "items[a].checked is a user's judgement, and the agent sets it" in stderr


def test_an_octopus_merge_holding_a_parent_without_the_asset_is_refused(
    removed: tuple[fakes.HostGitSandbox, str, str],
) -> None:
    guest, had_it, gone = removed
    (guest.workspace / 'notes.md').write_text('notes\n', 'utf-8')
    other = _commit(guest, 'notes')
    tree = _relabelled_tree(guest, had_it)
    merge = _run(guest, 'commit-tree', tree, '-p', had_it, '-p', other, '-p', gone, '-m', 'octopus').strip()
    _run(guest, 'reset', '-q', '--hard', merge)

    ok, stderr = _push(guest)

    assert not ok
    assert "items[a].checked is a user's judgement, and the agent sets it" in stderr
    assert REBASE in stderr


def _disguised(guest: fakes.HostGitSandbox) -> tuple[str, str]:
    """Two children of the ticked tip: `decoy` leaves the checklist as it is, `main` undoes the tick.

    Returns the decoy commit and the forged one, `main` left checked out.
    """
    _run(guest, 'checkout', '-q', '-b', 'decoy')
    (guest.workspace / 'notes.md').write_text('notes\n', 'utf-8')
    decoy = _commit(guest, 'take notes')
    _run(guest, 'checkout', '-q', 'main')
    _write_bytes(guest, _checklist(('a', False), ('b', False)))
    return decoy, _commit(guest, 'rebuild the checklist from my notes')


def test_a_pushed_replace_ref_cannot_disguise_a_forged_tick(ticked: fakes.HostGitSandbox) -> None:
    decoy, forged = _disguised(ticked)

    pushed = ticked.run(['git', 'push', 'origin', 'HEAD:main', f'{decoy}:refs/replace/{forged}'])

    assert not pushed.ok
    assert f'refs/replace/{forged} is not a ref a push may write' in pushed.stderr
    assert "items[a].checked is a user's judgement" in pushed.stderr


def test_a_replace_ref_in_the_mirror_does_not_change_what_the_content_check_reads(
    analysis: conftest.Analysis, ticked: fakes.HostGitSandbox
) -> None:
    """The mirror holds a replace ref standing the decoy in for the forged commit; the push carries none.

    Published straight to the store, as a writer that never meets the hook could, so the sync before the push writes
    it into the mirror. The check reads the objects the store keeps, and refuses.
    """
    decoy, forged = _disguised(ticked)
    _run(ticked, 'push', '-q', 'origin', 'decoy')
    mirror = analysis.hatches.mirror
    base = mirror.sync()
    replace_ref = f'refs/replace/{forged}'
    entry = reflog.record(mirror.git, base.tip(reflog.REF), [reflog.Transition(replace_ref, None, decoy)])
    analysis.store.publish(
        base,
        sheaf.Intent(
            ref_updates={
                replace_ref: sheaf.RefUpdate(None, decoy),
                reflog.REF: sheaf.RefUpdate(base.tip(reflog.REF), entry),
            },
            packs=[mirror.pack_for([entry])],
        ),
    )
    mirror.sync()
    assert mirror.local_refs()[replace_ref] == decoy

    ok, stderr = _push(ticked)

    assert not ok
    assert f"{ASSET}: items[a].checked is a user's judgement" in stderr


def test_the_agent_s_own_flows_land(analysis: conftest.Analysis, tmp_path: pathlib.Path) -> None:
    guest = _clone(analysis, tmp_path / 'ws')
    widgets.write(ASSET, _checklist(('a', False)), workspace=guest.workspace)
    (guest.workspace / 'notes.md').write_text('notes\n', 'utf-8')
    _commit(guest, 'first')
    assert _push(guest)[0]
    widgets.update(ASSET, lambda _before: _checklist(('a', False), ('b', False)), workspace=guest.workspace)
    (guest.workspace / 'other.bin').write_bytes(b'\x00\x01 not an asset')
    _commit(guest, 'more')
    ok, stderr = _push(guest)
    assert ok, stderr
