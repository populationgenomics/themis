"""Tests for tools.check_links."""

from __future__ import annotations

import pathlib

import pytest

from tools import check_links


class TestSlugify:
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
    def test_matches_github_slugs(self, heading: str, slug: str) -> None:
        assert check_links._slugify(heading) == slug


class TestHeadingSlugs:
    def test_skips_headings_inside_code_fences(self, tmp_path: pathlib.Path) -> None:
        md = tmp_path / 'doc.md'
        md.write_text('# Real\n\n```\n# Not a heading\n```\n\n## Also real\n')
        assert check_links.heading_slugs(md) == {'real', 'also-real'}


class TestResolve:
    def test_external_targets_skipped(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / 'a.md'
        src.write_text('')
        assert check_links.resolve(src, 'https://example.com', tmp_path) is None
        assert check_links.resolve(src, 'mailto:x@y.z', tmp_path) is None

    def test_missing_file_reported(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / 'a.md'
        src.write_text('')
        assert check_links.resolve(src, 'gone.md', tmp_path) == 'target does not exist'

    def test_existing_file_resolves(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / 'b.md').write_text('# Heading\n')
        src = tmp_path / 'a.md'
        src.write_text('')
        assert check_links.resolve(src, 'b.md', tmp_path) is None

    def test_repo_root_relative_resolves(self, tmp_path: pathlib.Path) -> None:
        # A path that does not resolve against the source's directory but does
        # against the repo root (the .github/review/*.md prompt-fragment case).
        (tmp_path / 'docs').mkdir()
        (tmp_path / 'docs' / 'style.md').write_text('# Style\n')
        src = tmp_path / 'sub' / 'frag.md'
        src.parent.mkdir()
        src.write_text('')
        assert check_links.resolve(src, 'docs/style.md', tmp_path) is None

    def test_leading_slash_is_repo_root_relative(self, tmp_path: pathlib.Path) -> None:
        # GitHub treats a leading-slash target as repo-root-relative, not
        # filesystem-root; it must resolve against root, not collapse to /docs.
        (tmp_path / 'docs').mkdir()
        (tmp_path / 'docs' / 'style.md').write_text('# Style\n')
        src = tmp_path / 'sub' / 'frag.md'
        src.parent.mkdir()
        src.write_text('')
        assert check_links.resolve(src, '/docs/style.md', tmp_path) is None
        assert check_links.resolve(src, '/docs/missing.md', tmp_path) == 'target does not exist'

    def test_anchor_into_other_file(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / 'b.md').write_text('## A Section\n')
        src = tmp_path / 'a.md'
        src.write_text('')
        assert check_links.resolve(src, 'b.md#a-section', tmp_path) is None
        assert check_links.resolve(src, 'b.md#missing', tmp_path) == 'no heading anchor #missing'

    def test_same_file_anchor(self, tmp_path: pathlib.Path) -> None:
        src = tmp_path / 'a.md'
        src.write_text('# Top\n')
        assert check_links.resolve(src, '#top', tmp_path) is None
        assert check_links.resolve(src, '#bottom', tmp_path) == 'no heading anchor #bottom'

    def test_anchor_not_checked_for_non_markdown(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / 'code.py').write_text('x = 1\n')
        src = tmp_path / 'a.md'
        src.write_text('')
        assert check_links.resolve(src, 'code.py#L1', tmp_path) is None


class TestCheckFile:
    def test_collects_broken_links_with_line_numbers(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / 'b.md').write_text('## Kept\n')
        src = tmp_path / 'a.md'
        src.write_text(
            'See [ok](b.md) and [bad](gone.md).\n'
            'A [stale anchor](b.md#dropped) here.\n'
            '```\n[not checked](inside-fence.md)\n```\n'
        )
        broken = check_links.check_file(src, tmp_path)
        assert broken == [
            check_links.Broken('a.md', 1, 'gone.md', 'target does not exist'),
            check_links.Broken('a.md', 2, 'b.md#dropped', 'no heading anchor #dropped'),
        ]

    def test_inline_code_link_syntax_not_checked(self, tmp_path: pathlib.Path) -> None:
        # `[text](path)` shown as inline-code syntax is documentation, not a link.
        src = tmp_path / 'a.md'
        src.write_text('A broken relative link is `[text](path)` whose target is gone.\n')
        assert check_links.check_file(src, tmp_path) == []
