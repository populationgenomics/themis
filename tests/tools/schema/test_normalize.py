"""Unit tests for the bundled-schema normalize pass (tools/schema/normalize.py).

Covers the #4084 workaround in isolation, without the Node toolchain: relative
``$ref`` rewriting (including nested under arrays), per-``$def`` ``$id``/``$schema``
stripping, root preservation, and the fail-loud on a non-bundle input.
"""

from __future__ import annotations

import pytest

from tools.schema import normalize

_DRAFT_2020_12 = 'https://json-schema.org/draft/2020-12/schema'


def _bundle() -> dict:
    """A bundle shaped exactly as @typespec/json-schema emits one (pre-normalize)."""
    return {
        '$schema': _DRAFT_2020_12,
        '$id': 'demo.schema.json',
        '$defs': {
            'Catalogue': {
                '$schema': _DRAFT_2020_12,
                '$id': 'Catalogue.json',
                'type': 'object',
                'properties': {'widgets': {'type': 'array', 'items': {'$ref': 'Widget.json'}}},
                'required': ['widgets'],
            },
            'Widget': {
                '$schema': _DRAFT_2020_12,
                '$id': 'Widget.json',
                'type': 'object',
                'properties': {'colour': {'$ref': 'Colour.json'}},
            },
            'Colour': {'$schema': _DRAFT_2020_12, '$id': 'Colour.json', 'type': 'string', 'enum': ['red']},
        },
    }


def test_rewrites_relative_refs_to_local_defs() -> None:
    result = normalize.normalize(_bundle())
    # Ref nested under an array's items.
    assert result['$defs']['Catalogue']['properties']['widgets']['items']['$ref'] == '#/$defs/Widget'
    # Ref directly on a property.
    assert result['$defs']['Widget']['properties']['colour']['$ref'] == '#/$defs/Colour'


def test_strips_per_def_id_and_schema() -> None:
    result = normalize.normalize(_bundle())
    for name, subschema in result['$defs'].items():
        assert '$id' not in subschema, f'{name} kept its $id'
        assert '$schema' not in subschema, f'{name} kept its $schema'


def test_preserves_root_id_and_schema() -> None:
    result = normalize.normalize(_bundle())
    assert result['$id'] == 'demo.schema.json'
    assert result['$schema'] == _DRAFT_2020_12


def test_leaves_already_local_refs_untouched() -> None:
    schema = {'$defs': {'A': {'$ref': '#/$defs/B'}, 'B': {'type': 'string'}}}
    assert normalize.normalize(schema)['$defs']['A']['$ref'] == '#/$defs/B'


def test_raises_when_not_a_bundle() -> None:
    with pytest.raises(ValueError, match=r'\$defs'):
        normalize.normalize({'$schema': _DRAFT_2020_12, 'type': 'string'})


def test_does_not_mutate_the_input() -> None:
    bundle = _bundle()
    normalize.normalize(bundle)
    # The input still carries its pre-normalize form: per-$def $id and relative refs.
    assert bundle['$defs']['Widget']['$id'] == 'Widget.json'
    assert bundle['$defs']['Catalogue']['properties']['widgets']['items']['$ref'] == 'Widget.json'
