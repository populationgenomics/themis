from google.protobuf import descriptor_pb2 as _descriptor_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor
WIDGET_FIELD_NUMBER: _ClassVar[int]
widget: _descriptor.FieldDescriptor
GUARD_FIELD_NUMBER: _ClassVar[int]
guard: _descriptor.FieldDescriptor
ELEMENT_KEY_FIELD_NUMBER: _ClassVar[int]
element_key: _descriptor.FieldDescriptor

class Guard(_message.Message):
    __slots__ = ("ignores",)
    IGNORES_FIELD_NUMBER: _ClassVar[int]
    ignores: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, ignores: _Optional[_Iterable[str]] = ...) -> None: ...
