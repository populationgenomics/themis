"""Tests for the working-document linter CLI (`python -m themis.document_linter`)."""

from __future__ import annotations

import pathlib
import sys

import pytest

from themis.document_linter import __main__ as cli


def _lint(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, markdown: str) -> int | str | None:
    doc = tmp_path / 'working_document.md'
    doc.write_text(markdown, 'utf-8')
    monkeypatch.setattr(sys, 'argv', ['themis.document_linter', str(doc)])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    return exited.value.code


def test_clean_document_exits_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _lint(monkeypatch, tmp_path, '# Title\n\nBody paragraph.\n') == 0
    assert capsys.readouterr().err == ''


def test_hash_inside_code_fence_is_not_a_title(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _lint(monkeypatch, tmp_path, '# Title\n\n```sh\n# not a heading\n```\n') == 0
    assert capsys.readouterr().err == ''


def test_empty_document_is_flagged(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _lint(monkeypatch, tmp_path, '   \n  ') == 1
    assert 'lint: document is empty\n' in capsys.readouterr().err


def test_missing_title_is_flagged(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _lint(monkeypatch, tmp_path, 'Body without a heading.\n') == 1
    assert capsys.readouterr().err == 'lint: document has no top-level title (a `# ` heading)\n'


def test_multiple_titles_are_flagged(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _lint(monkeypatch, tmp_path, '# One\n# Two\n') == 1
    assert capsys.readouterr().err == 'lint: document has 2 top-level titles; expected exactly one\n'


def test_wrong_argument_count_exits_with_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, 'argv', ['themis.document_linter'])
    with pytest.raises(SystemExit, match='usage:'):
        cli.main()
