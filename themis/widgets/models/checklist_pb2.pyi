from buf.validate import validate_pb2 as _validate_pb2
from themis.widgets.models import widget_pb2 as _widget_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Checklist(_message.Message):
    __slots__ = ("items",)
    class Citation(_message.Message):
        __slots__ = ("doc_id", "quote")
        DOC_ID_FIELD_NUMBER: _ClassVar[int]
        QUOTE_FIELD_NUMBER: _ClassVar[int]
        doc_id: str
        quote: str
        def __init__(self, doc_id: _Optional[str] = ..., quote: _Optional[str] = ...) -> None: ...
    class Item(_message.Message):
        __slots__ = ("checked", "id", "label", "citation")
        CHECKED_FIELD_NUMBER: _ClassVar[int]
        ID_FIELD_NUMBER: _ClassVar[int]
        LABEL_FIELD_NUMBER: _ClassVar[int]
        CITATION_FIELD_NUMBER: _ClassVar[int]
        checked: bool
        id: str
        label: str
        citation: Checklist.Citation
        def __init__(self, checked: _Optional[bool] = ..., id: _Optional[str] = ..., label: _Optional[str] = ..., citation: _Optional[_Union[Checklist.Citation, _Mapping]] = ...) -> None: ...
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[Checklist.Item]
    def __init__(self, items: _Optional[_Iterable[_Union[Checklist.Item, _Mapping]]] = ...) -> None: ...
