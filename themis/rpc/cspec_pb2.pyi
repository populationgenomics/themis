import datetime

from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from themis.evidence.models import evidence_pb2 as _evidence_pb2
from themis.rpc import sandbox_options_pb2 as _sandbox_options_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SpecificationCoverage(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SPECIFICATION_COVERAGE_UNSPECIFIED: _ClassVar[SpecificationCoverage]
    SPECIFICATION_COVERAGE_SPECIFIED: _ClassVar[SpecificationCoverage]
    SPECIFICATION_COVERAGE_NO_SPECIFICATION: _ClassVar[SpecificationCoverage]
    SPECIFICATION_COVERAGE_GENE_ABSENT: _ClassVar[SpecificationCoverage]

class SpecificationStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SPECIFICATION_STATUS_UNSPECIFIED: _ClassVar[SpecificationStatus]
    SPECIFICATION_STATUS_IN_FORCE: _ClassVar[SpecificationStatus]
    SPECIFICATION_STATUS_REPLACED: _ClassVar[SpecificationStatus]
    SPECIFICATION_STATUS_NOT_YET_EFFECTIVE: _ClassVar[SpecificationStatus]
    SPECIFICATION_STATUS_UNRELEASED: _ClassVar[SpecificationStatus]
    SPECIFICATION_STATUS_RELEASED_UNDER_REVISION: _ClassVar[SpecificationStatus]

class Applicability(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    APPLICABILITY_UNSPECIFIED: _ClassVar[Applicability]
    APPLICABILITY_APPLICABLE: _ClassVar[Applicability]
    APPLICABILITY_NOT_APPLICABLE: _ClassVar[Applicability]
    APPLICABILITY_APPLICABLE_WITH_VCEP_SPECIFICATION: _ClassVar[Applicability]
    APPLICABILITY_APPLICABLE_AS_ORIGINALLY_DESCRIBED: _ClassVar[Applicability]
SPECIFICATION_COVERAGE_UNSPECIFIED: SpecificationCoverage
SPECIFICATION_COVERAGE_SPECIFIED: SpecificationCoverage
SPECIFICATION_COVERAGE_NO_SPECIFICATION: SpecificationCoverage
SPECIFICATION_COVERAGE_GENE_ABSENT: SpecificationCoverage
SPECIFICATION_STATUS_UNSPECIFIED: SpecificationStatus
SPECIFICATION_STATUS_IN_FORCE: SpecificationStatus
SPECIFICATION_STATUS_REPLACED: SpecificationStatus
SPECIFICATION_STATUS_NOT_YET_EFFECTIVE: SpecificationStatus
SPECIFICATION_STATUS_UNRELEASED: SpecificationStatus
SPECIFICATION_STATUS_RELEASED_UNDER_REVISION: SpecificationStatus
APPLICABILITY_UNSPECIFIED: Applicability
APPLICABILITY_APPLICABLE: Applicability
APPLICABILITY_NOT_APPLICABLE: Applicability
APPLICABILITY_APPLICABLE_WITH_VCEP_SPECIFICATION: Applicability
APPLICABILITY_APPLICABLE_AS_ORIGINALLY_DESCRIBED: Applicability

class ListSpecificationsRequest(_message.Message):
    __slots__ = ("gene",)
    GENE_FIELD_NUMBER: _ClassVar[int]
    gene: str
    def __init__(self, gene: _Optional[str] = ...) -> None: ...

class SpecifiedEntity(_message.Message):
    __slots__ = ("gene", "preferred_transcript", "mondo_id", "disease_label", "inheritance_terms")
    GENE_FIELD_NUMBER: _ClassVar[int]
    PREFERRED_TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    MONDO_ID_FIELD_NUMBER: _ClassVar[int]
    DISEASE_LABEL_FIELD_NUMBER: _ClassVar[int]
    INHERITANCE_TERMS_FIELD_NUMBER: _ClassVar[int]
    gene: str
    preferred_transcript: str
    mondo_id: str
    disease_label: str
    inheritance_terms: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, gene: _Optional[str] = ..., preferred_transcript: _Optional[str] = ..., mondo_id: _Optional[str] = ..., disease_label: _Optional[str] = ..., inheritance_terms: _Optional[_Iterable[str]] = ...) -> None: ...

class SpecificationCitation(_message.Message):
    __slots__ = ("document_doi", "concept_doi", "version", "approved_on", "modified", "release_notes", "registry_url", "publisher_url")
    DOCUMENT_DOI_FIELD_NUMBER: _ClassVar[int]
    CONCEPT_DOI_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    APPROVED_ON_FIELD_NUMBER: _ClassVar[int]
    MODIFIED_FIELD_NUMBER: _ClassVar[int]
    RELEASE_NOTES_FIELD_NUMBER: _ClassVar[int]
    REGISTRY_URL_FIELD_NUMBER: _ClassVar[int]
    PUBLISHER_URL_FIELD_NUMBER: _ClassVar[int]
    document_doi: str
    concept_doi: str
    version: str
    approved_on: _timestamp_pb2.Timestamp
    modified: _timestamp_pb2.Timestamp
    release_notes: str
    registry_url: str
    publisher_url: str
    def __init__(self, document_doi: _Optional[str] = ..., concept_doi: _Optional[str] = ..., version: _Optional[str] = ..., approved_on: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., modified: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., release_notes: _Optional[str] = ..., registry_url: _Optional[str] = ..., publisher_url: _Optional[str] = ...) -> None: ...

class SpecificationReference(_message.Message):
    __slots__ = ("namespace", "id", "url", "title", "authors", "journal", "year", "doi")
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    AUTHORS_FIELD_NUMBER: _ClassVar[int]
    JOURNAL_FIELD_NUMBER: _ClassVar[int]
    YEAR_FIELD_NUMBER: _ClassVar[int]
    DOI_FIELD_NUMBER: _ClassVar[int]
    namespace: str
    id: str
    url: str
    title: str
    authors: str
    journal: str
    year: str
    doi: str
    def __init__(self, namespace: _Optional[str] = ..., id: _Optional[str] = ..., url: _Optional[str] = ..., title: _Optional[str] = ..., authors: _Optional[str] = ..., journal: _Optional[str] = ..., year: _Optional[str] = ..., doi: _Optional[str] = ...) -> None: ...

class CriterionNote(_message.Message):
    __slots__ = ("heading", "lines")
    HEADING_FIELD_NUMBER: _ClassVar[int]
    LINES_FIELD_NUMBER: _ClassVar[int]
    heading: str
    lines: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, heading: _Optional[str] = ..., lines: _Optional[_Iterable[str]] = ...) -> None: ...

