"""Validate the committed JSON Schema artifacts under schema/tests/jsonschema/.

These guard the slice S0.1 invariants without needing the Node toolchain: the
emitter produces exactly one bundled file per domain, every type lands under
``$defs``, and each file is a standards-valid 2020-12 schema (the same
``check-jsonschema --check-metaschema`` the ADR calls out). Freshness against the
``.tsp`` sources is a separate CI gate (slice S0.4); these run in the ordinary
pytest job, which has no Node.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

_JSONSCHEMA_DIR = pathlib.Path(__file__).resolve().parent / 'jsonschema'

_DRAFT_2020_12 = 'https://json-schema.org/draft/2020-12/schema'


def _committed_schemas() -> list[pathlib.Path]:
    return sorted(_JSONSCHEMA_DIR.glob('*.schema.json'))


def test_at_least_one_schema_committed() -> None:
    assert _committed_schemas(), f'no *.schema.json under {_JSONSCHEMA_DIR}'


@pytest.mark.parametrize('schema_path', _committed_schemas(), ids=lambda p: p.name)
def test_is_single_bundled_2020_12_file(schema_path: pathlib.Path) -> None:
    schema = json.loads(schema_path.read_text())
    assert schema['$schema'] == _DRAFT_2020_12
    assert '$id' in schema
    # A bundle carries its types under $defs; the root is a container, not a
    # type with its own object properties.
    assert isinstance(schema['$defs'], dict)
    assert schema['$defs'], 'bundle has an empty $defs'
    assert 'properties' not in schema


@pytest.mark.parametrize('schema_path', _committed_schemas(), ids=lambda p: p.name)
def test_passes_metaschema_check(schema_path: pathlib.Path) -> None:
    check_jsonschema = shutil.which('check-jsonschema')
    assert check_jsonschema is not None, 'check-jsonschema not installed (test dependency group)'
    # Resolved binary path plus literal args; no untrusted input.
    subprocess.run([check_jsonschema, '--check-metaschema', str(schema_path)], check=True)  # noqa: S603
