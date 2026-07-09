"""Normalize a bundled TypeSpec JSON Schema so datamodel-code-generator can read it.

The ``@typespec/json-schema`` emitter bundles a domain into one 2020-12 file with
every type under ``$defs``, but writes inter-type refs as ``$id``-relative
(``"$ref": "AccessKind.json"``) and gives each ``$def`` its own ``$id``/``$schema``.
``datamodel-code-generator`` reads the relative ref as a file path and fails to
resolve it (TypeSpec Discussion #4084:
https://github.com/microsoft/typespec/discussions/4084).

Normalize rewrites every ``"<Name>.json"`` ref to ``"#/$defs/<Name>"`` and strips
the per-``$def`` ``$id``/``$schema``, leaving the bundle root's ``$id``/``$schema``
intact. The committed JSON Schema is this normalized single-file form — it is both
the at-rest validation artifact and the codegen source. Pure #4084 workaround;
droppable if the emitter resolves refs to ``#/$defs/…`` itself.
"""

from __future__ import annotations

import copy
from typing import Any

# A parsed-JSON node: a dict/list/scalar. `object` (not Any) so the isinstance
# checks below narrow it and the walker stays type-checked.
type _Json = object

_REF = '$ref'
_DEFS = '$defs'
_ID = '$id'
_SCHEMA = '$schema'
_JSON_SUFFIX = '.json'
_PROPERTIES = 'properties'
_ADDITIONAL = 'additionalProperties'
_UNEVALUATED = 'unevaluatedProperties'


def _rewrite_refs(node: _Json) -> None:
    """Rewrite every ``$id``-relative ``$ref`` to a local ``#/$defs/…`` pointer, in place.

    A ref the emitter writes as ``"<Name>.json"`` points at the ``$def`` keyed by
    ``<Name>``. Already-local refs (``#/…``) and any other form are left untouched.
    """
    if isinstance(node, dict):
        ref = node.get(_REF)
        if isinstance(ref, str) and ref.endswith(_JSON_SUFFIX) and not ref.startswith('#'):
            node[_REF] = f'#/{_DEFS}/{ref[: -len(_JSON_SUFFIX)]}'
        for value in node.values():
            _rewrite_refs(value)
    elif isinstance(node, list):
        for item in node:
            _rewrite_refs(item)


def _rewrite_record_maps(node: _Json) -> None:
    """Rewrite TypeSpec ``Record<T>`` maps to the ``additionalProperties`` form, in place.

    The emitter writes ``Record<T>`` as ``{type: object, properties: {},
    unevaluatedProperties: <T>}``. ``datamodel-code-generator`` does not honour
    ``unevaluatedProperties`` (a 2020-12 keyword) and would emit an empty model;
    with no fixed properties the map is equivalent to ``additionalProperties: <T>``,
    which it renders as ``dict[str, T]``. Only rewrite when ``properties`` is
    empty/absent, so a model carrying both fixed and unevaluated properties is left
    untouched.
    """
    if isinstance(node, dict):
        unevaluated = node.get(_UNEVALUATED)
        if unevaluated is not None and not node.get(_PROPERTIES):
            node[_ADDITIONAL] = unevaluated
            node.pop(_UNEVALUATED, None)
            node.pop(_PROPERTIES, None)
        for value in node.values():
            _rewrite_record_maps(value)
    elif isinstance(node, list):
        for item in node:
            _rewrite_record_maps(item)


def seal(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``schema`` with every object ``$def`` closed.

    Sets ``additionalProperties: false`` on each ``$def`` that is an object type,
    giving the at-rest closed content model (docs/design/typespec.md "At-rest vs
    on-the-wire"): the current schema reads every artifact ever written, additive
    changes stay compatible, and an unknown field fails loud as drift. Applied
    only to at-rest domains; wire schemas stay open for forward-tolerant readers.

    Union containers (an ``anyOf`` of variant ``$ref``s, no ``properties``) carry
    no ``type: object`` and are left open — their variant models are the objects
    that get sealed. A typed map (``Record<T>``, already carrying an
    ``additionalProperties`` schema after ``normalize``) is left untouched —
    sealing it to ``additionalProperties: false`` would reject every key.

    Args:
        schema: A normalized bundle (a ``$defs`` of types).

    Returns:
        A new dict with every object ``$def`` sealed.

    Raises:
        ValueError: If the schema has no ``$defs`` object.
    """
    defs = schema.get(_DEFS)
    if not isinstance(defs, dict):
        raise ValueError(f'schema has no {_DEFS} object; not a bundled TypeSpec emission')

    result = copy.deepcopy(schema)
    for subschema in result[_DEFS].values():
        # Scalar 'object' is complete here: a $def for a model is always a plain
        # object type. TypeSpec expresses nullability at reference sites (a
        # property's schema), never on the $def itself, so the list form
        # (type: ['object','null']) does not arise for a top-level $def.
        if isinstance(subschema, dict) and subschema.get('type') == 'object' and _ADDITIONAL not in subschema:
            subschema[_ADDITIONAL] = False
    return result


def normalize(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized copy of ``schema`` in the local-ref single-file form.

    Strips each ``$def``'s ``$id``/``$schema`` (the bundle root keeps both) and
    rewrites every ``$id``-relative ``$ref`` to ``#/$defs/…``. The input is not
    modified — a deep copy is normalized and returned.

    Args:
        schema: The bundled schema as emitted by ``@typespec/json-schema``.

    Returns:
        A new dict in the normalized single-file form.

    Raises:
        ValueError: If the schema has no ``$defs`` — not a bundle, so the
            #4084 workaround does not apply and something upstream is wrong.
    """
    defs = schema.get(_DEFS)
    if not isinstance(defs, dict):
        raise ValueError(f'schema has no {_DEFS} object; not a bundled TypeSpec emission')

    result = copy.deepcopy(schema)
    for subschema in result[_DEFS].values():
        if isinstance(subschema, dict):
            subschema.pop(_ID, None)
            subschema.pop(_SCHEMA, None)

    _rewrite_refs(result)
    _rewrite_record_maps(result)
    return result
