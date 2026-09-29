"""The guest's asset helper: what it writes draws, and what it refuses it does not write at all."""

from __future__ import annotations

import pathlib
import subprocess

import pytest
from google.protobuf import message, message_factory, timestamp_pb2

from themis.services.sandbox_worker.guest import widgets
from themis.widgets import asset, tracking
from themis.widgets.models import checklist_pb2
from themis.widgets.tests import ownership_cases

_Checklist = checklist_pb2.Checklist
_ONE_ITEM = _Checklist(items=[_Checklist.Item(id='a', label='x')])


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    subprocess.run(['git', '-C', str(tmp_path), 'init', '--quiet'], check=True)  # noqa: S603, S607
    (tmp_path / '.gitignore').write_text('scratch/\n', 'utf-8')
    return tmp_path


def _written(workspace: pathlib.Path) -> list[pathlib.Path]:
    return sorted(path for path in workspace.iterdir() if path.name not in {'.git', '.gitignore'})


def test_write_lands_an_asset_decode_reads_back(workspace: pathlib.Path) -> None:
    payload = _Checklist(items=[_Checklist.Item(id='ps3', label='Functional studies reviewed')])
    written = widgets.write('assets/nested/checklist.binpb', payload, workspace=workspace)
    assert written == workspace / 'assets' / 'nested' / 'checklist.binpb'
    assert asset.decode(written.read_bytes()) == payload


@pytest.mark.parametrize('path', ['../escape.binpb', '/workspace/a.binpb', 'a b.binpb', 'assets/', 'scratch/a.binpb'])
def test_write_refuses_a_path_no_embed_would_draw(workspace: pathlib.Path, path: str) -> None:
    with pytest.raises(asset.AssetError):
        widgets.write(path, _ONE_ITEM, workspace=workspace)
    assert _written(workspace) == []


@pytest.mark.parametrize(
    'payload',
    [_Checklist(), _Checklist(items=[_Checklist.Item(id='a', label='x')] * 2), timestamp_pb2.Timestamp(seconds=1)],
    ids=['no items', 'duplicate ids', 'not a payload type'],
)
def test_write_refuses_a_payload_that_would_not_draw(
    workspace: pathlib.Path, payload: checklist_pb2.Checklist | timestamp_pb2.Timestamp
) -> None:
    with pytest.raises(asset.AssetError):
        widgets.write('assets/checklist.binpb', payload, workspace=workspace)
    assert _written(workspace) == []


def test_write_outside_a_repository_fails_loudly(tmp_path: pathlib.Path) -> None:
    with pytest.raises(tracking.TrackingError):
        widgets.write('assets/checklist.binpb', _ONE_ITEM, workspace=tmp_path)


def _commit(workspace: pathlib.Path, path: str) -> None:
    for argv in (['add', path], ['-c', 'user.name=u', '-c', 'user.email=u@x', 'commit', '-qm', 'commit']):
        subprocess.run(['git', '-C', str(workspace), *argv], check=True, capture_output=True)  # noqa: S603, S607


def _ticked(workspace: pathlib.Path) -> None:
    """The last commit's checklist holds item `a` a user ticked."""
    target = workspace / 'assets' / 'checklist.binpb'
    target.parent.mkdir(parents=True)
    target.write_bytes(asset.encode(_Checklist(items=[_Checklist.Item(id='a', label='x', checked=True)])))
    _commit(workspace, 'assets/checklist.binpb')


def test_write_refuses_to_undo_a_tick_the_last_commit_holds(workspace: pathlib.Path) -> None:
    _ticked(workspace)
    with pytest.raises(asset.AssetError, match=r"(?s)items\[a\]\.checked is a user's judgement.*`update`"):
        widgets.write('assets/checklist.binpb', _ONE_ITEM, workspace=workspace)


def test_update_carries_the_user_s_ticks_across_by_key(
    workspace: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ticked(workspace)
    seen: list[object] = []

    def rebuild(before: object) -> _Checklist:
        seen.append(before)
        return _Checklist(items=[_Checklist.Item(id='b', label='new'), _Checklist.Item(id='a', label='x')])

    written = widgets.update('assets/checklist.binpb', rebuild, workspace=workspace)

    landed = asset.decode(written.read_bytes())
    assert isinstance(landed, _Checklist)
    assert [(item.id, item.label, item.checked) for item in landed.items] == [
        ('b', 'new', False),
        ('a', 'x', True),
    ]
    assert isinstance(seen[0], _Checklist)
    assert capsys.readouterr().err == ''


def test_update_clears_the_tick_on_a_reworded_item_and_reports_it(
    workspace: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ticked(workspace)

    written = widgets.update(
        'assets/checklist.binpb',
        lambda _before: _Checklist(items=[_Checklist.Item(id='a', label='reworded')]),
        workspace=workspace,
    )

    landed = asset.decode(written.read_bytes())
    assert isinstance(landed, _Checklist)
    assert [(item.id, item.label, item.checked) for item in landed.items] == [('a', 'reworded', False)]
    assert capsys.readouterr().err == (
        '[themis.agent.widgets] assets/checklist.binpb: items[a].checked cleared: its label changed; '
        'the user reviews it again\n'
    )


def test_update_refuses_a_tick_the_agent_sets_itself(workspace: pathlib.Path) -> None:
    _ticked(workspace)
    with pytest.raises(asset.AssetError, match=r'items\[b\]\.checked'):
        widgets.update(
            'assets/checklist.binpb',
            lambda _before: _Checklist(items=[_Checklist.Item(id='b', label='x', checked=True)]),
            workspace=workspace,
        )


def test_update_refuses_while_a_merge_is_in_progress(workspace: pathlib.Path) -> None:
    _ticked(workspace)
    head = subprocess.run(  # noqa: S603
        ['git', '-C', str(workspace), 'rev-parse', 'HEAD'],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (workspace / '.git' / 'MERGE_HEAD').write_text(f'{head}\n', 'utf-8')
    with pytest.raises(asset.AssetError, match='git pull --rebase'):
        widgets.update('assets/checklist.binpb', lambda before: before or _ONE_ITEM, workspace=workspace)


def _another_payload() -> message.Message:
    """A payload of a second marked type, as the shared ownership cases declare it."""
    tree = message_factory.GetMessageClass(ownership_cases.pool().FindMessageTypeByName('themis.widgets.cases.Tree'))()
    tree.title = 'a tree'  # pyright: ignore[reportAttributeAccessIssue]
    return tree


def test_update_reports_every_tick_an_asset_of_another_type_clears(
    workspace: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ticked(workspace)

    widgets.update('assets/checklist.binpb', lambda _before: _another_payload(), workspace=workspace)

    assert capsys.readouterr().err == (
        '[themis.agent.widgets] assets/checklist.binpb: items[a].checked cleared: the asset is now another payload '
        'type\n'
    )


def test_update_reports_a_tick_on_an_item_it_leaves_out(
    workspace: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ticked(workspace)

    widgets.update(
        'assets/checklist.binpb',
        lambda _before: _Checklist(items=[_Checklist.Item(id='b', label='another')]),
        workspace=workspace,
    )

    assert capsys.readouterr().err == (
        '[themis.agent.widgets] assets/checklist.binpb: items[a].checked cleared: its element was removed\n'
    )
