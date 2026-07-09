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


def test_rewrites_record_maps_to_additional_properties() -> None:
    # @typespec/json-schema emits Record<T> as {type: object, properties: {},
    # unevaluatedProperties: <T>}; datamodel-code-generator ignores
    # unevaluatedProperties, so normalize rewrites it to additionalProperties.
    bundle = {
        '$schema': _DRAFT_2020_12,
        '$id': 'demo.schema.json',
        '$defs': {
            'RecordWidget': {'type': 'object', 'properties': {}, 'unevaluatedProperties': {'$ref': 'Widget.json'}},
            'Widget': {'type': 'object', 'properties': {'colour': {'type': 'string'}}},
        },
    }
    rec = normalize.normalize(bundle)['$defs']['RecordWidget']
    assert rec['additionalProperties'] == {'$ref': '#/$defs/Widget'}  # ref rewritten too
    assert 'unevaluatedProperties' not in rec
    assert 'properties' not in rec


def test_rewrite_record_maps_leaves_mixed_properties_untouched() -> None:
    # A $def carrying BOTH fixed properties and unevaluatedProperties is not a
    # plain Record<T>; collapsing it to additionalProperties would drop the fixed
    # fields, so normalize leaves it as-is (the inner $ref is still rewritten).
    bundle = {
        '$schema': _DRAFT_2020_12,
        '$id': 'demo.schema.json',
        '$defs': {
            'Mixed': {
                'type': 'object',
                'properties': {'name': {'type': 'string'}},
                'unevaluatedProperties': {'$ref': 'Widget.json'},
            },
            'Widget': {'type': 'object', 'properties': {'colour': {'type': 'string'}}},
        },
    }
    mixed = normalize.normalize(bundle)['$defs']['Mixed']
    assert 'additionalProperties' not in mixed  # not collapsed to a map
    assert mixed['properties'] == {'name': {'type': 'string'}}  # fixed fields kept
    assert mixed['unevaluatedProperties'] == {'$ref': '#/$defs/Widget'}  # kept; ref still rewritten


def test_seal_closes_object_defs() -> None:
    schema = {'$defs': {'Widget': {'type': 'object', 'properties': {'a': {'type': 'string'}}}}}
    sealed = normalize.seal(schema)
    assert sealed['$defs']['Widget']['additionalProperties'] is False


def test_seal_leaves_a_typed_map_open() -> None:
    # A Record<T> map (normalize already gave it an additionalProperties schema)
    # must not be sealed to additionalProperties: false — that rejects every key.
    schema = {'$defs': {'RecordWidget': {'type': 'object', 'additionalProperties': {'$ref': '#/$defs/Widget'}}}}
    sealed = normalize.seal(schema)
    assert sealed['$defs']['RecordWidget']['additionalProperties'] == {'$ref': '#/$defs/Widget'}


def test_seal_leaves_a_union_container_open() -> None:
    # A named union is an anyOf of variant $refs with no type: object — sealing it
    # to additionalProperties: false would reject every variant. Only the member
    # models get sealed; the container stays open.
    schema = {'$defs': {'Access': {'anyOf': [{'$ref': '#/$defs/FreeToRead'}, {'$ref': '#/$defs/Licensed'}]}}}
    sealed = normalize.seal(schema)
    assert 'additionalProperties' not in sealed['$defs']['Access']


def test_seal_does_not_mutate_the_input() -> None:
    schema = {'$defs': {'Widget': {'type': 'object', 'properties': {}}}}
    normalize.seal(schema)
    assert 'additionalProperties' not in schema['$defs']['Widget']


def test_seal_raises_when_not_a_bundle() -> None:
    with pytest.raises(ValueError, match=r'\$defs'):
        normalize.seal({'$schema': _DRAFT_2020_12, 'type': 'string'})
