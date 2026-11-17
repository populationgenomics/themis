"""Tests for ``tools.screen.regex_screen`` through its CLI entry point.

Test data uses the synthetic ``TST`` prefix so fixture content never resembles
an internal identifier on the public mirror or trips the screen on this file.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from collections.abc import Mapping

import pytest

from tools.screen import regex_screen

_DEFAULT_PATTERNS = '- name: tst\n  regex: \\bTST\\d+\\b\n'
_ISOLATED_GIT_ENV = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ['git', '-C', str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        env=_ISOLATED_GIT_ENV,
    ).stdout.strip()


def _commit_files(repo: pathlib.Path, files: Mapping[str, str | bytes], message: str) -> str:
    for rel_path, content in files.items():
        dest = repo / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            dest.write_bytes(content)
        else:
            dest.write_text(content, 'utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '--allow-empty', '-m', message)
    return _git(repo, 'rev-parse', 'HEAD')


def _init_repo(repo: pathlib.Path, base_files: Mapping[str, str | bytes] | None = None) -> str:
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 'test@example.com')
    _git(repo, 'config', 'user.name', 'test')
    return _commit_files(repo, base_files or {'README.md': '# Base\n'}, 'base')


def _screen(
    monkeypatch: pytest.MonkeyPatch,
    repo: pathlib.Path,
    *,
    base: str,
    head: str,
    patterns_yaml: str = _DEFAULT_PATTERNS,
) -> int:
    patterns = repo / 'patterns.yaml'
    patterns.write_text(patterns_yaml, 'utf-8')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', os.devnull)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        sys,
        'argv',
        ['regex_screen', '--base', base, '--head', head, '--patterns', str(patterns)],
    )
    return regex_screen.main()


def test_clean_diff_exits_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _init_repo(tmp_path, {'src/app.py': 'def hello():\n    return "ok"\n'})
    head = _commit_files(tmp_path, {'src/app.py': 'def hello():\n    return "still clean"\n'}, 'head')

    exit_code = _screen(monkeypatch, tmp_path, base=base, head=head)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == 'screen/regex: no matches\n'
    assert captured.err == ''


def test_unsuppressed_match_reports_file_line_and_pattern(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _init_repo(tmp_path, {'src/app.py': 'def hello():\n    return 1\n'})
    head = _commit_files(
        tmp_path,
        {'src/app.py': 'def hello():\n    return 1\n\nPARTICIPANT = "TST123"\n'},
        'head',
    )

    exit_code = _screen(monkeypatch, tmp_path, base=base, head=head)

    assert exit_code == 1
    assert capsys.readouterr().err.splitlines() == [
        'screen/regex: 1 unsuppressed match(es):',
        '  src/app.py:4 [tst] PARTICIPANT = "TST123"',
    ]


@pytest.mark.parametrize(
    'line',
    [
        'x = "TST123"  # screen-ignore: deliberate test fixture',
        'x = "TST123"  // screen-ignore: deliberate test fixture',
        'x = "TST123"  /* screen-ignore: deliberate test fixture */',
        'x = "TST123"  <!-- screen-ignore: deliberate test fixture -->',
        'x = "TST123"  #screen-ignore:reason',
    ],
)
def test_inline_suppression_comment_suppresses_match(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], line: str
) -> None:
    base = _init_repo(tmp_path)
    head = _commit_files(tmp_path, {'src/app.py': f'{line}\n'}, 'head')

    exit_code = _screen(monkeypatch, tmp_path, base=base, head=head)

    assert exit_code == 0
    assert capsys.readouterr().out == 'screen/regex: no matches\n'


@pytest.mark.parametrize(
    'line',
    [
        'x = "TST123"',
        'x = "TST123"  # just a normal comment',
        'x = "TST123"  # screen-ignore:',
        'x = "TST123"  # screen-ignore:   ',
        'x = "TST123"  # screen-ignore mentioned in prose without colon',
    ],
)
def test_malformed_or_missing_suppression_comment_still_flags_match(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], line: str
) -> None:
    base = _init_repo(tmp_path)
    head = _commit_files(tmp_path, {'src/app.py': f'{line}\n'}, 'head')

    exit_code = _screen(monkeypatch, tmp_path, base=base, head=head)

    assert exit_code == 1
    assert f'  src/app.py:1 [tst] {line}' in capsys.readouterr().err.splitlines()


def test_suppressed_line_is_omitted_while_unsuppressed_sibling_is_reported(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _init_repo(tmp_path, {'src/app.py': 'def hello():\n    return 1\n'})
    head = _commit_files(
        tmp_path,
        {
            'src/app.py': (
                'def hello():\n'
                '    return 1\n'
                '\n'
                'PARTICIPANT = "TST123"\n'
                'FIXTURE_ID = "TST456"  # screen-ignore: deliberate test fixture\n'
            )
        },
        'head',
    )

    exit_code = _screen(monkeypatch, tmp_path, base=base, head=head)

    assert exit_code == 1
    assert capsys.readouterr().err.splitlines() == [
        'screen/regex: 1 unsuppressed match(es):',
        '  src/app.py:4 [tst] PARTICIPANT = "TST123"',
    ]


def test_multi_file_multi_hunk_and_deleted_file_diffs_report_exact_added_lines(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _init_repo(
        tmp_path,
        {
            'src/app.py': 'def first():\n    return 1\n\ndef second():\n    return 2\n',
            'src/util.py': 'def add(a, b):\n    return a + b\n',
            'src/removed.py': 'OLD = "TST999"\n',
        },
    )
    (tmp_path / 'src' / 'removed.py').unlink()
    head = _commit_files(
        tmp_path,
        {
            'src/app.py': (
                'def first():\n    a = "TST111"\n    return a\n\ndef second():\n    b = "TST222"\n    return b\n'
            ),
            'src/new_file.py': 'NEW_FILE_ID = "TST444"\n',
            'src/util.py': 'def add(a, b):\n    # TST333\n    return a + b\n',
        },
        'head',
    )

    exit_code = _screen(monkeypatch, tmp_path, base=base, head=head)

    assert exit_code == 1
    assert capsys.readouterr().err.splitlines() == [
        'screen/regex: 4 unsuppressed match(es):',
        '  src/app.py:2 [tst]     a = "TST111"',
        '  src/app.py:6 [tst]     b = "TST222"',
        '  src/new_file.py:1 [tst] NEW_FILE_ID = "TST444"',
        '  src/util.py:2 [tst]     # TST333',
    ]


def test_renamed_file_attributes_hit_to_new_path(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = 'def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n'
    base = _init_repo(tmp_path, {'src/util.py': body})
    _git(tmp_path, 'mv', 'src/util.py', 'src/utils.py')
    head = _commit_files(tmp_path, {'src/utils.py': f'{body}\nPARTICIPANT = "TST789"\n'}, 'rename')

    exit_code = _screen(monkeypatch, tmp_path, base=base, head=head)

    assert exit_code == 1
    assert capsys.readouterr().err.splitlines() == [
        'screen/regex: 1 unsuppressed match(es):',
        '  src/utils.py:7 [tst] PARTICIPANT = "TST789"',
    ]


def test_binary_file_in_diff_is_ignored(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _init_repo(tmp_path)
    head = _commit_files(tmp_path, {'src/blob.bin': b'\x00\x89PNG\r\n\x1a\nTST123\x00'}, 'binary')

    assert _screen(monkeypatch, tmp_path, base=base, head=head) == 0


def test_non_utf8_bytes_in_text_diff_are_decoded_without_crashing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A text file carrying cp1252 smart-quote bytes (0x93 / 0x94) — git diffs it as text, so a strict
    # UTF-8 decode would raise UnicodeDecodeError instead of scanning the ASCII identifier beside them.
    base = _init_repo(tmp_path)
    head = _commit_files(tmp_path, {'src/f.txt': b'a smart \x93quote\x94 for TST123\n'}, 'cp1252')

    exit_code = _screen(monkeypatch, tmp_path, base=base, head=head)

    assert exit_code == 1
    assert capsys.readouterr().err.splitlines() == [
        'screen/regex: 1 unsuppressed match(es):',
        '  src/f.txt:1 [tst] a smart \ufffdquote\ufffd for TST123',
    ]


@pytest.mark.parametrize(
    ('patterns_yaml', 'reason'),
    [
        ('name: tst\nregex: TST[A-Z]+\n', 'expected a top-level list'),
        ('- name: only_name\n', "needs 'name' and 'regex' keys"),
    ],
)
def test_malformed_patterns_file_exits_with_reason(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, patterns_yaml: str, reason: str
) -> None:
    base = _init_repo(tmp_path)
    head = _commit_files(tmp_path, {'src/app.py': 'x = 1\n'}, 'head')

    with pytest.raises(SystemExit, match=reason):
        _screen(monkeypatch, tmp_path, base=base, head=head, patterns_yaml=patterns_yaml)
