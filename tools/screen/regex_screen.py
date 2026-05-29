"""Scan a PR's added lines for known-format identifiers.

Resolves the merge base of ``--head`` against ``--base``, diffs
``merge-base..head`` with ``--unified=0``, and matches each added line
against the patterns loaded from ``--patterns``. Lines carrying an
inline ``screen-ignore: <reason>`` marker are suppressed. Exits non-zero
if any unsuppressed match remains.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterator
from typing import NamedTuple

import yaml


class AddedLine(NamedTuple):
    path: str
    line_no: int
    text: str


class Hit(NamedTuple):
    path: str
    line_no: int
    pattern_name: str
    text: str


# A trailing same-line comment of the form:
#   # screen-ignore: reason            (Python / YAML / shell)
#   // screen-ignore: reason           (JS / TS / Go / Rust)
#   /* screen-ignore: reason */        (C / C++)
#   <!-- screen-ignore: reason -->     (HTML / XML)
# The reason must be non-empty.
SUPPRESSION_RE = re.compile(r'(?:#|//|/\*|<!--)\s*screen-ignore:\s*\S[^\n]*?(?:\s*\*/|\s*-->)?\s*$')


def _run(cmd: list[str]) -> str:
    # All callers pass literal git subcommands; no untrusted input.
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout  # noqa: S603


def _merge_base(head: str, base: str) -> str:
    return _run(['git', 'merge-base', base, head]).strip()


def _iter_added_lines(diff_output: str) -> Iterator[AddedLine]:
    """Yield every added line in the diff."""
    path: str | None = None
    new_line = 0
    for line in diff_output.splitlines():
        if line.startswith('+++ '):
            target = line[4:]
            if target == '/dev/null':
                path = None
            elif target.startswith('b/'):
                path = target[2:]
            else:
                path = target
        elif line.startswith('@@'):
            m = re.match(r'@@ -\S+ \+(\d+)', line)
            if m:
                new_line = int(m.group(1))
        elif line.startswith('+') and not line.startswith('+++'):
            if path is not None:
                yield AddedLine(path, new_line, line[1:])
            new_line += 1


def _load_patterns(path: pathlib.Path) -> list[tuple[str, re.Pattern[str]]]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, list):
        raise SystemExit(f'{path}: expected a top-level list of pattern entries')
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for entry in data:
        if not isinstance(entry, dict) or 'name' not in entry or 'regex' not in entry:
            raise SystemExit(f"{path}: each entry needs 'name' and 'regex' keys")
        compiled.append((entry['name'], re.compile(entry['regex'])))
    return compiled


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--head', required=True, help='PR head SHA')
    ap.add_argument('--base', required=True, help='PR base SHA')
    ap.add_argument('--patterns', required=True, type=pathlib.Path)
    args = ap.parse_args()

    mb = _merge_base(args.head, args.base)
    diff = _run(['git', 'diff', '--unified=0', '--no-color', f'{mb}..{args.head}'])
    patterns = _load_patterns(args.patterns)

    hits: list[Hit] = []
    for added in _iter_added_lines(diff):
        suppressed = SUPPRESSION_RE.search(added.text) is not None
        for name, regex in patterns:
            if regex.search(added.text):
                if suppressed:
                    continue
                hits.append(Hit(added.path, added.line_no, name, added.text.rstrip('\n')))

    if hits:
        print(f'screen/regex: {len(hits)} unsuppressed match(es):', file=sys.stderr)
        for h in hits:
            print(f'  {h.path}:{h.line_no} [{h.pattern_name}] {h.text}', file=sys.stderr)
        return 1

    print('screen/regex: no matches')
    return 0


if __name__ == '__main__':
    sys.exit(main())
