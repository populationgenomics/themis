from buf.validate import validate_pb2 as _validate_pb2
from grus.models import pedigree_pb2 as _pedigree_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PaperPedigrees(_message.Message):
    __slots__ = ("figures",)
    class FiguresEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: int
        value: _pedigree_pb2.PedigreeSet
        def __init__(self, key: _Optional[int] = ..., value: _Optional[_Union[_pedigree_pb2.PedigreeSet, _Mapping]] = ...) -> None: ...
    FIGURES_FIELD_NUMBER: _ClassVar[int]
    figures: _containers.MessageMap[int, _pedigree_pb2.PedigreeSet]
    def __init__(self, figures: _Optional[_Mapping[int, _pedigree_pb2.PedigreeSet]] = ...) -> None: ...
