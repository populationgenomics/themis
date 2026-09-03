import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Consequence(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONSEQUENCE_UNSPECIFIED: _ClassVar[Consequence]
    CONSEQUENCE_MISSENSE: _ClassVar[Consequence]
    CONSEQUENCE_NONSENSE: _ClassVar[Consequence]
    CONSEQUENCE_FRAMESHIFT: _ClassVar[Consequence]
    CONSEQUENCE_CANONICAL_SPLICE: _ClassVar[Consequence]
    CONSEQUENCE_INTRONIC: _ClassVar[Consequence]
    CONSEQUENCE_SYNONYMOUS: _ClassVar[Consequence]
    CONSEQUENCE_INFRAME_INDEL: _ClassVar[Consequence]
    CONSEQUENCE_START_LOST: _ClassVar[Consequence]
    CONSEQUENCE_STOP_LOST: _ClassVar[Consequence]
    CONSEQUENCE_EXON_DELETION: _ClassVar[Consequence]
    CONSEQUENCE_EXON_DUPLICATION: _ClassVar[Consequence]
    CONSEQUENCE_NON_CODING: _ClassVar[Consequence]

class Inheritance(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    INHERITANCE_UNSPECIFIED: _ClassVar[Inheritance]
    INHERITANCE_AUTOSOMAL_DOMINANT: _ClassVar[Inheritance]
    INHERITANCE_AUTOSOMAL_RECESSIVE: _ClassVar[Inheritance]
    INHERITANCE_X_LINKED: _ClassVar[Inheritance]
    INHERITANCE_Y_LINKED: _ClassVar[Inheritance]
    INHERITANCE_MITOCHONDRIAL: _ClassVar[Inheritance]
    INHERITANCE_SEMIDOMINANT: _ClassVar[Inheritance]
    INHERITANCE_UNDETERMINED: _ClassVar[Inheritance]
CONSEQUENCE_UNSPECIFIED: Consequence
CONSEQUENCE_MISSENSE: Consequence
CONSEQUENCE_NONSENSE: Consequence
CONSEQUENCE_FRAMESHIFT: Consequence
CONSEQUENCE_CANONICAL_SPLICE: Consequence
CONSEQUENCE_INTRONIC: Consequence
CONSEQUENCE_SYNONYMOUS: Consequence
CONSEQUENCE_INFRAME_INDEL: Consequence
CONSEQUENCE_START_LOST: Consequence
CONSEQUENCE_STOP_LOST: Consequence
CONSEQUENCE_EXON_DELETION: Consequence
CONSEQUENCE_EXON_DUPLICATION: Consequence
CONSEQUENCE_NON_CODING: Consequence
INHERITANCE_UNSPECIFIED: Inheritance
INHERITANCE_AUTOSOMAL_DOMINANT: Inheritance
INHERITANCE_AUTOSOMAL_RECESSIVE: Inheritance
INHERITANCE_X_LINKED: Inheritance
INHERITANCE_Y_LINKED: Inheritance
INHERITANCE_MITOCHONDRIAL: Inheritance
INHERITANCE_SEMIDOMINANT: Inheritance
INHERITANCE_UNDETERMINED: Inheritance

class Provenance(_message.Message):
    __slots__ = ("source", "dataset_versions", "query", "retrieved_at")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    DATASET_VERSIONS_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    RETRIEVED_AT_FIELD_NUMBER: _ClassVar[int]
    source: str
    dataset_versions: _containers.RepeatedScalarFieldContainer[str]
    query: str
    retrieved_at: _timestamp_pb2.Timestamp
    def __init__(self, source: _Optional[str] = ..., dataset_versions: _Optional[_Iterable[str]] = ..., query: _Optional[str] = ..., retrieved_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class GenomicSpan(_message.Message):
    __slots__ = ("start", "end")
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    start: int
    end: int
    def __init__(self, start: _Optional[int] = ..., end: _Optional[int] = ...) -> None: ...
