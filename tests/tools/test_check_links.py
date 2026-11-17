"""Tests for ``tools.check_links`` through its CLI entry point."""

from __future__ import annotations

import os
import pathlib
import subprocess
from collections.abc import Mapping

import pytest

from tools import check_links


def _check_repo(monkeypatch: pytest.MonkeyPatch, repo: pathlib.Path, tracked: Mapping[str, str]) -> int:
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', os.devnull)
    subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True, capture_output=True)  # noqa: S603, S607
    for rel_path, content in tracked.items():
        dest = repo / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, 'utf-8')
    if tracked:
        cmd = ['git', '-C', str(repo), 'add', '--', *tracked]
        subprocess.run(cmd, check=True, capture_output=True)  # noqa: S603
    monkeypatch.chdir(repo)
    return check_links.main()


def test_clean_relative_and_external_links_exit_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _check_repo(
        monkeypatch,
        tmp_path,
        {
            'b.md': '# Heading\n',
            'a.md': (
                'See [sibling](b.md), [angled](<b.md>), [titled](b.md "Title"), '
                '[site](https://example.com), [mail](mailto:x@y.z), and [cdn](//example.com/a).\n'
            ),
        },
    )

    assert exit_code == 0
    assert capsys.readouterr().out == '\n0 broken link(s) across tracked Markdown.\n'


def test_broken_file_and_anchor_links_are_reported_with_line_numbers(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _check_repo(
        monkeypatch,
        tmp_path,
        {
            'b.md': '## Kept\n',
            'a.md': 'See [ok](b.md) and [bad](gone.md).\nA [stale anchor](b.md#dropped) here.\n',
        },
    )

    assert exit_code == 1
    assert capsys.readouterr().out == (
        'a.md:1 -> gone.md  (target does not exist)\n'
        'a.md:2 -> b.md#dropped  (no heading anchor #dropped)\n'
        '\n2 broken link(s) across tracked Markdown.\n'
    )


def test_links_inside_fenced_code_blocks_are_not_checked(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code = _check_repo(
        monkeypatch,
        tmp_path,
        {'a.md': '# Doc\n\n```\n[not checked](inside-fence.md)\n```\n\n~~~\n[also fenced](gone.md)\n~~~\n'},
    )

    assert exit_code == 0


@pytest.mark.parametrize(
    ('heading', 'slug'),
    [
        ('Imports', 'imports'),
        ('Carved-out exceptions', 'carved-out-exceptions'),
        ('No `TYPE_CHECKING` blocks', 'no-type_checking-blocks'),
        ('Plans & design', 'plans--design'),
        ('  Trailing space  ', 'trailing-space'),
    ],
)
def test_heading_anchors_follow_github_slug_rules(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, heading: str, slug: str
) -> None:
    exit_code = _check_repo(
        monkeypatch,
        tmp_path,
        {
            'doc.md': f'## {heading}\n',
            'index.md': f'See [section](doc.md#{slug}).\n',
        },
    )

    assert exit_code == 0


def test_headings_inside_code_fences_do_not_satisfy_anchors(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _check_repo(
        monkeypatch,
        tmp_path,
        {
            'doc.md': '# Real\n\n```\n# Not a heading\n```\n\n## Also real\n',
            'a.md': 'See [ok](doc.md#real), [ok2](doc.md#also-real), and [fenced](doc.md#not-a-heading).\n',
        },
    )

    assert exit_code == 1
    assert capsys.readouterr().out.splitlines()[0] == (
        'a.md:1 -> doc.md#not-a-heading  (no heading anchor #not-a-heading)'
    )


def test_same_file_anchor_checks_the_linking_document(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _check_repo(
        monkeypatch,
        tmp_path,
        {'a.md': '# Top\n\nJump to [top](#top) or [bottom](#bottom).\n'},
    )

    assert exit_code == 1
    assert capsys.readouterr().out.splitlines()[0] == 'a.md:3 -> #bottom  (no heading anchor #bottom)'


def test_url_encoded_target_resolves_against_decoded_filesystem_path(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A route directory such as `[projectId]` is linked as `%5BprojectId%5D`.
    (tmp_path / '[projectId]').mkdir()
    assert _check_repo(monkeypatch, tmp_path, {'a.md': 'See [route](%5BprojectId%5D).\n'}) == 0


def test_repo_root_relative_target_resolves_when_absent_beside_source(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `.github/review/*.md` prompt fragments link repo-root-relative paths (`docs/style.md`).
    exit_code = _check_repo(
        monkeypatch,
        tmp_path,
        {
            'docs/style.md': '# Style\n',
            'sub/frag.md': 'Follow [style](docs/style.md).\n',
        },
    )

    assert exit_code == 0


def test_leading_slash_resolves_against_repo_root_only(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # GitHub treats `/docs/...` as repo-root-relative, never filesystem-root or source-dir-relative.
    exit_code = _check_repo(
        monkeypatch,
        tmp_path,
        {
            'docs/style.md': '# Style\n',
            'sub/docs/local_only.md': '# Local\n',
            'sub/frag.md': ('See [root](/docs/style.md), [gone](/docs/missing.md), and [sub](/docs/local_only.md).\n'),
        },
    )

    assert exit_code == 1
    assert capsys.readouterr().out == (
        'sub/frag.md:1 -> /docs/missing.md  (target does not exist)\n'
        'sub/frag.md:1 -> /docs/local_only.md  (target does not exist)\n'
        '\n2 broken link(s) across tracked Markdown.\n'
    )


def test_non_markdown_fragment_is_not_checked_as_heading_anchor(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_code = _check_repo(
        monkeypatch,
        tmp_path,
        {
            'code.py': 'x = 1\n',
            'a.md': 'See [line](code.py#L1).\n',
        },
    )

    assert exit_code == 0


def test_inline_code_link_syntax_is_not_checked(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code = _check_repo(
        monkeypatch,
        tmp_path,
        {'a.md': 'A broken relative link is `[text](path)` whose target is gone.\n'},
    )

    assert exit_code == 0


def test_untracked_markdown_is_ignored(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / 'scratch.md').write_text('Broken [draft](nowhere.md).\n', 'utf-8')
    assert _check_repo(monkeypatch, tmp_path, {'a.md': '# Tracked\n'}) == 0
