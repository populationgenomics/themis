"""Lint the working document (self-hosted-sandbox.md §9).

A convenience the model runs to self-correct during a turn — not a gate; the frontend renderer is the
arbiter at render time. Checks basic structural well-formedness (non-empty, exactly one top-level
title); ``_CHECKS`` is the extension point for richer working-document rules.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

_Check = Callable[[str], list[str]]


def lint(markdown: str) -> list[str]:
    """Return the working document's structural issues (empty when it is well-formed)."""
    return [issue for check in _CHECKS for issue in check(markdown)]


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