class StrengthSpecification(_message.Message):
    __slots__ = ("strength", "applicability", "applicability_term", "text", "instructions", "notes", "specification_types", "status", "default_points")
    STRENGTH_FIELD_NUMBER: _ClassVar[int]
    APPLICABILITY_FIELD_NUMBER: _ClassVar[int]
    APPLICABILITY_TERM_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTIONS_FIELD_NUMBER: _ClassVar[int]
    NOTES_FIELD_NUMBER: _ClassVar[int]
    SPECIFICATION_TYPES_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_POINTS_FIELD_NUMBER: _ClassVar[int]
    strength: str
    applicability: Applicability
    applicability_term: str
    text: str
    instructions: _containers.RepeatedScalarFieldContainer[str]
    notes: _containers.RepeatedCompositeFieldContainer[CriterionNote]
    specification_types: _containers.RepeatedScalarFieldContainer[str]
    status: str
    default_points: str
    def __init__(self, strength: _Optional[str] = ..., applicability: _Optional[_Union[Applicability, str]] = ..., applicability_term: _Optional[str] = ..., text: _Optional[str] = ..., instructions: _Optional[_Iterable[str]] = ..., notes: _Optional[_Iterable[_Union[CriterionNote, _Mapping]]] = ..., specification_types: _Optional[_Iterable[str]] = ..., status: _Optional[str] = ..., default_points: _Optional[str] = ...) -> None: ...

class CriterionSpecification(_message.Message):
    __slots__ = ("code", "genes", "diseases", "applicability", "applicability_term", "base_strength", "default_strength", "evidence_category", "original_acmg_summary", "instructions", "additional_comments", "references", "specification_types", "strengths")
    CODE_FIELD_NUMBER: _ClassVar[int]
    GENES_FIELD_NUMBER: _ClassVar[int]
    DISEASES_FIELD_NUMBER: _ClassVar[int]
    APPLICABILITY_FIELD_NUMBER: _ClassVar[int]
    APPLICABILITY_TERM_FIELD_NUMBER: _ClassVar[int]
    BASE_STRENGTH_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_STRENGTH_FIELD_NUMBER: _ClassVar[int]
    EVIDENCE_CATEGORY_FIELD_NUMBER: _ClassVar[int]
    ORIGINAL_ACMG_SUMMARY_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTIONS_FIELD_NUMBER: _ClassVar[int]
    ADDITIONAL_COMMENTS_FIELD_NUMBER: _ClassVar[int]
    REFERENCES_FIELD_NUMBER: _ClassVar[int]
    SPECIFICATION_TYPES_FIELD_NUMBER: _ClassVar[int]
    STRENGTHS_FIELD_NUMBER: _ClassVar[int]
    code: str
    genes: _containers.RepeatedScalarFieldContainer[str]
    diseases: _containers.RepeatedScalarFieldContainer[str]
    applicability: Applicability
    applicability_term: str
    base_strength: str
    default_strength: str
    evidence_category: str
    original_acmg_summary: str
    instructions: _containers.RepeatedScalarFieldContainer[str]
    additional_comments: str
    references: _containers.RepeatedCompositeFieldContainer[SpecificationReference]
    specification_types: _containers.RepeatedScalarFieldContainer[str]
    strengths: _containers.RepeatedCompositeFieldContainer[StrengthSpecification]
    def __init__(self, code: _Optional[str] = ..., genes: _Optional[_Iterable[str]] = ..., diseases: _Optional[_Iterable[str]] = ..., applicability: _Optional[_Union[Applicability, str]] = ..., applicability_term: _Optional[str] = ..., base_strength: _Optional[str] = ..., default_strength: _Optional[str] = ..., evidence_category: _Optional[str] = ..., original_acmg_summary: _Optional[str] = ..., instructions: _Optional[_Iterable[str]] = ..., additional_comments: _Optional[str] = ..., references: _Optional[_Iterable[_Union[SpecificationReference, _Mapping]]] = ..., specification_types: _Optional[_Iterable[str]] = ..., strengths: _Optional[_Iterable[_Union[StrengthSpecification, _Mapping]]] = ...) -> None: ...

