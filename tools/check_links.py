"""Check that every relative link and anchor in the tracked Markdown resolves.

Enumerates tracked Markdown with ``git ls-files '*.md'`` and, for each
``[text](target)`` link outside a fenced code block, verifies that:

- a relative file or directory target exists in the tree, and
- a ``#fragment`` (alone, or after a path into a Markdown file) names a
  heading that exists in the target file.

A relative target counts as resolved if it exists relative to *either* the
linking file's directory (GitHub's rendering semantics) or the repository
root. The repo-root fallback is load-bearing: the ``.github/review/*.md``
fragments are concatenated into a prompt run from the repo root, so they use
repo-root-relative paths (``docs/style/python.md``) that would 404 as a
GitHub link but are correct for that consumer. The cost is a rare false
negative — a broken ``foo.md`` masked by an unrelated ``foo.md`` at the root.
A leading-slash target (``/docs/foo.md``) is GitHub's explicit
repo-root-relative form and resolves against the root only.

Inline-code spans (`` `…` ``) and fenced code blocks are not scanned for
links: docs routinely show ``[text](path)`` link *syntax* as an example.

External targets (``http(s):``, ``mailto:``, protocol-relative ``//``, …)
are skipped. Prints one ``file:line -> target  (reason)`` per failure and
exits non-zero when any remain. This is the doc-gardening agent's link pass,
not a blocking CI gate: a design doc may deliberately link a sibling it
proposes but has not yet authored, so the agent judges each hit (repoint,
or leave-and-report) rather than treating a non-zero exit as failure.

Heading slugs follow GitHub's algorithm (lowercase; drop punctuation other
than ``-`` and ``_``; each space to a hyphen). Duplicate-heading
disambiguation (GitHub's ``-1`` / ``-2`` suffixes) is not modelled, so a
file with two identically-named headings can yield a false anchor miss.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
from typing import NamedTuple


class Broken(NamedTuple):
    source: str
    line_no: int
    target: str
    reason: str


# A `[text](target)` link; `target` is the first capture. Also matches the
# `(target)` of an image `![alt](target)`. Link text containing a `]` is not
# handled (rare in prose), and reference-style `[text][ref]` links are skipped.
LINK_RE = re.compile(r'\[[^\]]*\]\(\s*([^)]+?)\s*\)')
INLINE_CODE_RE = re.compile(r'`[^`]*`')
# An external scheme (`mailto:`, `https:`, …) or a protocol-relative `//`.
EXTERNAL_RE = re.compile(r'^(?:[a-z][a-z0-9+.-]*:|//)', re.IGNORECASE)
HEADING_RE = re.compile(r'^(#{1,6})\s+(.*\S)\s*$')
FENCE_RE = re.compile(r'^\s*(?:```|~~~)')


def _run(cmd: list[str]) -> str:
    # All callers pass literal git subcommands; no untrusted input.
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout  # noqa: S603


def tracked_markdown(root: pathlib.Path) -> list[pathlib.Path]:
    out = _run(['git', '-C', str(root), 'ls-files', '*.md'])
    return [root / line for line in out.splitlines() if line]


def _slugify(heading: str) -> str:
    text = re.sub(r'[^\w\s-]', '', heading.strip().lower())
    return re.sub(r'\s', '-', text)


def _prose_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return (1-based line number, text) for lines outside fenced code."""
    lines: list[tuple[int, str]] = []
    in_fence = False
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append((n, line))
    return lines


def heading_slugs(path: pathlib.Path) -> set[str]:
    slugs: set[str] = set()
    for _, line in _prose_lines(path):
        m = HEADING_RE.match(line)
        if m:
            slugs.add(_slugify(m.group(2)))
    return slugs


def _target_path(raw: str) -> str:
    """Strip an optional ``<>`` wrapper and a trailing ``"title"`` from a link."""
    inner = raw.strip()
    if inner.startswith('<') and inner.endswith('>'):
        inner = inner[1:-1]
    return inner.split(' "', 1)[0].strip()


def resolve(source: pathlib.Path, target: str, root: pathlib.Path) -> str | None:
    """Return a failure reason for a link target, or None if it resolves.

    Args:
        source: The Markdown file the link appears in; relative targets
            resolve against its parent directory.
        target: The raw link target, possibly ``path``, ``#fragment``, or
            ``path#fragment``.
        root: The repository root; relative targets also resolve against it
            (see the module docstring for why).

    Returns:
        A human-readable reason string when the target is broken, else None.
    """
    if EXTERNAL_RE.match(target):
        return None
    path_part, _, fragment = target.partition('#')
    if path_part:
        if path_part.startswith('/'):
            # GitHub treats a leading slash as repo-root-relative, not
            # filesystem-root; resolve against root only (pathlib would
            # otherwise discard the left operand of an absolute join).
            candidates = [(root / path_part.lstrip('/')).resolve()]
        else:
            candidates = [(source.parent / path_part).resolve(), (root / path_part).resolve()]
        dest = next((c for c in candidates if c.exists()), None)
        if dest is None:
            return 'target does not exist'
    else:
        dest = source
    if fragment and dest.suffix == '.md' and dest.exists() and fragment not in heading_slugs(dest):
        return f'no heading anchor #{fragment}'
    return None


def check_file(path: pathlib.Path, root: pathlib.Path) -> list[Broken]:
    broken: list[Broken] = []
    for n, line in _prose_lines(path):
        for m in LINK_RE.finditer(INLINE_CODE_RE.sub('', line)):
            target = _target_path(m.group(1))
            reason = resolve(path, target, root)
            if reason:
                broken.append(Broken(str(path.relative_to(root)), n, target, reason))
    return broken


def main() -> int:
    root = pathlib.Path(_run(['git', 'rev-parse', '--show-toplevel']).strip())
    broken = [b for md in tracked_markdown(root) for b in check_file(md, root)]
    for b in broken:
        print(f'{b.source}:{b.line_no} -> {b.target}  ({b.reason})')
    print(f'\n{len(broken)} broken link(s) across tracked Markdown.')
    return 1 if broken else 0


if __name__ == '__main__':
    sys.exit(main())
