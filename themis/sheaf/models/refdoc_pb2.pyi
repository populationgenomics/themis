from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RefTarget(_message.Message):
    __slots__ = ("oid", "ref")
    OID_FIELD_NUMBER: _ClassVar[int]
    REF_FIELD_NUMBER: _ClassVar[int]
    oid: str
    ref: str
    def __init__(self, oid: _Optional[str] = ..., ref: _Optional[str] = ...) -> None: ...

class RefDoc(_message.Message):
    __slots__ = ("refs", "packs", "head")
    class RefsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: RefTarget
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[RefTarget, _Mapping]] = ...) -> None: ...
    REFS_FIELD_NUMBER: _ClassVar[int]
    PACKS_FIELD_NUMBER: _ClassVar[int]
    HEAD_FIELD_NUMBER: _ClassVar[int]
    refs: _containers.MessageMap[str, RefTarget]
    packs: _containers.RepeatedScalarFieldContainer[str]
    head: RefTarget
    def __init__(self, refs: _Optional[_Mapping[str, RefTarget]] = ..., packs: _Optional[_Iterable[str]] = ..., head: _Optional[_Union[RefTarget, _Mapping]] = ...) -> None: ...
