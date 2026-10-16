"""A message's undeclared contents are found wherever they sit, so nothing that reads it loses them unawares."""

from __future__ import annotations

from google.protobuf import struct_pb2

from themis.svcv4.models import svcv4_pb2
from themis.widgets.models import svcv4_classification_pb2
from tools.run_review import protos

# Field 1000 with a varint payload: what a newer writer's field reads as to these stubs.
_UNKNOWN_FIELD = bytes([0xC0, 0x3E, 0x01])


def test_a_message_its_schema_names_whole_has_nothing_undeclared() -> None:
    record = svcv4_classification_pb2.Svcv4Classification(classification=svcv4_pb2.CLASSIFICATION_VUS)
    assert protos.undeclared(record) == []


def test_an_unknown_field_inside_a_map_value_is_found() -> None:
    struct = struct_pb2.Struct()
    struct.fields['k'].struct_value.MergeFromString(_UNKNOWN_FIELD)
    [found] = protos.undeclared(struct)
    assert 'google.protobuf.Struct field number(s) [1000]' in found


def test_an_enum_number_inside_a_repeated_message_is_found() -> None:
    record = svcv4_classification_pb2.Svcv4Classification(codes=[svcv4_classification_pb2.Svcv4Classification.Code()])
    record.codes[0].status = 42  # pyright: ignore[reportAttributeAccessIssue]
    [found] = protos.undeclared(record)
    assert 'Code.status enum number(s) [42]' in found
