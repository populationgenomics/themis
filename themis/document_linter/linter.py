"""Lint the working document (self-hosted-sandbox.md §9).

A convenience the model runs to self-correct during a turn — not a gate; the frontend renderer is the
arbiter at render time. ``lint`` checks the markdown's structural well-formedness (non-empty, exactly one
top-level title); ``_CHECKS`` is the extension point for richer rules over the text alone. ``lint_document``
adds what needs the files beside it: every ``::embed`` names an asset that draws.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Iterator

from themis.document_linter import embeds

_Check = Callable[[str], list[str]]


def lint(markdown: str) -> list[str]:
    """Return the working document's structural issues (empty when it is well-formed)."""
    return [issue for check in _CHECKS for issue in check(markdown)]


def lint_document(path: pathlib.Path) -> list[str]:
    """Return the issues of the working document at ``path``: its structure, and each embed against its asset."""
    markdown = path.read_text('utf-8')
    return lint(markdown) + embeds.issues(markdown, path.parent)


def _non_empty(markdown: str) -> list[str]:
    return [] if markdown.strip() else ['document is empty']


def _content_lines(markdown: str) -> Iterator[str]:
    """Yield lines outside fenced code blocks, so a ``# `` inside a fence is not read as a heading."""
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield line


def _single_title(markdown: str) -> list[str]:
    titles = [line for line in _content_lines(markdown) if line.startswith('# ')]
    if not titles:
        return ['document has no top-level title (a `# ` heading)']
    if len(titles) > 1:
        return [f'document has {len(titles)} top-level titles; expected exactly one']
    return []


_CHECKS: tuple[_Check, ...] = (_non_empty, _single_title)
