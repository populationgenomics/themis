"""The committed widget case files are what their generator writes, so a case is only ever changed in the generator."""

from __future__ import annotations

import json
import pathlib

import pytest

from themis.widgets.tests import case_files


@pytest.mark.parametrize(
    ('path', 'generate'),
    [
        (case_files.OWNERSHIP_CASES, case_files.ownership_cases),
        (case_files.ASSET_CASES, case_files.asset_cases),
    ],
    ids=['ownership cases', 'asset cases'],
)
def test_a_committed_case_file_is_what_the_generator_writes(path: pathlib.Path, generate: case_files.Generator) -> None:
    # Compared as data: the web app's formatter lays the file out.
    assert json.loads(path.read_text('utf-8')) == generate(), (
        f'{path.name} differs from what themis.widgets.tests.case_files writes; '
        'run `uv run python -m themis.widgets.tests.case_files`'
    )
