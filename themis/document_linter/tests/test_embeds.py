"""The linter's reading of ``::embed``, and its check of each against the file it names.

The grammar cases are the ones the browser's renderer is tested on (apps/web/src/components/workbench/
directives.test.tsx): the linter has to find the embeds the browser draws and read the same path from each, or
it passes a document the browser draws differently; every other line opening with ``::embed`` it reports.
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import subprocess
import sys

import pytest
from google.protobuf import any_pb2, timestamp_pb2

from themis.document_linter import __main__ as cli
from themis.document_linter import embeds, linter
from themis.services.sandbox_worker.guest import widgets
from themis.widgets.models import checklist_pb2

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_GRAMMAR = _REPO_ROOT / 'apps' / 'web' / 'src' / 'widgets' / 'embed-grammar.test-support.json'
_CASES = json.loads(_GRAMMAR.read_text('utf-8'))['cases']
_ASSET = 'assets/checklist.binpb'
# Characters a label may carry that markdown or the directive grammar gives a meaning to.
_ALPHABET = [*'aZ09./-_*`[]\\{}"\'<>:@!#~|()& ', 'www.', 'http://', '\\]', '\\[']

_Checklist = checklist_pb2.Checklist


@pytest.mark.parametrize('case', _CASES, ids=[case['name'] for case in _CASES])
def test_the_linter_reads_the_embeds_the_browser_draws(case: dict[str, object]) -> None:
    reading = embeds.read(str(case['markdown']))
    assert [embed.path for embed in reading.embeds] == case['embeds']
    assert len(reading.stray) == case['stray']


def test_the_linter_reads_any_label_and_draws_only_the_label_it_scanned() -> None:
    generator = random.Random(20_260_925)  # noqa: S311 — a fixed seed, so a failing label reproduces
    for _ in range(3_000):
        label = ''.join(generator.choice(_ALPHABET) for _ in range(generator.randint(1, 12)))
        markdown = f'::embed[{label}]'
        reading = embeds.read(markdown)
        assert len(reading.embeds) + len(reading.stray) == 1, markdown
        for embed in reading.embeds:
            assert markdown == f'::embed[{embed.path}]'


def _git(workspace: pathlib.Path, *args: str) -> None:
    subprocess.run(['git', '-C', str(workspace), *args], check=True, capture_output=True)  # noqa: S603, S607


@pytest.fixture
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    _git(tmp_path, 'init', '--quiet')
    (tmp_path / '.gitignore').write_text('scratch/\n', 'utf-8')
    widgets.write(_ASSET, _Checklist(items=[_Checklist.Item(id='ps3', label='Reviewed')]), workspace=tmp_path)
    _git(tmp_path, 'add', _ASSET)
    return tmp_path


def _document(workspace: pathlib.Path, body: str) -> pathlib.Path:
    document = workspace / 'working_document.md'
    document.write_text(f'# Title\n\n{body}\n', 'utf-8')
    return document


def test_an_embed_of_a_tracked_asset_the_helper_wrote_draws(workspace: pathlib.Path) -> None:
    assert linter.lint_document(_document(workspace, f'::embed[{_ASSET}]')) == []


def test_an_embed_resolves_against_the_document_s_own_directory(workspace: pathlib.Path) -> None:
    nested = workspace / 'threads' / 'working_document.md'
    nested.parent.mkdir()
    widgets.write('threads/assets/x.binpb', _Checklist(items=[_Checklist.Item(id='a', label='x')]), workspace=workspace)
    _git(workspace, 'add', 'threads/assets/x.binpb')
    nested.write_text('# Title\n\n::embed[assets/x.binpb]\n', 'utf-8')
    assert linter.lint_document(nested) == []


def _packed(payload: checklist_pb2.Checklist | timestamp_pb2.Timestamp) -> bytes:
    wrapped = any_pb2.Any()
    wrapped.Pack(payload)
    return wrapped.SerializeToString()


@pytest.mark.parametrize(
    ('files', 'directive', 'reason'),
    [
        ({}, '::embed[assets/missing.binpb]', 'assets/missing.binpb does not exist'),
        ({}, '::embed[assets/checklist.binpb/inner]', 'assets/checklist.binpb is not a directory'),
        ({}, '::embed[assets]', 'assets is not a file'),
        ({}, '::embed[../outside.binpb]', 'segment'),
        ({}, '::embed[a*b*.binpb]', 'character outside'),
        ({}, '::embed[]', 'names no path'),
        ({}, '::embed[assets/checklist.binpb]{x=1}', 'written exactly as ::embed[<path>]'),
        ({}, '::embed[assets/checklist.binpb]{x="}"}', 'written exactly as ::embed[<path>]'),
        ({}, '- ::embed[assets/checklist.binpb]', 'top level of the document'),
        ({'notes.md': b'# notes\n'}, '::embed[notes.md]', 'not a serialized google.protobuf.Any'),
        ({'time.binpb': _packed(timestamp_pb2.Timestamp(seconds=1))}, '::embed[time.binpb]', 'not a widget payload'),
        ({'empty.binpb': _packed(_Checklist())}, '::embed[empty.binpb]', 'fails its rules'),
        (
            {'new.binpb': _packed(_Checklist(items=[_Checklist.Item(id='a', label='x')]))},
            '::embed[new.binpb]',
            'git add',
        ),
    ],
)
def test_an_embed_that_would_not_draw_is_reported_with_its_line_and_reason(
    workspace: pathlib.Path, files: dict[str, bytes], directive: str, reason: str
) -> None:
    for name, data in files.items():
        (workspace / name).write_bytes(data)
    [issue] = linter.lint_document(_document(workspace, f'Some prose.\n\n{directive}'))
    assert issue.startswith('line 5: ')
    assert reason in issue


def test_an_asset_git_ignores_is_reported(workspace: pathlib.Path) -> None:
    (workspace / 'scratch').mkdir()
    (workspace / 'scratch' / 'c.binpb').write_bytes((workspace / _ASSET).read_bytes())
    [issue] = linter.lint_document(_document(workspace, '::embed[scratch/c.binpb]'))
    assert 'ignored by git' in issue


def test_an_embed_through_a_symbolic_link_is_reported(workspace: pathlib.Path) -> None:
    os.symlink(workspace / 'assets', workspace / 'linked')
    [issue] = linter.lint_document(_document(workspace, '::embed[linked/checklist.binpb]'))
    assert 'linked is a symbolic link' in issue


def test_an_embed_in_a_fenced_block_is_not_checked(workspace: pathlib.Path) -> None:
    assert linter.lint_document(_document(workspace, '```\n::embed[assets/missing.binpb]\n```')) == []


def test_a_document_outside_a_repository_reports_its_embeds(tmp_path: pathlib.Path) -> None:
    (tmp_path / 'a.binpb').write_bytes(_packed(_Checklist(items=[_Checklist.Item(id='a', label='x')])))
    [issue] = linter.lint_document(_document(tmp_path, '::embed[a.binpb]'))
    assert 'cannot be checked against git' in issue


def test_the_command_exits_non_zero_and_names_each_issue(
    workspace: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    document = _document(workspace, f'::embed[{_ASSET}]\n\n::embed[assets/missing.binpb]')
    monkeypatch.setattr(sys, 'argv', ['themis.document_linter', str(document)])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 1
    assert 'assets/missing.binpb does not exist' in capsys.readouterr().err


def test_an_asset_that_undoes_a_committed_tick_is_reported(workspace: pathlib.Path) -> None:
    ticked = _Checklist(items=[_Checklist.Item(id='ps3', label='Reviewed', checked=True)])
    (workspace / _ASSET).write_bytes(_packed(ticked))
    _git(workspace, 'add', _ASSET)
    _git(workspace, '-c', 'user.name=u', '-c', 'user.email=u@x', 'commit', '-qm', 'tick')
    (workspace / _ASSET).write_bytes(_packed(_Checklist(items=[_Checklist.Item(id='ps3', label='Reviewed')])))
    _git(workspace, 'add', _ASSET)

    [issue] = linter.lint_document(_document(workspace, f'::embed[{_ASSET}]'))

    assert "does not keep the user's judgements" in issue
    assert 'items[ps3].checked' in issue


def test_a_violating_local_commit_is_still_reported_against_the_pushed_tip(
    workspace: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    remote = tmp_path / 'remote.git'
    _git(tmp_path, 'init', '--bare', '--quiet', str(remote))
    ticked = _Checklist(items=[_Checklist.Item(id='ps3', label='Reviewed', checked=True)])
    (workspace / _ASSET).write_bytes(_packed(ticked))
    _git(workspace, 'add', _ASSET)
    _git(workspace, '-c', 'user.name=u', '-c', 'user.email=u@x', 'commit', '-qm', 'tick')
    _git(workspace, 'remote', 'add', 'origin', str(remote))
    _git(workspace, 'push', '-q', '-u', 'origin', 'HEAD:main')
    (workspace / _ASSET).write_bytes(_packed(_Checklist(items=[_Checklist.Item(id='ps3', label='Reviewed')])))
    _git(workspace, '-c', 'user.name=u', '-c', 'user.email=u@x', 'commit', '-qam', 'untick, locally')

    [issue] = linter.lint_document(_document(workspace, f'::embed[{_ASSET}]'))

    assert 'items[ps3].checked' in issue


def test_a_merge_in_progress_is_reported_with_the_rebase_guidance(workspace: pathlib.Path) -> None:
    _git(workspace, '-c', 'user.name=u', '-c', 'user.email=u@x', 'commit', '-qm', 'first')
    head = subprocess.run(  # noqa: S603
        ['git', '-C', str(workspace), 'rev-parse', 'HEAD'],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (workspace / '.git' / 'MERGE_HEAD').write_text(f'{head}\n', 'utf-8')

    [issue] = linter.lint_document(_document(workspace, f'::embed[{_ASSET}]'))

    assert 'git pull --rebase' in issue
