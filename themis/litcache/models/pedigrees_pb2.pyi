from buf.validate import validate_pb2 as _validate_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PedigreeExtractionConstruction(_message.Message):
    __slots__ = ("model", "harness_version", "figures_read")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    HARNESS_VERSION_FIELD_NUMBER: _ClassVar[int]
    FIGURES_READ_FIELD_NUMBER: _ClassVar[int]
    model: str
    harness_version: str
    figures_read: _containers.RepeatedCompositeFieldContainer[PedigreeFigure]
    def __init__(self, model: _Optional[str] = ..., harness_version: _Optional[str] = ..., figures_read: _Optional[_Iterable[_Union[PedigreeFigure, _Mapping]]] = ...) -> None: ...

class PedigreeFigure(_message.Message):
    __slots__ = ("figure", "failures")
    FIGURE_FIELD_NUMBER: _ClassVar[int]
    FAILURES_FIELD_NUMBER: _ClassVar[int]
    figure: int
    failures: _containers.RepeatedCompositeFieldContainer[PageFailure]
    def __init__(self, figure: _Optional[int] = ..., failures: _Optional[_Iterable[_Union[PageFailure, _Mapping]]] = ...) -> None: ...

class PageFailure(_message.Message):
    __slots__ = ("pdf_page", "reason")
    PDF_PAGE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    pdf_page: int
    reason: str
    def __init__(self, pdf_page: _Optional[int] = ..., reason: _Optional[str] = ...) -> None: ...
