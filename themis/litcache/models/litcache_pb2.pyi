import datetime

from buf.validate import validate_pb2 as _validate_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SourceKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SOURCE_KIND_UNSPECIFIED: _ClassVar[SourceKind]
    SOURCE_KIND_PMC_OA_S3: _ClassVar[SourceKind]
    SOURCE_KIND_EUROPE_PMC: _ClassVar[SourceKind]
    SOURCE_KIND_ELSEVIER_OA: _ClassVar[SourceKind]
    SOURCE_KIND_BIORXIV: _ClassVar[SourceKind]
    SOURCE_KIND_UPLOAD: _ClassVar[SourceKind]
    SOURCE_KIND_SEED: _ClassVar[SourceKind]

class SourceFormat(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SOURCE_FORMAT_UNSPECIFIED: _ClassVar[SourceFormat]
    SOURCE_FORMAT_XML: _ClassVar[SourceFormat]
    SOURCE_FORMAT_PDF: _ClassVar[SourceFormat]

class LicenceBasis(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    LICENCE_BASIS_UNSPECIFIED: _ClassVar[LicenceBasis]
    LICENCE_BASIS_ARTIFACT: _ClassVar[LicenceBasis]
    LICENCE_BASIS_ASSERTED: _ClassVar[LicenceBasis]

class Converter(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONVERTER_UNSPECIFIED: _ClassVar[Converter]
    CONVERTER_LITDOWN: _ClassVar[Converter]
    CONVERTER_DOCLING: _ClassVar[Converter]
    CONVERTER_LLM_OCR: _ClassVar[Converter]

class AssociatedFileRole(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ASSOCIATED_FILE_ROLE_UNSPECIFIED: _ClassVar[AssociatedFileRole]
    ASSOCIATED_FILE_ROLE_FIGURE: _ClassVar[AssociatedFileRole]
    ASSOCIATED_FILE_ROLE_SUPPLEMENTARY: _ClassVar[AssociatedFileRole]
SOURCE_KIND_UNSPECIFIED: SourceKind
SOURCE_KIND_PMC_OA_S3: SourceKind
SOURCE_KIND_EUROPE_PMC: SourceKind
SOURCE_KIND_ELSEVIER_OA: SourceKind
SOURCE_KIND_BIORXIV: SourceKind
SOURCE_KIND_UPLOAD: SourceKind
SOURCE_KIND_SEED: SourceKind
SOURCE_FORMAT_UNSPECIFIED: SourceFormat
SOURCE_FORMAT_XML: SourceFormat
SOURCE_FORMAT_PDF: SourceFormat
LICENCE_BASIS_UNSPECIFIED: LicenceBasis
LICENCE_BASIS_ARTIFACT: LicenceBasis
LICENCE_BASIS_ASSERTED: LicenceBasis
CONVERTER_UNSPECIFIED: Converter
CONVERTER_LITDOWN: Converter
CONVERTER_DOCLING: Converter
CONVERTER_LLM_OCR: Converter
ASSOCIATED_FILE_ROLE_UNSPECIFIED: AssociatedFileRole
ASSOCIATED_FILE_ROLE_FIGURE: AssociatedFileRole
ASSOCIATED_FILE_ROLE_SUPPLEMENTARY: AssociatedFileRole

class FreeToRead(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Licensed(_message.Message):
    __slots__ = ("publisher",)
    PUBLISHER_FIELD_NUMBER: _ClassVar[int]
    publisher: str
    def __init__(self, publisher: _Optional[str] = ...) -> None: ...

class InstitutionCaptured(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class UnknownAccess(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Access(_message.Message):
    __slots__ = ("free_to_read", "licensed", "institution_captured", "unknown")
    FREE_TO_READ_FIELD_NUMBER: _ClassVar[int]
    LICENSED_FIELD_NUMBER: _ClassVar[int]
    INSTITUTION_CAPTURED_FIELD_NUMBER: _ClassVar[int]
    UNKNOWN_FIELD_NUMBER: _ClassVar[int]
    free_to_read: FreeToRead
    licensed: Licensed
    institution_captured: InstitutionCaptured
    unknown: UnknownAccess
    def __init__(self, free_to_read: _Optional[_Union[FreeToRead, _Mapping]] = ..., licensed: _Optional[_Union[Licensed, _Mapping]] = ..., institution_captured: _Optional[_Union[InstitutionCaptured, _Mapping]] = ..., unknown: _Optional[_Union[UnknownAccess, _Mapping]] = ...) -> None: ...

class Revision(_message.Message):
    __slots__ = ("hash", "origin_url", "kind", "captured_at", "has_text_layer")
    HASH_FIELD_NUMBER: _ClassVar[int]
    ORIGIN_URL_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    CAPTURED_AT_FIELD_NUMBER: _ClassVar[int]
    HAS_TEXT_LAYER_FIELD_NUMBER: _ClassVar[int]
    hash: str
    origin_url: str
    kind: SourceKind
    captured_at: _timestamp_pb2.Timestamp
    has_text_layer: bool
    def __init__(self, hash: _Optional[str] = ..., origin_url: _Optional[str] = ..., kind: _Optional[_Union[SourceKind, str]] = ..., captured_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., has_text_layer: _Optional[bool] = ...) -> None: ...

class Source(_message.Message):
    __slots__ = ("handle", "media_type", "licence", "licence_basis", "access", "revisions")
    HANDLE_FIELD_NUMBER: _ClassVar[int]
    MEDIA_TYPE_FIELD_NUMBER: _ClassVar[int]
    LICENCE_FIELD_NUMBER: _ClassVar[int]
    LICENCE_BASIS_FIELD_NUMBER: _ClassVar[int]
    ACCESS_FIELD_NUMBER: _ClassVar[int]
    REVISIONS_FIELD_NUMBER: _ClassVar[int]
    handle: str
    media_type: SourceFormat
    licence: str
    licence_basis: LicenceBasis
    access: Access
    revisions: _containers.RepeatedCompositeFieldContainer[Revision]
    def __init__(self, handle: _Optional[str] = ..., media_type: _Optional[_Union[SourceFormat, str]] = ..., licence: _Optional[str] = ..., licence_basis: _Optional[_Union[LicenceBasis, str]] = ..., access: _Optional[_Union[Access, _Mapping]] = ..., revisions: _Optional[_Iterable[_Union[Revision, _Mapping]]] = ...) -> None: ...

class Rendering(_message.Message):
    __slots__ = ("from_source", "from_revision", "converter", "converter_version", "model", "created_at")
    FROM_SOURCE_FIELD_NUMBER: _ClassVar[int]
    FROM_REVISION_FIELD_NUMBER: _ClassVar[int]
    CONVERTER_FIELD_NUMBER: _ClassVar[int]
    CONVERTER_VERSION_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    from_source: str
    from_revision: str
    converter: Converter
    converter_version: str
    model: str
    created_at: _timestamp_pb2.Timestamp
    def __init__(self, from_source: _Optional[str] = ..., from_revision: _Optional[str] = ..., converter: _Optional[_Union[Converter, str]] = ..., converter_version: _Optional[str] = ..., model: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ExternalIds(_message.Message):
    __slots__ = ("doi", "pmid", "pmcid", "arxiv", "biorxiv")
    DOI_FIELD_NUMBER: _ClassVar[int]
    PMID_FIELD_NUMBER: _ClassVar[int]
    PMCID_FIELD_NUMBER: _ClassVar[int]
    ARXIV_FIELD_NUMBER: _ClassVar[int]
    BIORXIV_FIELD_NUMBER: _ClassVar[int]
    doi: str
    pmid: str
    pmcid: str
    arxiv: str
    biorxiv: str
    def __init__(self, doi: _Optional[str] = ..., pmid: _Optional[str] = ..., pmcid: _Optional[str] = ..., arxiv: _Optional[str] = ..., biorxiv: _Optional[str] = ...) -> None: ...

class Equivalence(_message.Message):
    __slots__ = ("edges", "canonical_doc_id")
    EDGES_FIELD_NUMBER: _ClassVar[int]
    CANONICAL_DOC_ID_FIELD_NUMBER: _ClassVar[int]
    edges: _containers.RepeatedScalarFieldContainer[str]
    canonical_doc_id: str
    def __init__(self, edges: _Optional[_Iterable[str]] = ..., canonical_doc_id: _Optional[str] = ...) -> None: ...

class Retraction(_message.Message):
    __slots__ = ("retracted", "source", "date")
    RETRACTED_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    DATE_FIELD_NUMBER: _ClassVar[int]
    retracted: bool
    source: str
    date: str
    def __init__(self, retracted: _Optional[bool] = ..., source: _Optional[str] = ..., date: _Optional[str] = ...) -> None: ...

class AssociatedFile(_message.Message):
    __slots__ = ("role", "name", "source_url", "path")
    ROLE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    SOURCE_URL_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    role: AssociatedFileRole
    name: str
    source_url: str
    path: str
    def __init__(self, role: _Optional[_Union[AssociatedFileRole, str]] = ..., name: _Optional[str] = ..., source_url: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class Manifest(_message.Message):
    __slots__ = ("doc_id", "external_ids", "claim_key", "equivalence", "retraction", "sources", "renderings", "files")
    class RenderingsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: Rendering
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[Rendering, _Mapping]] = ...) -> None: ...
    DOC_ID_FIELD_NUMBER: _ClassVar[int]
    EXTERNAL_IDS_FIELD_NUMBER: _ClassVar[int]
    CLAIM_KEY_FIELD_NUMBER: _ClassVar[int]
    EQUIVALENCE_FIELD_NUMBER: _ClassVar[int]
    RETRACTION_FIELD_NUMBER: _ClassVar[int]
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    RENDERINGS_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    doc_id: str
    external_ids: ExternalIds
    claim_key: str
    equivalence: Equivalence
    retraction: Retraction
    sources: _containers.RepeatedCompositeFieldContainer[Source]
    renderings: _containers.MessageMap[str, Rendering]
    files: _containers.RepeatedCompositeFieldContainer[AssociatedFile]
    def __init__(self, doc_id: _Optional[str] = ..., external_ids: _Optional[_Union[ExternalIds, _Mapping]] = ..., claim_key: _Optional[str] = ..., equivalence: _Optional[_Union[Equivalence, _Mapping]] = ..., retraction: _Optional[_Union[Retraction, _Mapping]] = ..., sources: _Optional[_Iterable[_Union[Source, _Mapping]]] = ..., renderings: _Optional[_Mapping[str, Rendering]] = ..., files: _Optional[_Iterable[_Union[AssociatedFile, _Mapping]]] = ...) -> None: ...
