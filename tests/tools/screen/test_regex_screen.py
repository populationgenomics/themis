"""Tests for tools.screen.regex_screen.

Test data uses the ``TST`` prefix rather than ``CPG`` / ``XPG`` so the
fixture content does not look like an intended CPG identifier. See
``tests/tools/screen/fixtures/README.md``.
"""

from __future__ import annotations

import pathlib

import pytest

from tools.screen import regex_screen

FIXTURES = pathlib.Path(__file__).parent / 'fixtures'


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


class TestSuppressionRegex:
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
    def test_matches_valid_markers(self, line: str) -> None:
        assert regex_screen.SUPPRESSION_RE.search(line) is not None

    @pytest.mark.parametrize(
        'line',
        [
            'x = "TST123"',
            'x = "TST123"  # just a normal comment',
            'x = "TST123"  # screen-ignore:',
            'x = "TST123"  # screen-ignore:   ',
            '# screen-ignore mentioned in prose without colon',
        ],
    )
    def test_rejects_invalid_markers(self, line: str) -> None:
        assert regex_screen.SUPPRESSION_RE.search(line) is None


class TestIterAddedLines:
    def test_single_file_one_hit(self) -> None:
        lines = list(regex_screen._iter_added_lines(_load('single-file-one-hit.diff')))
        assert lines == [
            regex_screen.AddedLine('src/app.py', 3, ''),
            regex_screen.AddedLine('src/app.py', 4, 'PARTICIPANT = "TST123"'),
        ]

    def test_with_suppression_line(self) -> None:
        lines = list(regex_screen._iter_added_lines(_load('with-suppression.diff')))
        assert lines == [
            regex_screen.AddedLine('src/app.py', 3, ''),
            regex_screen.AddedLine('src/app.py', 4, 'PARTICIPANT = "TST123"'),
            regex_screen.AddedLine(
                'src/app.py',
                5,
                'FIXTURE_ID = "TST456"  # screen-ignore: deliberate test fixture',
            ),
        ]

    def test_multi_file_and_multi_hunk(self) -> None:
        lines = list(regex_screen._iter_added_lines(_load('multi-file.diff')))
        by_path: dict[str, list[regex_screen.AddedLine]] = {}
        for line in lines:
            by_path.setdefault(line.path, []).append(line)

        assert set(by_path) == {'src/app.py', 'src/new_file.py', 'src/util.py'}

        app = by_path['src/app.py']
        assert [line.line_no for line in app] == [3, 4, 5, 6, 7, 8, 9]
        assert app[1].text == 'A = "TST111"'
        assert app[6].text == 'B = "TST222"'

        new_file = by_path['src/new_file.py']
        assert new_file == [regex_screen.AddedLine('src/new_file.py', 1, 'NEW_FILE_ID = "TST444"')]

        util = by_path['src/util.py']
        assert util == [regex_screen.AddedLine('src/util.py', 2, '    # TST333')]

    def test_rename_attributes_to_new_path(self) -> None:
        lines = list(regex_screen._iter_added_lines(_load('rename.diff')))
        assert lines == [
            regex_screen.AddedLine('src/utils.py', 3, ''),
            regex_screen.AddedLine('src/utils.py', 4, 'PARTICIPANT = "TST789"'),
        ]
        # Crucially, no AddedLine is attributed to the old path src/util.py.
        assert all(line.path != 'src/util.py' for line in lines)

    def test_binary_file_yields_nothing(self) -> None:
        lines = list(regex_screen._iter_added_lines(_load('binary.diff')))
        assert lines == []


class TestLoadPatterns:
    def test_loads_valid_yaml(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'patterns.yaml'
        p.write_text('- name: tst\n  regex: \\bTST\\d+\\b\n')
        patterns = regex_screen._load_patterns(p)
        assert len(patterns) == 1
        assert patterns[0][0] == 'tst'
        assert patterns[0][1].search('hello TST123') is not None

    def test_rejects_non_list(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'patterns.yaml'
        p.write_text('name: tst\nregex: TST[A-Z]+\n')
        with pytest.raises(SystemExit):
            regex_screen._load_patterns(p)

    def test_rejects_missing_keys(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / 'patterns.yaml'
        p.write_text('- name: only_name\n')
        with pytest.raises(SystemExit):
            regex_screen._load_patterns(p)
