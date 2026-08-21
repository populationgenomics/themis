from google.protobuf import struct_pb2 as _struct_pb2
from themis.evidence.models import evidence_pb2 as _evidence_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class DescribeVariantRequest(_message.Message):
    __slots__ = ("variant",)
    VARIANT_FIELD_NUMBER: _ClassVar[int]
    variant: str
    def __init__(self, variant: _Optional[str] = ...) -> None: ...

class DescribeVariantResponse(_message.Message):
    __slots__ = ("oddspath_ratio", "acmg_criterion", "acmg_strength", "score", "raw", "provenance")
    ODDSPATH_RATIO_FIELD_NUMBER: _ClassVar[int]
    ACMG_CRITERION_FIELD_NUMBER: _ClassVar[int]
    ACMG_STRENGTH_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    oddspath_ratio: float
    acmg_criterion: str
    acmg_strength: str
    score: float
    raw: _struct_pb2.Struct
    provenance: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.Provenance]
    def __init__(self, oddspath_ratio: _Optional[float] = ..., acmg_criterion: _Optional[str] = ..., acmg_strength: _Optional[str] = ..., score: _Optional[float] = ..., raw: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., provenance: _Optional[_Iterable[_Union[_evidence_pb2.Provenance, _Mapping]]] = ...) -> None: ...
