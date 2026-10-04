import datetime

from buf.validate import validate_pb2 as _validate_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from themis.evidence.models import evidence_pb2 as _evidence_pb2
from themis.rpc import gene_disease_pb2 as _gene_disease_pb2
from themis.svcv4.models import svcv4_pb2 as _svcv4_pb2
from themis.widgets.models import widget_pb2 as _widget_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Svcv4Classification(_message.Message):
    __slots__ = ("reviewed", "note", "framework", "routing", "codes", "paths", "lines", "tally", "open_values", "sensitivity", "classification", "bands")
    class Framework(_message.Message):
        __slots__ = ("name", "status", "citations_repository", "citations_revision", "usage")
        NAME_FIELD_NUMBER: _ClassVar[int]
        STATUS_FIELD_NUMBER: _ClassVar[int]
        CITATIONS_REPOSITORY_FIELD_NUMBER: _ClassVar[int]
        CITATIONS_REVISION_FIELD_NUMBER: _ClassVar[int]
        USAGE_FIELD_NUMBER: _ClassVar[int]
        name: str
        status: str
        citations_repository: str
        citations_revision: str
        usage: str
        def __init__(self, name: _Optional[str] = ..., status: _Optional[str] = ..., citations_repository: _Optional[str] = ..., citations_revision: _Optional[str] = ..., usage: _Optional[str] = ...) -> None: ...
    class Variant(_message.Message):
        __slots__ = ("transcript_hgvs", "protein_hgvs", "genomic_hgvs", "gene_symbol", "hgnc_id", "caid")
        TRANSCRIPT_HGVS_FIELD_NUMBER: _ClassVar[int]
        PROTEIN_HGVS_FIELD_NUMBER: _ClassVar[int]
        GENOMIC_HGVS_FIELD_NUMBER: _ClassVar[int]
        GENE_SYMBOL_FIELD_NUMBER: _ClassVar[int]
        HGNC_ID_FIELD_NUMBER: _ClassVar[int]
        CAID_FIELD_NUMBER: _ClassVar[int]
        transcript_hgvs: str
        protein_hgvs: str
        genomic_hgvs: str
        gene_symbol: str
        hgnc_id: str
        caid: str
        def __init__(self, transcript_hgvs: _Optional[str] = ..., protein_hgvs: _Optional[str] = ..., genomic_hgvs: _Optional[str] = ..., gene_symbol: _Optional[str] = ..., hgnc_id: _Optional[str] = ..., caid: _Optional[str] = ...) -> None: ...
    class Entity(_message.Message):
        __slots__ = ("disease", "mondo_id", "inheritance", "mechanism", "validity_source", "validity_classification", "gate_level")
        DISEASE_FIELD_NUMBER: _ClassVar[int]
        MONDO_ID_FIELD_NUMBER: _ClassVar[int]
        INHERITANCE_FIELD_NUMBER: _ClassVar[int]
        MECHANISM_FIELD_NUMBER: _ClassVar[int]
        VALIDITY_SOURCE_FIELD_NUMBER: _ClassVar[int]
        VALIDITY_CLASSIFICATION_FIELD_NUMBER: _ClassVar[int]
        GATE_LEVEL_FIELD_NUMBER: _ClassVar[int]
        disease: str
        mondo_id: str
        inheritance: _evidence_pb2.Inheritance
        mechanism: str
        validity_source: str
        validity_classification: str
        gate_level: _gene_disease_pb2.GateLevel
        def __init__(self, disease: _Optional[str] = ..., mondo_id: _Optional[str] = ..., inheritance: _Optional[_Union[_evidence_pb2.Inheritance, str]] = ..., mechanism: _Optional[str] = ..., validity_source: _Optional[str] = ..., validity_classification: _Optional[str] = ..., gate_level: _Optional[_Union[_gene_disease_pb2.GateLevel, str]] = ...) -> None: ...
    class Route(_message.Message):
        __slots__ = ("family", "label")
        FAMILY_FIELD_NUMBER: _ClassVar[int]
        LABEL_FIELD_NUMBER: _ClassVar[int]
        family: str
        label: str
        def __init__(self, family: _Optional[str] = ..., label: _Optional[str] = ...) -> None: ...
    class Routing(_message.Message):
        __slots__ = ("reviewed", "note", "variant", "consequence", "entity", "routes", "rationale")
        REVIEWED_FIELD_NUMBER: _ClassVar[int]
        NOTE_FIELD_NUMBER: _ClassVar[int]
        VARIANT_FIELD_NUMBER: _ClassVar[int]
        CONSEQUENCE_FIELD_NUMBER: _ClassVar[int]
        ENTITY_FIELD_NUMBER: _ClassVar[int]
        ROUTES_FIELD_NUMBER: _ClassVar[int]
        RATIONALE_FIELD_NUMBER: _ClassVar[int]
        reviewed: bool
        note: str
        variant: Svcv4Classification.Variant
        consequence: _evidence_pb2.Consequence
        entity: Svcv4Classification.Entity
        routes: _containers.RepeatedCompositeFieldContainer[Svcv4Classification.Route]
        rationale: str
        def __init__(self, reviewed: _Optional[bool] = ..., note: _Optional[str] = ..., variant: _Optional[_Union[Svcv4Classification.Variant, _Mapping]] = ..., consequence: _Optional[_Union[_evidence_pb2.Consequence, str]] = ..., entity: _Optional[_Union[Svcv4Classification.Entity, _Mapping]] = ..., routes: _Optional[_Iterable[_Union[Svcv4Classification.Route, _Mapping]]] = ..., rationale: _Optional[str] = ...) -> None: ...
    class Release(_message.Message):
        __slots__ = ("source", "dataset_version")
        SOURCE_FIELD_NUMBER: _ClassVar[int]
        DATASET_VERSION_FIELD_NUMBER: _ClassVar[int]
        source: str
        dataset_version: str
        def __init__(self, source: _Optional[str] = ..., dataset_version: _Optional[str] = ...) -> None: ...
    class Retrieval(_message.Message):
        __slots__ = ("source", "dataset_versions", "retrieved_at")
        SOURCE_FIELD_NUMBER: _ClassVar[int]
        DATASET_VERSIONS_FIELD_NUMBER: _ClassVar[int]
        RETRIEVED_AT_FIELD_NUMBER: _ClassVar[int]
        source: str
        dataset_versions: _containers.RepeatedScalarFieldContainer[str]
        retrieved_at: _timestamp_pb2.Timestamp
        def __init__(self, source: _Optional[str] = ..., dataset_versions: _Optional[_Iterable[str]] = ..., retrieved_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...
    class Citation(_message.Message):
        __slots__ = ("doc_id", "quote")
        DOC_ID_FIELD_NUMBER: _ClassVar[int]
        QUOTE_FIELD_NUMBER: _ClassVar[int]
        doc_id: str
        quote: str
        def __init__(self, doc_id: _Optional[str] = ..., quote: _Optional[str] = ...) -> None: ...
    class WebResult(_message.Message):
        __slots__ = ("url", "title")
        URL_FIELD_NUMBER: _ClassVar[int]
        TITLE_FIELD_NUMBER: _ClassVar[int]
        url: str
        title: str
        def __init__(self, url: _Optional[str] = ..., title: _Optional[str] = ...) -> None: ...
    class CaseText(_message.Message):
        __slots__ = ("quote",)
        QUOTE_FIELD_NUMBER: _ClassVar[int]
        quote: str
        def __init__(self, quote: _Optional[str] = ...) -> None: ...
    class Evidence(_message.Message):
        __slots__ = ("statement", "retrieval", "citation", "case_text", "web_result")
        STATEMENT_FIELD_NUMBER: _ClassVar[int]
        RETRIEVAL_FIELD_NUMBER: _ClassVar[int]
        CITATION_FIELD_NUMBER: _ClassVar[int]
        CASE_TEXT_FIELD_NUMBER: _ClassVar[int]
        WEB_RESULT_FIELD_NUMBER: _ClassVar[int]
        statement: str
        retrieval: Svcv4Classification.Retrieval
        citation: Svcv4Classification.Citation
        case_text: Svcv4Classification.CaseText
        web_result: Svcv4Classification.WebResult
        def __init__(self, statement: _Optional[str] = ..., retrieval: _Optional[_Union[Svcv4Classification.Retrieval, _Mapping]] = ..., citation: _Optional[_Union[Svcv4Classification.Citation, _Mapping]] = ..., case_text: _Optional[_Union[Svcv4Classification.CaseText, _Mapping]] = ..., web_result: _Optional[_Union[Svcv4Classification.WebResult, _Mapping]] = ...) -> None: ...
    class Cell(_message.Message):
        __slots__ = ("cell_id", "description", "count", "points_each")
        CELL_ID_FIELD_NUMBER: _ClassVar[int]
        DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
        COUNT_FIELD_NUMBER: _ClassVar[int]
        POINTS_EACH_FIELD_NUMBER: _ClassVar[int]
        cell_id: str
        description: str
        count: int
        points_each: str
        def __init__(self, cell_id: _Optional[str] = ..., description: _Optional[str] = ..., count: _Optional[int] = ..., points_each: _Optional[str] = ...) -> None: ...
    class NearestAlternative(_message.Message):
        __slots__ = ("cell_id", "description", "reason")
        CELL_ID_FIELD_NUMBER: _ClassVar[int]
        DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
        REASON_FIELD_NUMBER: _ClassVar[int]
        cell_id: str
        description: str
        reason: str
        def __init__(self, cell_id: _Optional[str] = ..., description: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...
    class Code(_message.Message):
        __slots__ = ("reviewed", "note", "code", "family", "title", "status", "status_reason", "points", "raw_points", "adjustment", "basis", "path_family", "decision", "cells", "releases", "evidence", "rationale", "nearest_alternative", "confidence", "confidence_note")
        REVIEWED_FIELD_NUMBER: _ClassVar[int]
        NOTE_FIELD_NUMBER: _ClassVar[int]
        CODE_FIELD_NUMBER: _ClassVar[int]
        FAMILY_FIELD_NUMBER: _ClassVar[int]
        TITLE_FIELD_NUMBER: _ClassVar[int]
        STATUS_FIELD_NUMBER: _ClassVar[int]
        STATUS_REASON_FIELD_NUMBER: _ClassVar[int]
        POINTS_FIELD_NUMBER: _ClassVar[int]
        RAW_POINTS_FIELD_NUMBER: _ClassVar[int]
        ADJUSTMENT_FIELD_NUMBER: _ClassVar[int]
        BASIS_FIELD_NUMBER: _ClassVar[int]
        PATH_FAMILY_FIELD_NUMBER: _ClassVar[int]
        DECISION_FIELD_NUMBER: _ClassVar[int]
        CELLS_FIELD_NUMBER: _ClassVar[int]
        RELEASES_FIELD_NUMBER: _ClassVar[int]
        EVIDENCE_FIELD_NUMBER: _ClassVar[int]
        RATIONALE_FIELD_NUMBER: _ClassVar[int]
        NEAREST_ALTERNATIVE_FIELD_NUMBER: _ClassVar[int]
        CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
        CONFIDENCE_NOTE_FIELD_NUMBER: _ClassVar[int]
        reviewed: bool
        note: str
        code: str
        family: str
        title: str
        status: _svcv4_pb2.AssessmentStatus
        status_reason: str
        points: str
        raw_points: str
        adjustment: str
        basis: str
        path_family: str
        decision: str
        cells: _containers.RepeatedCompositeFieldContainer[Svcv4Classification.Cell]
        releases: _containers.RepeatedCompositeFieldContainer[Svcv4Classification.Release]
        evidence: _containers.RepeatedCompositeFieldContainer[Svcv4Classification.Evidence]
        rationale: str
        nearest_alternative: Svcv4Classification.NearestAlternative
        confidence: _svcv4_pb2.Confidence
        confidence_note: str
        def __init__(self, reviewed: _Optional[bool] = ..., note: _Optional[str] = ..., code: _Optional[str] = ..., family: _Optional[str] = ..., title: _Optional[str] = ..., status: _Optional[_Union[_svcv4_pb2.AssessmentStatus, str]] = ..., status_reason: _Optional[str] = ..., points: _Optional[str] = ..., raw_points: _Optional[str] = ..., adjustment: _Optional[str] = ..., basis: _Optional[str] = ..., path_family: _Optional[str] = ..., decision: _Optional[str] = ..., cells: _Optional[_Iterable[_Union[Svcv4Classification.Cell, _Mapping]]] = ..., releases: _Optional[_Iterable[_Union[Svcv4Classification.Release, _Mapping]]] = ..., evidence: _Optional[_Iterable[_Union[Svcv4Classification.Evidence, _Mapping]]] = ..., rationale: _Optional[str] = ..., nearest_alternative: _Optional[_Union[Svcv4Classification.NearestAlternative, _Mapping]] = ..., confidence: _Optional[_Union[_svcv4_pb2.Confidence, str]] = ..., confidence_note: _Optional[str] = ...) -> None: ...
    class Path(_message.Message):
        __slots__ = ("family", "selected", "total", "multiplier", "mechanism", "exon_relevance", "releases")
        FAMILY_FIELD_NUMBER: _ClassVar[int]
        SELECTED_FIELD_NUMBER: _ClassVar[int]
        TOTAL_FIELD_NUMBER: _ClassVar[int]
        MULTIPLIER_FIELD_NUMBER: _ClassVar[int]
        MECHANISM_FIELD_NUMBER: _ClassVar[int]
        EXON_RELEVANCE_FIELD_NUMBER: _ClassVar[int]
        RELEASES_FIELD_NUMBER: _ClassVar[int]
        family: str
        selected: bool
        total: str
        multiplier: str
        mechanism: str
        exon_relevance: str
        releases: _containers.RepeatedCompositeFieldContainer[Svcv4Classification.Release]
        def __init__(self, family: _Optional[str] = ..., selected: _Optional[bool] = ..., total: _Optional[str] = ..., multiplier: _Optional[str] = ..., mechanism: _Optional[str] = ..., exon_relevance: _Optional[str] = ..., releases: _Optional[_Iterable[_Union[Svcv4Classification.Release, _Mapping]]] = ...) -> None: ...
    class Line(_message.Message):
        __slots__ = ("kind", "label", "points", "raw_points", "note", "path_family")
        class Kind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
            __slots__ = ()
            KIND_UNSPECIFIED: _ClassVar[Svcv4Classification.Line.Kind]
            KIND_CAP: _ClassVar[Svcv4Classification.Line.Kind]
            KIND_AWARD: _ClassVar[Svcv4Classification.Line.Kind]
        KIND_UNSPECIFIED: Svcv4Classification.Line.Kind
        KIND_CAP: Svcv4Classification.Line.Kind
        KIND_AWARD: Svcv4Classification.Line.Kind
        KIND_FIELD_NUMBER: _ClassVar[int]
        LABEL_FIELD_NUMBER: _ClassVar[int]
        POINTS_FIELD_NUMBER: _ClassVar[int]
        RAW_POINTS_FIELD_NUMBER: _ClassVar[int]
        NOTE_FIELD_NUMBER: _ClassVar[int]
        PATH_FAMILY_FIELD_NUMBER: _ClassVar[int]
        kind: Svcv4Classification.Line.Kind
        label: str
        points: str
        raw_points: str
        note: str
        path_family: str
        def __init__(self, kind: _Optional[_Union[Svcv4Classification.Line.Kind, str]] = ..., label: _Optional[str] = ..., points: _Optional[str] = ..., raw_points: _Optional[str] = ..., note: _Optional[str] = ..., path_family: _Optional[str] = ...) -> None: ...
    class Tally(_message.Message):
        __slots__ = ("total", "band", "vus_subband", "final_class", "gate_capped")
        TOTAL_FIELD_NUMBER: _ClassVar[int]
        BAND_FIELD_NUMBER: _ClassVar[int]
        VUS_SUBBAND_FIELD_NUMBER: _ClassVar[int]
        FINAL_CLASS_FIELD_NUMBER: _ClassVar[int]
        GATE_CAPPED_FIELD_NUMBER: _ClassVar[int]
        total: str
        band: _svcv4_pb2.Classification
        vus_subband: str
        final_class: _svcv4_pb2.Classification
        gate_capped: bool
        def __init__(self, total: _Optional[str] = ..., band: _Optional[_Union[_svcv4_pb2.Classification, str]] = ..., vus_subband: _Optional[str] = ..., final_class: _Optional[_Union[_svcv4_pb2.Classification, str]] = ..., gate_capped: _Optional[bool] = ...) -> None: ...
    class Alternative(_message.Message):
        __slots__ = ("assumption", "codes", "tally")
        ASSUMPTION_FIELD_NUMBER: _ClassVar[int]
        CODES_FIELD_NUMBER: _ClassVar[int]
        TALLY_FIELD_NUMBER: _ClassVar[int]
        assumption: str
        codes: _containers.RepeatedScalarFieldContainer[str]
        tally: Svcv4Classification.Tally
        def __init__(self, assumption: _Optional[str] = ..., codes: _Optional[_Iterable[str]] = ..., tally: _Optional[_Union[Svcv4Classification.Tally, _Mapping]] = ...) -> None: ...
    class Band(_message.Message):
        __slots__ = ("name", "classification", "lower", "lower_inclusive", "upper", "upper_inclusive")
        NAME_FIELD_NUMBER: _ClassVar[int]
        CLASSIFICATION_FIELD_NUMBER: _ClassVar[int]
        LOWER_FIELD_NUMBER: _ClassVar[int]
        LOWER_INCLUSIVE_FIELD_NUMBER: _ClassVar[int]
        UPPER_FIELD_NUMBER: _ClassVar[int]
        UPPER_INCLUSIVE_FIELD_NUMBER: _ClassVar[int]
        name: str
        classification: _svcv4_pb2.Classification
        lower: str
        lower_inclusive: bool
        upper: str
        upper_inclusive: bool
        def __init__(self, name: _Optional[str] = ..., classification: _Optional[_Union[_svcv4_pb2.Classification, str]] = ..., lower: _Optional[str] = ..., lower_inclusive: _Optional[bool] = ..., upper: _Optional[str] = ..., upper_inclusive: _Optional[bool] = ...) -> None: ...
    REVIEWED_FIELD_NUMBER: _ClassVar[int]
    NOTE_FIELD_NUMBER: _ClassVar[int]
    FRAMEWORK_FIELD_NUMBER: _ClassVar[int]
    ROUTING_FIELD_NUMBER: _ClassVar[int]
    CODES_FIELD_NUMBER: _ClassVar[int]
    PATHS_FIELD_NUMBER: _ClassVar[int]
    LINES_FIELD_NUMBER: _ClassVar[int]
    TALLY_FIELD_NUMBER: _ClassVar[int]
    OPEN_VALUES_FIELD_NUMBER: _ClassVar[int]
    SENSITIVITY_FIELD_NUMBER: _ClassVar[int]
    CLASSIFICATION_FIELD_NUMBER: _ClassVar[int]
    BANDS_FIELD_NUMBER: _ClassVar[int]
    reviewed: bool
    note: str
    framework: Svcv4Classification.Framework
    routing: Svcv4Classification.Routing
    codes: _containers.RepeatedCompositeFieldContainer[Svcv4Classification.Code]
    paths: _containers.RepeatedCompositeFieldContainer[Svcv4Classification.Path]
    lines: _containers.RepeatedCompositeFieldContainer[Svcv4Classification.Line]
    tally: Svcv4Classification.Tally
    open_values: _containers.RepeatedCompositeFieldContainer[Svcv4Classification.Alternative]
    sensitivity: _containers.RepeatedCompositeFieldContainer[Svcv4Classification.Alternative]
    classification: _svcv4_pb2.Classification
    bands: _containers.RepeatedCompositeFieldContainer[Svcv4Classification.Band]
    def __init__(self, reviewed: _Optional[bool] = ..., note: _Optional[str] = ..., framework: _Optional[_Union[Svcv4Classification.Framework, _Mapping]] = ..., routing: _Optional[_Union[Svcv4Classification.Routing, _Mapping]] = ..., codes: _Optional[_Iterable[_Union[Svcv4Classification.Code, _Mapping]]] = ..., paths: _Optional[_Iterable[_Union[Svcv4Classification.Path, _Mapping]]] = ..., lines: _Optional[_Iterable[_Union[Svcv4Classification.Line, _Mapping]]] = ..., tally: _Optional[_Union[Svcv4Classification.Tally, _Mapping]] = ..., open_values: _Optional[_Iterable[_Union[Svcv4Classification.Alternative, _Mapping]]] = ..., sensitivity: _Optional[_Iterable[_Union[Svcv4Classification.Alternative, _Mapping]]] = ..., classification: _Optional[_Union[_svcv4_pb2.Classification, str]] = ..., bands: _Optional[_Iterable[_Union[Svcv4Classification.Band, _Mapping]]] = ...) -> None: ...
