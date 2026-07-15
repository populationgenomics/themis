"""Run the working-document linter over a file: ``python -m themis.document_linter <document.md>``.

Exits 0 when the document is well-formed, 1 (with issues on stderr) otherwise, so the model can run it
under ``bash`` and read the result.
"""

from __future__ import annotations

import pathlib
import sys

from themis.document_linter import linter


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit('usage: python -m themis.document_linter <document.md>')
    issues = linter.lint(pathlib.Path(sys.argv[1]).read_text())
    for issue in issues:
        print(f'lint: {issue}', file=sys.stderr)
    raise SystemExit(1 if issues else 0)


if __name__ == '__main__':
    main()
