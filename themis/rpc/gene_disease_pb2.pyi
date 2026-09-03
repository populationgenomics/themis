from google.protobuf import struct_pb2 as _struct_pb2
from themis.evidence.models import evidence_pb2 as _evidence_pb2
from themis.rpc import sandbox_options_pb2 as _sandbox_options_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GateLevel(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    GATE_LEVEL_UNSPECIFIED: _ClassVar[GateLevel]
    GATE_LEVEL_DEFINITIVE: _ClassVar[GateLevel]
    GATE_LEVEL_STRONG: _ClassVar[GateLevel]
    GATE_LEVEL_MODERATE: _ClassVar[GateLevel]
    GATE_LEVEL_LIMITED: _ClassVar[GateLevel]
    GATE_LEVEL_LESS_THAN_LIMITED: _ClassVar[GateLevel]
    GATE_LEVEL_DISPUTED_OR_REFUTED: _ClassVar[GateLevel]

class TermRelation(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TERM_RELATION_UNSPECIFIED: _ClassVar[TermRelation]
    TERM_RELATION_SAME: _ClassVar[TermRelation]
    TERM_RELATION_DESCENDANT: _ClassVar[TermRelation]

class GeneCoverage(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    GENE_COVERAGE_UNSPECIFIED: _ClassVar[GeneCoverage]
    GENE_COVERAGE_CURATED: _ClassVar[GeneCoverage]
    GENE_COVERAGE_NO_VALIDITY_ASSERTION: _ClassVar[GeneCoverage]
    GENE_COVERAGE_ABSENT: _ClassVar[GeneCoverage]
GATE_LEVEL_UNSPECIFIED: GateLevel
GATE_LEVEL_DEFINITIVE: GateLevel
GATE_LEVEL_STRONG: GateLevel
GATE_LEVEL_MODERATE: GateLevel
GATE_LEVEL_LIMITED: GateLevel
GATE_LEVEL_LESS_THAN_LIMITED: GateLevel
GATE_LEVEL_DISPUTED_OR_REFUTED: GateLevel
TERM_RELATION_UNSPECIFIED: TermRelation
TERM_RELATION_SAME: TermRelation
TERM_RELATION_DESCENDANT: TermRelation
GENE_COVERAGE_UNSPECIFIED: GeneCoverage
GENE_COVERAGE_CURATED: GeneCoverage
GENE_COVERAGE_NO_VALIDITY_ASSERTION: GeneCoverage
GENE_COVERAGE_ABSENT: GeneCoverage

class DescribeGeneRequest(_message.Message):
    __slots__ = ("hgnc_id", "mondo_id", "inheritance")
    HGNC_ID_FIELD_NUMBER: _ClassVar[int]
    MONDO_ID_FIELD_NUMBER: _ClassVar[int]
    INHERITANCE_FIELD_NUMBER: _ClassVar[int]
    hgnc_id: str
    mondo_id: str
    inheritance: _evidence_pb2.Inheritance
    def __init__(self, hgnc_id: _Optional[str] = ..., mondo_id: _Optional[str] = ..., inheritance: _Optional[_Union[_evidence_pb2.Inheritance, str]] = ...) -> None: ...

class DescribeGeneResponse(_message.Message):
    __slots__ = ("entities", "resolution", "gene_scoped", "coverage", "raw", "provenance")
    ENTITIES_FIELD_NUMBER: _ClassVar[int]
    RESOLUTION_FIELD_NUMBER: _ClassVar[int]
    GENE_SCOPED_FIELD_NUMBER: _ClassVar[int]
    COVERAGE_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    entities: _containers.RepeatedCompositeFieldContainer[CuratedEntity]
    resolution: EntityResolution
    gene_scoped: GeneScopedSignals
    coverage: GeneCoverage
    raw: _struct_pb2.Struct
    provenance: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.Provenance]
    def __init__(self, entities: _Optional[_Iterable[_Union[CuratedEntity, _Mapping]]] = ..., resolution: _Optional[_Union[EntityResolution, _Mapping]] = ..., gene_scoped: _Optional[_Union[GeneScopedSignals, _Mapping]] = ..., coverage: _Optional[_Union[GeneCoverage, str]] = ..., raw: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., provenance: _Optional[_Iterable[_Union[_evidence_pb2.Provenance, _Mapping]]] = ...) -> None: ...

class CuratedEntity(_message.Message):
    __slots__ = ("source", "disease_label", "mondo_id", "inheritance", "inheritance_term", "validity_classification", "gate_level", "submissions", "mechanism_statements")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    DISEASE_LABEL_FIELD_NUMBER: _ClassVar[int]
    MONDO_ID_FIELD_NUMBER: _ClassVar[int]
    INHERITANCE_FIELD_NUMBER: _ClassVar[int]
    INHERITANCE_TERM_FIELD_NUMBER: _ClassVar[int]
    VALIDITY_CLASSIFICATION_FIELD_NUMBER: _ClassVar[int]
    GATE_LEVEL_FIELD_NUMBER: _ClassVar[int]
    SUBMISSIONS_FIELD_NUMBER: _ClassVar[int]
    MECHANISM_STATEMENTS_FIELD_NUMBER: _ClassVar[int]
    source: str
    disease_label: str
    mondo_id: str
    inheritance: _evidence_pb2.Inheritance
    inheritance_term: str
    validity_classification: str
    gate_level: GateLevel
    submissions: _containers.RepeatedCompositeFieldContainer[GenccSubmission]
    mechanism_statements: _containers.RepeatedCompositeFieldContainer[MechanismStatement]
    def __init__(self, source: _Optional[str] = ..., disease_label: _Optional[str] = ..., mondo_id: _Optional[str] = ..., inheritance: _Optional[_Union[_evidence_pb2.Inheritance, str]] = ..., inheritance_term: _Optional[str] = ..., validity_classification: _Optional[str] = ..., gate_level: _Optional[_Union[GateLevel, str]] = ..., submissions: _Optional[_Iterable[_Union[GenccSubmission, _Mapping]]] = ..., mechanism_statements: _Optional[_Iterable[_Union[MechanismStatement, _Mapping]]] = ...) -> None: ...

class GenccSubmission(_message.Message):
    __slots__ = ("submitter", "validity_classification", "mechanism_note")
    SUBMITTER_FIELD_NUMBER: _ClassVar[int]
    VALIDITY_CLASSIFICATION_FIELD_NUMBER: _ClassVar[int]
    MECHANISM_NOTE_FIELD_NUMBER: _ClassVar[int]
    submitter: str
    validity_classification: str
    mechanism_note: str
    def __init__(self, submitter: _Optional[str] = ..., validity_classification: _Optional[str] = ..., mechanism_note: _Optional[str] = ...) -> None: ...

class EntityResolution(_message.Message):
    __slots__ = ("requested_mondo_id", "requested_inheritance", "mondo_id", "inheritance", "relation", "entities")
    REQUESTED_MONDO_ID_FIELD_NUMBER: _ClassVar[int]
    REQUESTED_INHERITANCE_FIELD_NUMBER: _ClassVar[int]
    MONDO_ID_FIELD_NUMBER: _ClassVar[int]
    INHERITANCE_FIELD_NUMBER: _ClassVar[int]
    RELATION_FIELD_NUMBER: _ClassVar[int]
    ENTITIES_FIELD_NUMBER: _ClassVar[int]
    requested_mondo_id: str
    requested_inheritance: _evidence_pb2.Inheritance
    mondo_id: str
    inheritance: _evidence_pb2.Inheritance
    relation: TermRelation
    entities: _containers.RepeatedCompositeFieldContainer[CuratedEntity]
    def __init__(self, requested_mondo_id: _Optional[str] = ..., requested_inheritance: _Optional[_Union[_evidence_pb2.Inheritance, str]] = ..., mondo_id: _Optional[str] = ..., inheritance: _Optional[_Union[_evidence_pb2.Inheritance, str]] = ..., relation: _Optional[_Union[TermRelation, str]] = ..., entities: _Optional[_Iterable[_Union[CuratedEntity, _Mapping]]] = ...) -> None: ...

class GeneScopedSignals(_message.Message):
    __slots__ = ("haploinsufficiency_score", "mode_of_pathogenicity", "mode_of_inheritance", "mechanism_statements", "sources_holding_the_gene")
    HAPLOINSUFFICIENCY_SCORE_FIELD_NUMBER: _ClassVar[int]
    MODE_OF_PATHOGENICITY_FIELD_NUMBER: _ClassVar[int]
    MODE_OF_INHERITANCE_FIELD_NUMBER: _ClassVar[int]
    MECHANISM_STATEMENTS_FIELD_NUMBER: _ClassVar[int]
    SOURCES_HOLDING_THE_GENE_FIELD_NUMBER: _ClassVar[int]
    haploinsufficiency_score: int
    mode_of_pathogenicity: str
    mode_of_inheritance: str
    mechanism_statements: _containers.RepeatedCompositeFieldContainer[MechanismStatement]
    sources_holding_the_gene: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, haploinsufficiency_score: _Optional[int] = ..., mode_of_pathogenicity: _Optional[str] = ..., mode_of_inheritance: _Optional[str] = ..., mechanism_statements: _Optional[_Iterable[_Union[MechanismStatement, _Mapping]]] = ..., sources_holding_the_gene: _Optional[_Iterable[str]] = ...) -> None: ...

class MechanismStatement(_message.Message):
    __slots__ = ("source", "context", "text")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    source: str
    context: str
    text: str
    def __init__(self, source: _Optional[str] = ..., context: _Optional[str] = ..., text: _Optional[str] = ...) -> None: ...
