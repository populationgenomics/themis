from clinvar_proto import clinvar_pb2 as _clinvar_pb2
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

class CodingRegion(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CODING_REGION_UNSPECIFIED: _ClassVar[CodingRegion]
    CODING_REGION_FIVE_PRIME_UTR: _ClassVar[CodingRegion]
    CODING_REGION_CDS: _ClassVar[CodingRegion]
    CODING_REGION_THREE_PRIME_UTR: _ClassVar[CodingRegion]
CODING_REGION_UNSPECIFIED: CodingRegion
CODING_REGION_FIVE_PRIME_UTR: CodingRegion
CODING_REGION_CDS: CodingRegion
CODING_REGION_THREE_PRIME_UTR: CodingRegion

class DescribeVariantRequest(_message.Message):
    __slots__ = ("vcv", "gene", "review_status_floor", "max_pool_records")
    VCV_FIELD_NUMBER: _ClassVar[int]
    GENE_FIELD_NUMBER: _ClassVar[int]
    REVIEW_STATUS_FLOOR_FIELD_NUMBER: _ClassVar[int]
    MAX_POOL_RECORDS_FIELD_NUMBER: _ClassVar[int]
    vcv: str
    gene: str
    review_status_floor: int
    max_pool_records: int
    def __init__(self, vcv: _Optional[str] = ..., gene: _Optional[str] = ..., review_status_floor: _Optional[int] = ..., max_pool_records: _Optional[int] = ...) -> None: ...

class ClinVarZygosityCount(_message.Message):
    __slots__ = ("zygosity", "count")
    ZYGOSITY_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    zygosity: str
    count: int
    def __init__(self, zygosity: _Optional[str] = ..., count: _Optional[int] = ...) -> None: ...

class ClinVarObservation(_message.Message):
    __slots__ = ("origin", "affected_status", "zygosities", "variant_alleles", "age", "sex", "collection_method", "descriptions", "traits", "pubmed_ids")
    ORIGIN_FIELD_NUMBER: _ClassVar[int]
    AFFECTED_STATUS_FIELD_NUMBER: _ClassVar[int]
    ZYGOSITIES_FIELD_NUMBER: _ClassVar[int]
    VARIANT_ALLELES_FIELD_NUMBER: _ClassVar[int]
    AGE_FIELD_NUMBER: _ClassVar[int]
    SEX_FIELD_NUMBER: _ClassVar[int]
    COLLECTION_METHOD_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTIONS_FIELD_NUMBER: _ClassVar[int]
    TRAITS_FIELD_NUMBER: _ClassVar[int]
    PUBMED_IDS_FIELD_NUMBER: _ClassVar[int]
    origin: str
    affected_status: str
    zygosities: _containers.RepeatedCompositeFieldContainer[ClinVarZygosityCount]
    variant_alleles: int
    age: str
    sex: str
    collection_method: str
    descriptions: _containers.RepeatedScalarFieldContainer[str]
    traits: _containers.RepeatedScalarFieldContainer[str]
    pubmed_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, origin: _Optional[str] = ..., affected_status: _Optional[str] = ..., zygosities: _Optional[_Iterable[_Union[ClinVarZygosityCount, _Mapping]]] = ..., variant_alleles: _Optional[int] = ..., age: _Optional[str] = ..., sex: _Optional[str] = ..., collection_method: _Optional[str] = ..., descriptions: _Optional[_Iterable[str]] = ..., traits: _Optional[_Iterable[str]] = ..., pubmed_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class ClinVarSubmission(_message.Message):
    __slots__ = ("scv", "submitter", "organization_category", "classification", "review_status", "date_evaluated", "assertion_method", "mode_of_inheritance", "comment", "conditions", "pubmed_ids", "erepo_url", "observations")
    SCV_FIELD_NUMBER: _ClassVar[int]
    SUBMITTER_FIELD_NUMBER: _ClassVar[int]
    ORGANIZATION_CATEGORY_FIELD_NUMBER: _ClassVar[int]
    CLASSIFICATION_FIELD_NUMBER: _ClassVar[int]
    REVIEW_STATUS_FIELD_NUMBER: _ClassVar[int]
    DATE_EVALUATED_FIELD_NUMBER: _ClassVar[int]
    ASSERTION_METHOD_FIELD_NUMBER: _ClassVar[int]
    MODE_OF_INHERITANCE_FIELD_NUMBER: _ClassVar[int]
    COMMENT_FIELD_NUMBER: _ClassVar[int]
    CONDITIONS_FIELD_NUMBER: _ClassVar[int]
    PUBMED_IDS_FIELD_NUMBER: _ClassVar[int]
    EREPO_URL_FIELD_NUMBER: _ClassVar[int]
    OBSERVATIONS_FIELD_NUMBER: _ClassVar[int]
    scv: str
    submitter: str
    organization_category: str
    classification: str
    review_status: str
    date_evaluated: str
    assertion_method: str
    mode_of_inheritance: str
    comment: str
    conditions: _containers.RepeatedScalarFieldContainer[str]
    pubmed_ids: _containers.RepeatedScalarFieldContainer[str]
    erepo_url: str
    observations: _containers.RepeatedCompositeFieldContainer[ClinVarObservation]
    def __init__(self, scv: _Optional[str] = ..., submitter: _Optional[str] = ..., organization_category: _Optional[str] = ..., classification: _Optional[str] = ..., review_status: _Optional[str] = ..., date_evaluated: _Optional[str] = ..., assertion_method: _Optional[str] = ..., mode_of_inheritance: _Optional[str] = ..., comment: _Optional[str] = ..., conditions: _Optional[_Iterable[str]] = ..., pubmed_ids: _Optional[_Iterable[str]] = ..., erepo_url: _Optional[str] = ..., observations: _Optional[_Iterable[_Union[ClinVarObservation, _Mapping]]] = ...) -> None: ...

class CodingCoordinate(_message.Message):
    __slots__ = ("region", "position", "intron_offset")
    REGION_FIELD_NUMBER: _ClassVar[int]
    POSITION_FIELD_NUMBER: _ClassVar[int]
    INTRON_OFFSET_FIELD_NUMBER: _ClassVar[int]
    region: CodingRegion
    position: int
    intron_offset: int
    def __init__(self, region: _Optional[_Union[CodingRegion, str]] = ..., position: _Optional[int] = ..., intron_offset: _Optional[int] = ...) -> None: ...

class CodingSpan(_message.Message):
    __slots__ = ("transcript", "start", "end")
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    transcript: str
    start: CodingCoordinate
    end: CodingCoordinate
    def __init__(self, transcript: _Optional[str] = ..., start: _Optional[_Union[CodingCoordinate, _Mapping]] = ..., end: _Optional[_Union[CodingCoordinate, _Mapping]] = ...) -> None: ...

class ClinVarRecord(_message.Message):
    __slots__ = ("clinvar_id", "hgvs", "classification", "review_stars", "conditions", "submissions", "coding_span", "review_status")
    CLINVAR_ID_FIELD_NUMBER: _ClassVar[int]
    HGVS_FIELD_NUMBER: _ClassVar[int]
    CLASSIFICATION_FIELD_NUMBER: _ClassVar[int]
    REVIEW_STARS_FIELD_NUMBER: _ClassVar[int]
    CONDITIONS_FIELD_NUMBER: _ClassVar[int]
    SUBMISSIONS_FIELD_NUMBER: _ClassVar[int]
    CODING_SPAN_FIELD_NUMBER: _ClassVar[int]
    REVIEW_STATUS_FIELD_NUMBER: _ClassVar[int]
    clinvar_id: str
    hgvs: str
    classification: str
    review_stars: int
    conditions: _containers.RepeatedScalarFieldContainer[str]
    submissions: _containers.RepeatedCompositeFieldContainer[ClinVarSubmission]
    coding_span: CodingSpan
    review_status: str
    def __init__(self, clinvar_id: _Optional[str] = ..., hgvs: _Optional[str] = ..., classification: _Optional[str] = ..., review_stars: _Optional[int] = ..., conditions: _Optional[_Iterable[str]] = ..., submissions: _Optional[_Iterable[_Union[ClinVarSubmission, _Mapping]]] = ..., coding_span: _Optional[_Union[CodingSpan, _Mapping]] = ..., review_status: _Optional[str] = ...) -> None: ...

class DescribeVariantResponse(_message.Message):
    __slots__ = ("this_variant", "classified_in_gene", "total_in_gene", "considered_in_gene", "pool_truncated", "records_with_unparsed_hgvs", "esearch_term", "variation_archive", "provenance")
    THIS_VARIANT_FIELD_NUMBER: _ClassVar[int]
    CLASSIFIED_IN_GENE_FIELD_NUMBER: _ClassVar[int]
    TOTAL_IN_GENE_FIELD_NUMBER: _ClassVar[int]
    CONSIDERED_IN_GENE_FIELD_NUMBER: _ClassVar[int]
    POOL_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    RECORDS_WITH_UNPARSED_HGVS_FIELD_NUMBER: _ClassVar[int]
    ESEARCH_TERM_FIELD_NUMBER: _ClassVar[int]
    VARIATION_ARCHIVE_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    this_variant: ClinVarRecord
    classified_in_gene: _containers.RepeatedCompositeFieldContainer[ClinVarRecord]
    total_in_gene: int
    considered_in_gene: int
    pool_truncated: bool
    records_with_unparsed_hgvs: _containers.RepeatedScalarFieldContainer[str]
    esearch_term: str
    variation_archive: _clinvar_pb2.VariationArchiveType
    provenance: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.Provenance]
    def __init__(self, this_variant: _Optional[_Union[ClinVarRecord, _Mapping]] = ..., classified_in_gene: _Optional[_Iterable[_Union[ClinVarRecord, _Mapping]]] = ..., total_in_gene: _Optional[int] = ..., considered_in_gene: _Optional[int] = ..., pool_truncated: _Optional[bool] = ..., records_with_unparsed_hgvs: _Optional[_Iterable[str]] = ..., esearch_term: _Optional[str] = ..., variation_archive: _Optional[_Union[_clinvar_pb2.VariationArchiveType, _Mapping]] = ..., provenance: _Optional[_Iterable[_Union[_evidence_pb2.Provenance, _Mapping]]] = ...) -> None: ...

