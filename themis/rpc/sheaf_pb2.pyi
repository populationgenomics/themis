import datetime

from google.protobuf import empty_pb2 as _empty_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from themis.sheaf.models import refdoc_pb2 as _refdoc_pb2
from themis.rpc import sandbox_options_pb2 as _sandbox_options_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RefDocSnapshot(_message.Message):
    __slots__ = ("document", "generation")
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    document: _refdoc_pb2.RefDoc
    generation: int
    def __init__(self, document: _Optional[_Union[_refdoc_pb2.RefDoc, _Mapping]] = ..., generation: _Optional[int] = ...) -> None: ...

class FetchPackRequest(_message.Message):
    __slots__ = ("pack_id",)
    PACK_ID_FIELD_NUMBER: _ClassVar[int]
    pack_id: str
    def __init__(self, pack_id: _Optional[str] = ...) -> None: ...

class PackChunk(_message.Message):
    __slots__ = ("content",)
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    content: bytes
    def __init__(self, content: _Optional[bytes] = ...) -> None: ...

class RefUpdate(_message.Message):
    __slots__ = ("old", "new")
    OLD_FIELD_NUMBER: _ClassVar[int]
    NEW_FIELD_NUMBER: _ClassVar[int]
    old: str
    new: str
    def __init__(self, old: _Optional[str] = ..., new: _Optional[str] = ...) -> None: ...

class PublishIntent(_message.Message):
    __slots__ = ("base_generation", "ref_updates", "head", "packs")
    class RefUpdatesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: RefUpdate
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[RefUpdate, _Mapping]] = ...) -> None: ...
    BASE_GENERATION_FIELD_NUMBER: _ClassVar[int]
    REF_UPDATES_FIELD_NUMBER: _ClassVar[int]
    HEAD_FIELD_NUMBER: _ClassVar[int]
    PACKS_FIELD_NUMBER: _ClassVar[int]
    base_generation: int
    ref_updates: _containers.MessageMap[str, RefUpdate]
    head: _refdoc_pb2.RefTarget
    packs: _containers.RepeatedCompositeFieldContainer[PackDescriptor]
    def __init__(self, base_generation: _Optional[int] = ..., ref_updates: _Optional[_Mapping[str, RefUpdate]] = ..., head: _Optional[_Union[_refdoc_pb2.RefTarget, _Mapping]] = ..., packs: _Optional[_Iterable[_Union[PackDescriptor, _Mapping]]] = ...) -> None: ...

class PackDescriptor(_message.Message):
    __slots__ = ("size", "pack_id")
    SIZE_FIELD_NUMBER: _ClassVar[int]
    PACK_ID_FIELD_NUMBER: _ClassVar[int]
    size: int
    pack_id: str
    def __init__(self, size: _Optional[int] = ..., pack_id: _Optional[str] = ...) -> None: ...

class PublishRequest(_message.Message):
    __slots__ = ("intent", "chunk")
    INTENT_FIELD_NUMBER: _ClassVar[int]
    CHUNK_FIELD_NUMBER: _ClassVar[int]
    intent: PublishIntent
    chunk: PublishChunk
    def __init__(self, intent: _Optional[_Union[PublishIntent, _Mapping]] = ..., chunk: _Optional[_Union[PublishChunk, _Mapping]] = ...) -> None: ...

class PublishChunk(_message.Message):
    __slots__ = ("pack", "content")
    PACK_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    pack: int
    content: bytes
    def __init__(self, pack: _Optional[int] = ..., content: _Optional[bytes] = ...) -> None: ...

class SignPackUrlsRequest(_message.Message):
    __slots__ = ("pack_ids",)
    PACK_IDS_FIELD_NUMBER: _ClassVar[int]
    pack_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, pack_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class SignPackUrlsResponse(_message.Message):
    __slots__ = ("packs",)
    PACKS_FIELD_NUMBER: _ClassVar[int]
    packs: _containers.RepeatedCompositeFieldContainer[SignedPack]
    def __init__(self, packs: _Optional[_Iterable[_Union[SignedPack, _Mapping]]] = ...) -> None: ...

class SignedPack(_message.Message):
    __slots__ = ("pack_id", "url", "size", "expire_time")
    PACK_ID_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    EXPIRE_TIME_FIELD_NUMBER: _ClassVar[int]
    pack_id: str
    url: str
    size: int
    expire_time: _timestamp_pb2.Timestamp
    def __init__(self, pack_id: _Optional[str] = ..., url: _Optional[str] = ..., size: _Optional[int] = ..., expire_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class PublishResponse(_message.Message):
    __slots__ = ("generation",)
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    generation: int
    def __init__(self, generation: _Optional[int] = ...) -> None: ...