class SpecificationAttachment(_message.Message):
    __slots__ = ("label", "description", "file_name", "media_type", "size_bytes", "registry_url")
    LABEL_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    FILE_NAME_FIELD_NUMBER: _ClassVar[int]
    MEDIA_TYPE_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    REGISTRY_URL_FIELD_NUMBER: _ClassVar[int]
    label: str
    description: str
    file_name: str
    media_type: str
    size_bytes: int
    registry_url: str
    def __init__(self, label: _Optional[str] = ..., description: _Optional[str] = ..., file_name: _Optional[str] = ..., media_type: _Optional[str] = ..., size_bytes: _Optional[int] = ..., registry_url: _Optional[str] = ...) -> None: ...

class VcepSpecification(_message.Message):
    __slots__ = ("id", "title", "short_title", "status", "state", "expert_panel", "expert_panel_abbreviation", "citation", "entities", "general_comments", "references", "criteria", "attachments")
    ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    SHORT_TITLE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    EXPERT_PANEL_FIELD_NUMBER: _ClassVar[int]
    EXPERT_PANEL_ABBREVIATION_FIELD_NUMBER: _ClassVar[int]
    CITATION_FIELD_NUMBER: _ClassVar[int]
    ENTITIES_FIELD_NUMBER: _ClassVar[int]
    GENERAL_COMMENTS_FIELD_NUMBER: _ClassVar[int]
    REFERENCES_FIELD_NUMBER: _ClassVar[int]
    CRITERIA_FIELD_NUMBER: _ClassVar[int]
    ATTACHMENTS_FIELD_NUMBER: _ClassVar[int]
    id: str
    title: str
    short_title: str
    status: SpecificationStatus
    state: str
    expert_panel: str
    expert_panel_abbreviation: str
    citation: SpecificationCitation
    entities: _containers.RepeatedCompositeFieldContainer[SpecifiedEntity]
    general_comments: str
    references: _containers.RepeatedCompositeFieldContainer[SpecificationReference]
    criteria: _containers.RepeatedCompositeFieldContainer[CriterionSpecification]
    attachments: _containers.RepeatedCompositeFieldContainer[SpecificationAttachment]
    def __init__(self, id: _Optional[str] = ..., title: _Optional[str] = ..., short_title: _Optional[str] = ..., status: _Optional[_Union[SpecificationStatus, str]] = ..., state: _Optional[str] = ..., expert_panel: _Optional[str] = ..., expert_panel_abbreviation: _Optional[str] = ..., citation: _Optional[_Union[SpecificationCitation, _Mapping]] = ..., entities: _Optional[_Iterable[_Union[SpecifiedEntity, _Mapping]]] = ..., general_comments: _Optional[str] = ..., references: _Optional[_Iterable[_Union[SpecificationReference, _Mapping]]] = ..., criteria: _Optional[_Iterable[_Union[CriterionSpecification, _Mapping]]] = ..., attachments: _Optional[_Iterable[_Union[SpecificationAttachment, _Mapping]]] = ...) -> None: ...

class ListSpecificationsResponse(_message.Message):
    __slots__ = ("specifications", "coverage", "raw", "provenance")
    SPECIFICATIONS_FIELD_NUMBER: _ClassVar[int]
    COVERAGE_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    specifications: _containers.RepeatedCompositeFieldContainer[VcepSpecification]
    coverage: SpecificationCoverage
    raw: _struct_pb2.Struct
    provenance: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.Provenance]
    def __init__(self, specifications: _Optional[_Iterable[_Union[VcepSpecification, _Mapping]]] = ..., coverage: _Optional[_Union[SpecificationCoverage, str]] = ..., raw: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., provenance: _Optional[_Iterable[_Union[_evidence_pb2.Provenance, _Mapping]]] = ...) -> None: ...
