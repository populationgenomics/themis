from buf.validate import validate_pb2 as _validate_pb2
from themis.rpc import literature_pb2 as _literature_pb2
from themis.rpc import sheaf_pb2 as _sheaf_pb2
from themis.workbench.models import workbench_pb2 as _workbench_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ReadWorkspaceRefDocRequest(_message.Message):
    __slots__ = ("analysis_id",)
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    def __init__(self, analysis_id: _Optional[str] = ...) -> None: ...

class SignWorkspacePackUrlsRequest(_message.Message):
    __slots__ = ("analysis_id", "pack_ids")
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    PACK_IDS_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    pack_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, analysis_id: _Optional[str] = ..., pack_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class PublishWorkspaceRequest(_message.Message):
    __slots__ = ("analysis_id", "intent", "pack_bytes")
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    INTENT_FIELD_NUMBER: _ClassVar[int]
    PACK_BYTES_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    intent: _sheaf_pb2.PublishIntent
    pack_bytes: _containers.RepeatedScalarFieldContainer[bytes]
    def __init__(self, analysis_id: _Optional[str] = ..., intent: _Optional[_Union[_sheaf_pb2.PublishIntent, _Mapping]] = ..., pack_bytes: _Optional[_Iterable[bytes]] = ...) -> None: ...
