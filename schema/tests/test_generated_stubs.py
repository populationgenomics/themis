"""Verify the committed feature-corpus proto stub (``schema/tests/proto/features_pb2``).

The proto half of the round-trip verification: each construct in the corpus
(``schema/tests/fixtures/features/``) must survive ``@typespec/protobuf`` -> ``protoc``
into a usable message. Imports the committed stub (no Node/``protoc`` at test time) and
checks each construct's proto shape. Freshness against the ``.tsp`` sources is the S0.4 CI
gate; these run in the ordinary pytest job.
"""

from __future__ import annotations

import features_pb2
from google.protobuf import descriptor

_F = descriptor.FieldDescriptor


def _field(message: type, name: str) -> descriptor.FieldDescriptor:
    return message.DESCRIPTOR.fields_by_name[name]


def test_integer_enum() -> None:
    # Proto enums are integer with a zero-valued first member (string_enum.tsp).
    assert features_pb2.Colour.red == 0
    assert features_pb2.Colour.green == 1
    assert features_pb2.Colour.blue == 2
    colour = _field(features_pb2.EnumHolder, 'colour')
    assert colour.type == _F.TYPE_ENUM
    assert colour.enum_type is not None
    assert colour.enum_type.name == 'Colour'


def test_optional_presence() -> None:
    # `optional_field?` -> proto3 explicit presence; a plain singular scalar has none.
    holder = features_pb2.OptionalHolder
    assert _field(holder, 'required_field').type == _F.TYPE_STRING
    assert not _field(holder, 'required_field').has_presence
    assert _field(holder, 'optional_field').has_presence


def test_optional_with_default() -> None:
    # proto3 has no field defaults; the default drops, leaving an optional bool.
    flagged = _field(features_pb2.DefaultHolder, 'flagged')
    assert flagged.type == _F.TYPE_BOOL
    assert flagged.has_presence


def test_array() -> None:
    holder = features_pb2.ArrayHolder
    tags = _field(holder, 'tags')
    assert tags.is_repeated
    assert tags.type == _F.TYPE_STRING
    palette = _field(holder, 'palette')
    assert palette.is_repeated
    assert palette.type == _F.TYPE_ENUM
    assert palette.enum_type is not None
    assert palette.enum_type.name == 'Colour'


def test_nested_model() -> None:
    inner = _field(features_pb2.Outer, 'inner')
    assert inner.type == _F.TYPE_MESSAGE
    assert inner.message_type is not None
    assert inner.message_type.name == 'Inner'
    assert _field(features_pb2.Inner, 'value').type == _F.TYPE_STRING


def test_scalar_formats() -> None:
    holder = features_pb2.ScalarHolder
    assert _field(holder, 'count').type == _F.TYPE_INT32
    assert _field(holder, 'ratio').type == _F.TYPE_DOUBLE
    assert _field(holder, 'link').type == _F.TYPE_STRING
    when = _field(holder, 'when')
    assert when.type == _F.TYPE_MESSAGE
    assert when.message_type is not None
    assert when.message_type.full_name == 'google.protobuf.Timestamp'
