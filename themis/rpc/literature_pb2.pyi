from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Representation(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    REPRESENTATION_UNSPECIFIED: _ClassVar[Representation]
    REPRESENTATION_MARKDOWN: _ClassVar[Representation]
    REPRESENTATION_PDF: _ClassVar[Representation]

class FileRole(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FILE_ROLE_UNSPECIFIED: _ClassVar[FileRole]
    FILE_ROLE_FIGURE: _ClassVar[FileRole]
    FILE_ROLE_SUPPLEMENTARY: _ClassVar[FileRole]

class FullTextState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FULL_TEXT_STATE_UNSPECIFIED: _ClassVar[FullTextState]
    FULL_TEXT_STATE_READY: _ClassVar[FullTextState]
    FULL_TEXT_STATE_PENDING: _ClassVar[FullTextState]
    FULL_TEXT_STATE_NO_FULL_TEXT: _ClassVar[FullTextState]
    FULL_TEXT_STATE_FAILED: _ClassVar[FullTextState]
    FULL_TEXT_STATE_UNKNOWN_PAPER: _ClassVar[FullTextState]
REPRESENTATION_UNSPECIFIED: Representation
REPRESENTATION_MARKDOWN: Representation
REPRESENTATION_PDF: Representation
FILE_ROLE_UNSPECIFIED: FileRole
FILE_ROLE_FIGURE: FileRole
FILE_ROLE_SUPPLEMENTARY: FileRole
FULL_TEXT_STATE_UNSPECIFIED: FullTextState
FULL_TEXT_STATE_READY: FullTextState
FULL_TEXT_STATE_PENDING: FullTextState
FULL_TEXT_STATE_NO_FULL_TEXT: FullTextState
FULL_TEXT_STATE_FAILED: FullTextState
FULL_TEXT_STATE_UNKNOWN_PAPER: FullTextState

class DescribePaperRequest(_message.Message):
    __slots__ = ("doc_id",)
    DOC_ID_FIELD_NUMBER: _ClassVar[int]
    doc_id: str
    def __init__(self, doc_id: _Optional[str] = ...) -> None: ...

class FileInfo(_message.Message):
    __slots__ = ("name", "role", "media_type")
    NAME_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    MEDIA_TYPE_FIELD_NUMBER: _ClassVar[int]
    name: str
    role: FileRole
    media_type: str
    def __init__(self, name: _Optional[str] = ..., role: _Optional[_Union[FileRole, str]] = ..., media_type: _Optional[str] = ...) -> None: ...

class PaperInfo(_message.Message):
    __slots__ = ("doc_id", "title", "has_markdown", "markdown_from_xml", "has_pdf", "default_representation", "files")
    DOC_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    HAS_MARKDOWN_FIELD_NUMBER: _ClassVar[int]
    MARKDOWN_FROM_XML_FIELD_NUMBER: _ClassVar[int]
    HAS_PDF_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_REPRESENTATION_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    doc_id: str
    title: str
    has_markdown: bool
    markdown_from_xml: bool
    has_pdf: bool
    default_representation: Representation
    files: _containers.RepeatedCompositeFieldContainer[FileInfo]
    def __init__(self, doc_id: _Optional[str] = ..., title: _Optional[str] = ..., has_markdown: _Optional[bool] = ..., markdown_from_xml: _Optional[bool] = ..., has_pdf: _Optional[bool] = ..., default_representation: _Optional[_Union[Representation, str]] = ..., files: _Optional[_Iterable[_Union[FileInfo, _Mapping]]] = ...) -> None: ...

class MarkdownSelector(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class PdfSelector(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class FileSelector(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class ResolveContentRequest(_message.Message):
    __slots__ = ("doc_id", "markdown", "pdf", "file")
    DOC_ID_FIELD_NUMBER: _ClassVar[int]
    MARKDOWN_FIELD_NUMBER: _ClassVar[int]
    PDF_FIELD_NUMBER: _ClassVar[int]
    FILE_FIELD_NUMBER: _ClassVar[int]
    doc_id: str
    markdown: MarkdownSelector
    pdf: PdfSelector
    file: FileSelector
    def __init__(self, doc_id: _Optional[str] = ..., markdown: _Optional[_Union[MarkdownSelector, _Mapping]] = ..., pdf: _Optional[_Union[PdfSelector, _Mapping]] = ..., file: _Optional[_Union[FileSelector, _Mapping]] = ...) -> None: ...

class ContentLocation(_message.Message):
    __slots__ = ("gcs_uri", "media_type")
    GCS_URI_FIELD_NUMBER: _ClassVar[int]
    MEDIA_TYPE_FIELD_NUMBER: _ClassVar[int]
    gcs_uri: str
    media_type: str
    def __init__(self, gcs_uri: _Optional[str] = ..., media_type: _Optional[str] = ...) -> None: ...

class LocateRequest(_message.Message):
    __slots__ = ("doc_id", "quote", "representation")
    DOC_ID_FIELD_NUMBER: _ClassVar[int]
    QUOTE_FIELD_NUMBER: _ClassVar[int]
    REPRESENTATION_FIELD_NUMBER: _ClassVar[int]
    doc_id: str
    quote: str
    representation: Representation
    def __init__(self, doc_id: _Optional[str] = ..., quote: _Optional[str] = ..., representation: _Optional[_Union[Representation, str]] = ...) -> None: ...

class TextOffsets(_message.Message):
    __slots__ = ("start", "end")
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    start: int
    end: int
    def __init__(self, start: _Optional[int] = ..., end: _Optional[int] = ...) -> None: ...

class Rect(_message.Message):
    __slots__ = ("x", "y", "width", "height")
    X_FIELD_NUMBER: _ClassVar[int]
    Y_FIELD_NUMBER: _ClassVar[int]
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    x: float
    y: float
    width: float
    height: float
    def __init__(self, x: _Optional[float] = ..., y: _Optional[float] = ..., width: _Optional[float] = ..., height: _Optional[float] = ...) -> None: ...

class PdfRegion(_message.Message):
    __slots__ = ("page", "rects")
    PAGE_FIELD_NUMBER: _ClassVar[int]
    RECTS_FIELD_NUMBER: _ClassVar[int]
    page: int
    rects: _containers.RepeatedCompositeFieldContainer[Rect]
    def __init__(self, page: _Optional[int] = ..., rects: _Optional[_Iterable[_Union[Rect, _Mapping]]] = ...) -> None: ...

class QuoteNotLocated(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class LocateResponse(_message.Message):
    __slots__ = ("offsets", "region", "not_located")
    OFFSETS_FIELD_NUMBER: _ClassVar[int]
    REGION_FIELD_NUMBER: _ClassVar[int]
    NOT_LOCATED_FIELD_NUMBER: _ClassVar[int]
    offsets: TextOffsets
    region: PdfRegion
    not_located: QuoteNotLocated
    def __init__(self, offsets: _Optional[_Union[TextOffsets, _Mapping]] = ..., region: _Optional[_Union[PdfRegion, _Mapping]] = ..., not_located: _Optional[_Union[QuoteNotLocated, _Mapping]] = ...) -> None: ...

class FullTextReadiness(_message.Message):
    __slots__ = ("doc_id", "state")
    DOC_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    doc_id: str
    state: FullTextState
    def __init__(self, doc_id: _Optional[str] = ..., state: _Optional[_Union[FullTextState, str]] = ...) -> None: ...

class PollFullTextsRequest(_message.Message):
    __slots__ = ("doc_ids",)
    DOC_IDS_FIELD_NUMBER: _ClassVar[int]
    doc_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, doc_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class PollFullTextsResponse(_message.Message):
    __slots__ = ("readiness",)
    READINESS_FIELD_NUMBER: _ClassVar[int]
    readiness: _containers.RepeatedCompositeFieldContainer[FullTextReadiness]
    def __init__(self, readiness: _Optional[_Iterable[_Union[FullTextReadiness, _Mapping]]] = ...) -> None: ...

class PaperReadiness(_message.Message):
    __slots__ = ("external_id", "doc_id", "state")
    EXTERNAL_ID_FIELD_NUMBER: _ClassVar[int]
    DOC_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    external_id: str
    doc_id: str
    state: FullTextState
    def __init__(self, external_id: _Optional[str] = ..., doc_id: _Optional[str] = ..., state: _Optional[_Union[FullTextState, str]] = ...) -> None: ...

class MaybeIngestPapersRequest(_message.Message):
    __slots__ = ("external_ids",)
    EXTERNAL_IDS_FIELD_NUMBER: _ClassVar[int]
    external_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, external_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class MaybeIngestPapersResponse(_message.Message):
    __slots__ = ("readiness",)
    READINESS_FIELD_NUMBER: _ClassVar[int]
    readiness: _containers.RepeatedCompositeFieldContainer[PaperReadiness]
    def __init__(self, readiness: _Optional[_Iterable[_Union[PaperReadiness, _Mapping]]] = ...) -> None: ...

class ValidateRequest(_message.Message):
    __slots__ = ("doc_id", "quote")
    DOC_ID_FIELD_NUMBER: _ClassVar[int]
    QUOTE_FIELD_NUMBER: _ClassVar[int]
    doc_id: str
    quote: str
    def __init__(self, doc_id: _Optional[str] = ..., quote: _Optional[str] = ...) -> None: ...

class ValidateResponse(_message.Message):
    __slots__ = ("ok", "located_in", "reason")
    OK_FIELD_NUMBER: _ClassVar[int]
    LOCATED_IN_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    located_in: _containers.RepeatedScalarFieldContainer[Representation]
    reason: str
    def __init__(self, ok: _Optional[bool] = ..., located_in: _Optional[_Iterable[_Union[Representation, str]]] = ..., reason: _Optional[str] = ...) -> None: ...
