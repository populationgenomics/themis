"""The JSON Schema the pedigree reader's answer follows, derived from grus's pedigree set descriptor.

Built from the proto rather than written out, so it follows whatever grus version is pinned. It narrows the record
to what a model writes:
- `Provenance` is dropped: it is harness metadata, never read off a figure.
- A citation's `document_id` and a region's `page` are dropped: the harness stamps both.
- Each enum omits its zero, so the model cannot write the sentinel grus's loader rejects.
- A deprecated field is dropped, so a retired field is never offered.

Only the JSON Schema subset structured answers use is emitted (types, `enum`, `required`, `additionalProperties`);
bounds such as `min_len` are left to grus's loader, which checks the answer.
"""

from __future__ import annotations

from typing import cast

from google.protobuf import descriptor
from grus.models import pedigree_pb2

# The fields the model must always write. Everything else is optional: booleans default false, and an absent enum is
# filled per the prompt. Keyed by full name, so a renumbering cannot break it.
_REQUIRED: dict[str, tuple[str, ...]] = {
    pedigree_pb2.PedigreeSet.DESCRIPTOR.full_name: ('pedigrees',),  # [] is the answer for a page with none
    pedigree_pb2.Pedigree.DESCRIPTOR.full_name: ('individuals',),
    pedigree_pb2.Position.DESCRIPTOR.full_name: ('generation', 'index'),
    pedigree_pb2.Individual.DESCRIPTOR.full_name: ('generation', 'index', 'gender'),
    pedigree_pb2.ConditionDef.DESCRIPTOR.full_name: ('id',),  # an empty name is the figure's sole, unnamed condition
    pedigree_pb2.Condition.DESCRIPTOR.full_name: ('condition_id', 'status'),
    pedigree_pb2.Annotation.DESCRIPTOR.full_name: ('text', 'type'),
    pedigree_pb2.Mating.DESCRIPTOR.full_name: (),  # no partner is a founder sibship; one is a single parent
    pedigree_pb2.Offspring.DESCRIPTOR.full_name: ('child',),
    pedigree_pb2.Label.DESCRIPTOR.full_name: ('text', 'kind'),
    pedigree_pb2.Citation.DESCRIPTOR.full_name: ('id',),
    pedigree_pb2.Region.DESCRIPTOR.full_name: ('x0', 'y0', 'x1', 'y1'),
    pedigree_pb2.Support.DESCRIPTOR.full_name: ('citations', 'field'),
    pedigree_pb2.Locus.DESCRIPTOR.full_name: ('id', 'alleles'),
    pedigree_pb2.Allele.DESCRIPTOR.full_name: ('symbol',),
    pedigree_pb2.Identifier.DESCRIPTOR.full_name: ('scheme', 'value'),
    pedigree_pb2.Genotype.DESCRIPTOR.full_name: ('locus_id', 'alleles'),
}
_OMIT_MESSAGES = frozenset({pedigree_pb2.Provenance.DESCRIPTOR.full_name})
_HARNESS_FIELDS = frozenset(
    {
        pedigree_pb2.Citation.DESCRIPTOR.fields_by_name['document_id'].full_name,
        pedigree_pb2.Region.DESCRIPTOR.fields_by_name['page'].full_name,
    }
)
_SCALAR_JSON_TYPE: dict[int, str] = {
    descriptor.FieldDescriptor.TYPE_STRING: 'string',
    descriptor.FieldDescriptor.TYPE_BOOL: 'boolean',
    descriptor.FieldDescriptor.TYPE_INT32: 'integer',
    descriptor.FieldDescriptor.TYPE_FLOAT: 'number',
    descriptor.FieldDescriptor.TYPE_DOUBLE: 'number',
}


def _field_schema(field: descriptor.FieldDescriptor) -> dict[str, object]:
    schema: dict[str, object]
    if field.message_type is not None:
        schema = _message_schema(field.message_type)
    elif field.enum_type is not None:
        schema = {'type': 'string', 'enum': [v.name for v in field.enum_type.values if v.number != 0]}
    else:
        json_type = _SCALAR_JSON_TYPE.get(field.type)
        if json_type is None:
            raise TypeError(f'no JSON Schema type for the proto field {field.full_name} (type {field.type})')
        schema = {'type': json_type}
    return {'type': 'array', 'items': schema} if field.is_repeated else schema


def _message_schema(message: descriptor.Descriptor) -> dict[str, object]:
    properties: dict[str, object] = {}
    for field in message.fields:
        if field.message_type is not None and field.message_type.full_name in _OMIT_MESSAGES:
            continue
        if field.full_name in _HARNESS_FIELDS or field.GetOptions().deprecated:
            continue
        properties[field.name] = _field_schema(field)
    return {
        'type': 'object',
        'properties': properties,
        'required': list(_REQUIRED.get(message.full_name, ())),
        'additionalProperties': False,
    }


def pedigree_set_schema() -> dict[str, object]:
    """The schema of one answer: every pedigree on a page or in a figure, with its citations."""
    return _message_schema(cast(descriptor.Descriptor, pedigree_pb2.PedigreeSet.DESCRIPTOR))