class SearchCodingSpanRequest(_message.Message):
    __slots__ = ("transcript", "cds_start", "cds_end", "max_records")
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    CDS_START_FIELD_NUMBER: _ClassVar[int]
    CDS_END_FIELD_NUMBER: _ClassVar[int]
    MAX_RECORDS_FIELD_NUMBER: _ClassVar[int]
    transcript: str
    cds_start: int
    cds_end: int
    max_records: int
    def __init__(self, transcript: _Optional[str] = ..., cds_start: _Optional[int] = ..., cds_end: _Optional[int] = ..., max_records: _Optional[int] = ...) -> None: ...

class SearchCodingSpanResponse(_message.Message):
    __slots__ = ("records", "total_in_span", "considered_in_span", "span_truncated", "records_with_unparsed_hgvs", "transcript", "gene", "chromosome_accession", "searched_span", "esearch_term", "variantvalidator_transcript", "provenance")
    RECORDS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_IN_SPAN_FIELD_NUMBER: _ClassVar[int]
    CONSIDERED_IN_SPAN_FIELD_NUMBER: _ClassVar[int]
    SPAN_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    RECORDS_WITH_UNPARSED_HGVS_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    GENE_FIELD_NUMBER: _ClassVar[int]
    CHROMOSOME_ACCESSION_FIELD_NUMBER: _ClassVar[int]
    SEARCHED_SPAN_FIELD_NUMBER: _ClassVar[int]
    ESEARCH_TERM_FIELD_NUMBER: _ClassVar[int]
    VARIANTVALIDATOR_TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    records: _containers.RepeatedCompositeFieldContainer[ClinVarRecord]
    total_in_span: int
    considered_in_span: int
    span_truncated: bool
    records_with_unparsed_hgvs: _containers.RepeatedScalarFieldContainer[str]
    transcript: str
    gene: str
    chromosome_accession: str
    searched_span: _evidence_pb2.GenomicSpan
    esearch_term: str
    variantvalidator_transcript: _struct_pb2.Struct
    provenance: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.Provenance]
    def __init__(self, records: _Optional[_Iterable[_Union[ClinVarRecord, _Mapping]]] = ..., total_in_span: _Optional[int] = ..., considered_in_span: _Optional[int] = ..., span_truncated: _Optional[bool] = ..., records_with_unparsed_hgvs: _Optional[_Iterable[str]] = ..., transcript: _Optional[str] = ..., gene: _Optional[str] = ..., chromosome_accession: _Optional[str] = ..., searched_span: _Optional[_Union[_evidence_pb2.GenomicSpan, _Mapping]] = ..., esearch_term: _Optional[str] = ..., variantvalidator_transcript: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., provenance: _Optional[_Iterable[_Union[_evidence_pb2.Provenance, _Mapping]]] = ...) -> None: ...
