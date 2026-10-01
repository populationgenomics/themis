from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from typing import ClassVar as _ClassVar

DESCRIPTOR: _descriptor.FileDescriptor

class Classification(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CLASSIFICATION_UNSPECIFIED: _ClassVar[Classification]
    CLASSIFICATION_PATHOGENIC: _ClassVar[Classification]
    CLASSIFICATION_LIKELY_PATHOGENIC: _ClassVar[Classification]
    CLASSIFICATION_VUS: _ClassVar[Classification]
    CLASSIFICATION_LIKELY_BENIGN: _ClassVar[Classification]
    CLASSIFICATION_BENIGN: _ClassVar[Classification]
    CLASSIFICATION_NOT_ESTABLISHED: _ClassVar[Classification]
    CLASSIFICATION_VARIANT_IN_GENE_OF_UNCERTAIN_SIGNIFICANCE: _ClassVar[Classification]
    CLASSIFICATION_DO_NOT_REPORT: _ClassVar[Classification]

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
CLASSIFICATION_UNSPECIFIED: Classification
CLASSIFICATION_PATHOGENIC: Classification
CLASSIFICATION_LIKELY_PATHOGENIC: Classification
CLASSIFICATION_VUS: Classification
CLASSIFICATION_LIKELY_BENIGN: Classification
CLASSIFICATION_BENIGN: Classification
CLASSIFICATION_NOT_ESTABLISHED: Classification
CLASSIFICATION_VARIANT_IN_GENE_OF_UNCERTAIN_SIGNIFICANCE: Classification
CLASSIFICATION_DO_NOT_REPORT: Classification
ASSESSMENT_STATUS_UNSPECIFIED: AssessmentStatus
ASSESSMENT_STATUS_SCORED: AssessmentStatus
ASSESSMENT_STATUS_NOT_APPLICABLE: AssessmentStatus
ASSESSMENT_STATUS_NO_DATA: AssessmentStatus
CONFIDENCE_UNSPECIFIED: Confidence
CONFIDENCE_SETTLED: Confidence
CONFIDENCE_LEANING: Confidence
CONFIDENCE_OPEN: Confidence
