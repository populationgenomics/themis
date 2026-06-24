"""Validate the committed JSON Schema artifacts under schema/tests/jsonschema/.

These guard the slice S0.1 invariants without needing the Node toolchain: the
emitter produces exactly one bundled file per domain, every type lands under
``$defs``, and each file is a standards-valid 2020-12 schema (the same
``check-jsonschema --check-metaschema`` the ADR calls out).

The ``test_features_*`` cases are the JSON Schema half of the S0.5 round-trip
verification: each construct in the feature-coverage corpus
(``schema/tests/fixtures/features/``) must project to its expected JSON Schema
shape. The Pydantic half lives in ``test_generated_pydantic``; the Zod half is the
``tsc`` smoke test. Freshness against the ``.tsp`` sources is a separate CI gate
(slice S0.4); these run in the ordinary pytest job, which has no Node.
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


def _features_defs() -> dict[str, dict]:
    schema = json.loads((_JSONSCHEMA_DIR / 'features.schema.json').read_text())
    return schema['$defs']


def test_features_string_enum_projects_a_closed_value_set() -> None:
    colour = _features_defs()['Colour']
    assert colour == {'type': 'string', 'enum': ['red', 'green', 'blue']}


def test_features_optional_field_is_absent_from_required() -> None:
    holder = _features_defs()['OptionalHolder']
    assert 'optional_field' in holder['properties']
    assert holder['required'] == ['required_field']


def test_features_optional_with_default_emits_the_default() -> None:
    holder = _features_defs()['DefaultHolder']
    assert holder['properties']['flagged']['default'] is False
    assert 'required' not in holder


def test_features_literal_projects_a_const() -> None:
    holder = _features_defs()['LiteralHolder']
    assert holder['properties']['kind'] == {'type': 'string', 'const': 'widget'}


def test_features_named_union_projects_an_anyof() -> None:
    defs = _features_defs()
    assert defs['Access'] == {'anyOf': [{'$ref': '#/$defs/FreeToRead'}, {'$ref': '#/$defs/Licensed'}]}
    # The licensed variant's per-variant rule: publisher is required.
    assert defs['Licensed']['required'] == ['access', 'publisher']


def test_features_nested_model_projects_a_ref() -> None:
    outer = _features_defs()['Outer']
    assert outer['properties']['inner'] == {'$ref': '#/$defs/Inner'}


def test_features_array_projects_items() -> None:
    holder = _features_defs()['ArrayHolder']
    assert holder['properties']['tags'] == {'type': 'array', 'items': {'type': 'string'}}
    assert holder['properties']['palette'] == {'type': 'array', 'items': {'$ref': '#/$defs/Colour'}}


def test_features_scalar_formats_project_type_and_format() -> None:
    props = _features_defs()['ScalarHolder']['properties']
    assert props['count'] == {'type': 'integer', 'minimum': -2147483648, 'maximum': 2147483647}
    assert props['ratio'] == {'type': 'number'}
    assert props['when'] == {'type': 'string', 'format': 'date-time'}
    assert props['day'] == {'type': 'string', 'format': 'date'}
    assert props['link'] == {'type': 'string', 'format': 'uri'}
