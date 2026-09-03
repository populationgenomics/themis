from buf.validate import validate_pb2 as _validate_pb2
from themis.evidence.models import evidence_pb2 as _evidence_pb2
from themis.svcv4.models import svcv4_pb2 as _svcv4_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AssessmentStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ASSESSMENT_STATUS_UNSPECIFIED: _ClassVar[AssessmentStatus]
    ASSESSMENT_STATUS_SCORED: _ClassVar[AssessmentStatus]
    ASSESSMENT_STATUS_NOT_APPLICABLE: _ClassVar[AssessmentStatus]
    ASSESSMENT_STATUS_NO_DATA: _ClassVar[AssessmentStatus]

class Confidence(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONFIDENCE_UNSPECIFIED: _ClassVar[Confidence]
    CONFIDENCE_SETTLED: _ClassVar[Confidence]
    CONFIDENCE_LEANING: _ClassVar[Confidence]
    CONFIDENCE_OPEN: _ClassVar[Confidence]
ASSESSMENT_STATUS_UNSPECIFIED: AssessmentStatus
ASSESSMENT_STATUS_SCORED: AssessmentStatus
ASSESSMENT_STATUS_NOT_APPLICABLE: AssessmentStatus
ASSESSMENT_STATUS_NO_DATA: AssessmentStatus
CONFIDENCE_UNSPECIFIED: Confidence
CONFIDENCE_SETTLED: Confidence
CONFIDENCE_LEANING: Confidence
CONFIDENCE_OPEN: Confidence

class FieldValue(_message.Message):
    __slots__ = ("field_id", "cell_id", "label", "value")
    FIELD_ID_FIELD_NUMBER: _ClassVar[int]
    CELL_ID_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    field_id: str
    cell_id: str
    label: str
    value: str
    def __init__(self, field_id: _Optional[str] = ..., cell_id: _Optional[str] = ..., label: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class WorkflowAssessment(_message.Message):
    __slots__ = ("status", "fields", "status_reason", "evidence", "rationale", "nearest_alternative", "nearest_alternative_reason", "confidence", "confidence_note")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    FIELDS_FIELD_NUMBER: _ClassVar[int]
    STATUS_REASON_FIELD_NUMBER: _ClassVar[int]
    EVIDENCE_FIELD_NUMBER: _ClassVar[int]
    RATIONALE_FIELD_NUMBER: _ClassVar[int]
    NEAREST_ALTERNATIVE_FIELD_NUMBER: _ClassVar[int]
    NEAREST_ALTERNATIVE_REASON_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_NOTE_FIELD_NUMBER: _ClassVar[int]
    status: AssessmentStatus
    fields: _containers.RepeatedCompositeFieldContainer[FieldValue]
    status_reason: str
    evidence: str
    rationale: str
    nearest_alternative: FieldValue
    nearest_alternative_reason: str
    confidence: Confidence
    confidence_note: str
    def __init__(self, status: _Optional[_Union[AssessmentStatus, str]] = ..., fields: _Optional[_Iterable[_Union[FieldValue, _Mapping]]] = ..., status_reason: _Optional[str] = ..., evidence: _Optional[str] = ..., rationale: _Optional[str] = ..., nearest_alternative: _Optional[_Union[FieldValue, _Mapping]] = ..., nearest_alternative_reason: _Optional[str] = ..., confidence: _Optional[_Union[Confidence, str]] = ..., confidence_note: _Optional[str] = ...) -> None: ...

class CaseAssessment(_message.Message):
    __slots__ = ("proband_narrative", "testing_performed", "co_observed_variant", "segregation", "assays", "other")
    PROBAND_NARRATIVE_FIELD_NUMBER: _ClassVar[int]
    TESTING_PERFORMED_FIELD_NUMBER: _ClassVar[int]
    CO_OBSERVED_VARIANT_FIELD_NUMBER: _ClassVar[int]
    SEGREGATION_FIELD_NUMBER: _ClassVar[int]
    ASSAYS_FIELD_NUMBER: _ClassVar[int]
    OTHER_FIELD_NUMBER: _ClassVar[int]
    proband_narrative: str
    testing_performed: str
    co_observed_variant: str
    segregation: str
    assays: str
    other: str
    def __init__(self, proband_narrative: _Optional[str] = ..., testing_performed: _Optional[str] = ..., co_observed_variant: _Optional[str] = ..., segregation: _Optional[str] = ..., assays: _Optional[str] = ..., other: _Optional[str] = ...) -> None: ...

class RoutingAssessment(_message.Message):
    __slots__ = ("inheritance", "consequence_class", "entity", "mondo_id", "rationale")
    INHERITANCE_FIELD_NUMBER: _ClassVar[int]
    CONSEQUENCE_CLASS_FIELD_NUMBER: _ClassVar[int]
    ENTITY_FIELD_NUMBER: _ClassVar[int]
    MONDO_ID_FIELD_NUMBER: _ClassVar[int]
    RATIONALE_FIELD_NUMBER: _ClassVar[int]
    inheritance: _evidence_pb2.Inheritance
    consequence_class: _evidence_pb2.Consequence
    entity: str
    mondo_id: str
    rationale: str
    def __init__(self, inheritance: _Optional[_Union[_evidence_pb2.Inheritance, str]] = ..., consequence_class: _Optional[_Union[_evidence_pb2.Consequence, str]] = ..., entity: _Optional[str] = ..., mondo_id: _Optional[str] = ..., rationale: _Optional[str] = ...) -> None: ...

class VerdictAssessment(_message.Message):
    __slots__ = ("classification", "rationale", "class_determinative")
    CLASSIFICATION_FIELD_NUMBER: _ClassVar[int]
    RATIONALE_FIELD_NUMBER: _ClassVar[int]
    CLASS_DETERMINATIVE_FIELD_NUMBER: _ClassVar[int]
    classification: _svcv4_pb2.Classification
    rationale: str
    class_determinative: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, classification: _Optional[_Union[_svcv4_pb2.Classification, str]] = ..., rationale: _Optional[str] = ..., class_determinative: _Optional[_Iterable[str]] = ...) -> None: ...

class Assessment(_message.Message):
    __slots__ = ("workflow", "routing", "verdict", "case_context")
    WORKFLOW_FIELD_NUMBER: _ClassVar[int]
    ROUTING_FIELD_NUMBER: _ClassVar[int]
    VERDICT_FIELD_NUMBER: _ClassVar[int]
    CASE_CONTEXT_FIELD_NUMBER: _ClassVar[int]
    workflow: WorkflowAssessment
    routing: RoutingAssessment
    verdict: VerdictAssessment
    case_context: CaseAssessment
    def __init__(self, workflow: _Optional[_Union[WorkflowAssessment, _Mapping]] = ..., routing: _Optional[_Union[RoutingAssessment, _Mapping]] = ..., verdict: _Optional[_Union[VerdictAssessment, _Mapping]] = ..., case_context: _Optional[_Union[CaseAssessment, _Mapping]] = ...) -> None: ...
