"""Tests for the working-document linter."""

from __future__ import annotations

from themis.document_linter import linter


def test_clean_document_has_no_issues() -> None:
    assert linter.lint('# Title\n\nBody paragraph.') == []


def test_empty_document_is_flagged() -> None:
    assert 'document is empty' in linter.lint('   \n  ')


def test_missing_title_is_flagged() -> None:
    assert linter.lint('Body without a heading.') == ['document has no top-level title (a `# ` heading)']


def test_multiple_titles_are_flagged() -> None:
    issues = linter.lint('# One\n# Two')
    assert len(issues) == 1
    assert 'expected exactly one' in issues[0]


def test_hash_inside_code_fence_is_not_a_title() -> None:
    assert linter.lint('# Title\n\n```sh\n# not a heading\n```\n') == []
