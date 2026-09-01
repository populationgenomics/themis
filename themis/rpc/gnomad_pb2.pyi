from google.protobuf import struct_pb2 as _struct_pb2
from themis.evidence.models import evidence_pb2 as _evidence_pb2
from themis.rpc import sandbox_options_pb2 as _sandbox_options_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class DescribeVariantRequest(_message.Message):
    __slots__ = ("gnomad_id", "dataset", "cooccurrence_with")
    GNOMAD_ID_FIELD_NUMBER: _ClassVar[int]
    DATASET_FIELD_NUMBER: _ClassVar[int]
    COOCCURRENCE_WITH_FIELD_NUMBER: _ClassVar[int]
    gnomad_id: str
    dataset: str
    cooccurrence_with: str
    def __init__(self, gnomad_id: _Optional[str] = ..., dataset: _Optional[str] = ..., cooccurrence_with: _Optional[str] = ...) -> None: ...

class DescribeVariantResponse(_message.Message):
    __slots__ = ("raw", "provenance")
    RAW_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    raw: _struct_pb2.Struct
    provenance: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.Provenance]
    def __init__(self, raw: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., provenance: _Optional[_Iterable[_Union[_evidence_pb2.Provenance, _Mapping]]] = ...) -> None: ...
