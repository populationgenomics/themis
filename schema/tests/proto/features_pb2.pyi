import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Colour(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    red: _ClassVar[Colour]
    green: _ClassVar[Colour]
    blue: _ClassVar[Colour]
red: Colour
green: Colour
blue: Colour

class EnumHolder(_message.Message):
    __slots__ = ("colour",)
    COLOUR_FIELD_NUMBER: _ClassVar[int]
    colour: Colour
    def __init__(self, colour: _Optional[_Union[Colour, str]] = ...) -> None: ...

class OptionalHolder(_message.Message):
    __slots__ = ("required_field", "optional_field")
    REQUIRED_FIELD_FIELD_NUMBER: _ClassVar[int]
    OPTIONAL_FIELD_FIELD_NUMBER: _ClassVar[int]
    required_field: str
    optional_field: str
    def __init__(self, required_field: _Optional[str] = ..., optional_field: _Optional[str] = ...) -> None: ...

class DefaultHolder(_message.Message):
    __slots__ = ("flagged",)
    FLAGGED_FIELD_NUMBER: _ClassVar[int]
    flagged: bool
    def __init__(self, flagged: _Optional[bool] = ...) -> None: ...

class Inner(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: str
    def __init__(self, value: _Optional[str] = ...) -> None: ...

class Outer(_message.Message):
    __slots__ = ("inner",)
    INNER_FIELD_NUMBER: _ClassVar[int]
    inner: Inner
    def __init__(self, inner: _Optional[_Union[Inner, _Mapping]] = ...) -> None: ...

class ArrayHolder(_message.Message):
    __slots__ = ("tags", "palette")
    TAGS_FIELD_NUMBER: _ClassVar[int]
    PALETTE_FIELD_NUMBER: _ClassVar[int]
    tags: _containers.RepeatedScalarFieldContainer[str]
    palette: _containers.RepeatedScalarFieldContainer[Colour]
    def __init__(self, tags: _Optional[_Iterable[str]] = ..., palette: _Optional[_Iterable[_Union[Colour, str]]] = ...) -> None: ...

class ScalarHolder(_message.Message):
    __slots__ = ("count", "ratio", "when", "link")
    COUNT_FIELD_NUMBER: _ClassVar[int]
    RATIO_FIELD_NUMBER: _ClassVar[int]
    WHEN_FIELD_NUMBER: _ClassVar[int]
    LINK_FIELD_NUMBER: _ClassVar[int]
    count: int
    ratio: float
    when: _timestamp_pb2.Timestamp
    link: str
    def __init__(self, count: _Optional[int] = ..., ratio: _Optional[float] = ..., when: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., link: _Optional[str] = ...) -> None: ...
