from google.protobuf import struct_pb2 as _struct_pb2
from themis.evidence.models import evidence_pb2 as _evidence_pb2
from themis.rpc import sandbox_options_pb2 as _sandbox_options_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AnnotateRequest(_message.Message):
    __slots__ = ("variant", "predictors")
    VARIANT_FIELD_NUMBER: _ClassVar[int]
    PREDICTORS_FIELD_NUMBER: _ClassVar[int]
    variant: str
    predictors: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, variant: _Optional[str] = ..., predictors: _Optional[_Iterable[str]] = ...) -> None: ...

class AnnotateResponse(_message.Message):
    __slots__ = ("most_severe_consequence", "raw", "provenance")
    MOST_SEVERE_CONSEQUENCE_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    most_severe_consequence: _evidence_pb2.Consequence
    raw: _struct_pb2.Struct
    provenance: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.Provenance]
    def __init__(self, most_severe_consequence: _Optional[_Union[_evidence_pb2.Consequence, str]] = ..., raw: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., provenance: _Optional[_Iterable[_Union[_evidence_pb2.Provenance, _Mapping]]] = ...) -> None: ...
