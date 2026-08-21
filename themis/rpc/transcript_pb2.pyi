from google.protobuf import struct_pb2 as _struct_pb2
from themis.evidence.models import evidence_pb2 as _evidence_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ExonMembership(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EXON_MEMBERSHIP_UNSPECIFIED: _ClassVar[ExonMembership]
    EXON_MEMBERSHIP_CARRIES_THE_EXON: _ClassVar[ExonMembership]
    EXON_MEMBERSHIP_CARRIES_A_DIFFERENT_INTERVAL: _ClassVar[ExonMembership]
    EXON_MEMBERSHIP_SPANS_BUT_SKIPS: _ClassVar[ExonMembership]
    EXON_MEMBERSHIP_DOES_NOT_REACH: _ClassVar[ExonMembership]

class TranscriptNamespace(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRANSCRIPT_NAMESPACE_UNSPECIFIED: _ClassVar[TranscriptNamespace]
    TRANSCRIPT_NAMESPACE_REFSEQ: _ClassVar[TranscriptNamespace]
    TRANSCRIPT_NAMESPACE_ENSEMBL: _ClassVar[TranscriptNamespace]
EXON_MEMBERSHIP_UNSPECIFIED: ExonMembership
EXON_MEMBERSHIP_CARRIES_THE_EXON: ExonMembership
EXON_MEMBERSHIP_CARRIES_A_DIFFERENT_INTERVAL: ExonMembership
EXON_MEMBERSHIP_SPANS_BUT_SKIPS: ExonMembership
EXON_MEMBERSHIP_DOES_NOT_REACH: ExonMembership
TRANSCRIPT_NAMESPACE_UNSPECIFIED: TranscriptNamespace
TRANSCRIPT_NAMESPACE_REFSEQ: TranscriptNamespace
TRANSCRIPT_NAMESPACE_ENSEMBL: TranscriptNamespace

class AssessExonRelevanceRequest(_message.Message):
    __slots__ = ("gene", "transcript", "exon", "in_mane_select", "in_mane_plus_clinical", "tissues", "include_gtex_detail")
    GENE_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    EXON_FIELD_NUMBER: _ClassVar[int]
    IN_MANE_SELECT_FIELD_NUMBER: _ClassVar[int]
    IN_MANE_PLUS_CLINICAL_FIELD_NUMBER: _ClassVar[int]
    TISSUES_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_GTEX_DETAIL_FIELD_NUMBER: _ClassVar[int]
    gene: str
    transcript: str
    exon: int
    in_mane_select: bool
    in_mane_plus_clinical: bool
    tissues: _containers.RepeatedScalarFieldContainer[str]
    include_gtex_detail: bool
    def __init__(self, gene: _Optional[str] = ..., transcript: _Optional[str] = ..., exon: _Optional[int] = ..., in_mane_select: _Optional[bool] = ..., in_mane_plus_clinical: _Optional[bool] = ..., tissues: _Optional[_Iterable[str]] = ..., include_gtex_detail: _Optional[bool] = ...) -> None: ...

class TranscriptExpression(_message.Message):
    __slots__ = ("transcript", "tissue", "median_tpm", "transcript_base")
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    TISSUE_FIELD_NUMBER: _ClassVar[int]
    MEDIAN_TPM_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPT_BASE_FIELD_NUMBER: _ClassVar[int]
    transcript: str
    tissue: str
    median_tpm: float
    transcript_base: str
    def __init__(self, transcript: _Optional[str] = ..., tissue: _Optional[str] = ..., median_tpm: _Optional[float] = ..., transcript_base: _Optional[str] = ...) -> None: ...

class TranscriptExonMembership(_message.Message):
    __slots__ = ("transcript", "accession_base", "namespace", "mane_select", "mane_plus_clinical", "coding", "membership", "expression", "assessed_transcript", "overlapping_exons")
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    ACCESSION_BASE_FIELD_NUMBER: _ClassVar[int]
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    MANE_SELECT_FIELD_NUMBER: _ClassVar[int]
    MANE_PLUS_CLINICAL_FIELD_NUMBER: _ClassVar[int]
    CODING_FIELD_NUMBER: _ClassVar[int]
    MEMBERSHIP_FIELD_NUMBER: _ClassVar[int]
    EXPRESSION_FIELD_NUMBER: _ClassVar[int]
    ASSESSED_TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    OVERLAPPING_EXONS_FIELD_NUMBER: _ClassVar[int]
    transcript: str
    accession_base: str
    namespace: TranscriptNamespace
    mane_select: bool
    mane_plus_clinical: bool
    coding: bool
    membership: ExonMembership
    expression: _containers.RepeatedCompositeFieldContainer[TranscriptExpression]
    assessed_transcript: bool
    overlapping_exons: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.GenomicSpan]
    def __init__(self, transcript: _Optional[str] = ..., accession_base: _Optional[str] = ..., namespace: _Optional[_Union[TranscriptNamespace, str]] = ..., mane_select: _Optional[bool] = ..., mane_plus_clinical: _Optional[bool] = ..., coding: _Optional[bool] = ..., membership: _Optional[_Union[ExonMembership, str]] = ..., expression: _Optional[_Iterable[_Union[TranscriptExpression, _Mapping]]] = ..., assessed_transcript: _Optional[bool] = ..., overlapping_exons: _Optional[_Iterable[_Union[_evidence_pb2.GenomicSpan, _Mapping]]] = ...) -> None: ...

class TranscriptInventoryDenominator(_message.Message):
    __slots__ = ("namespace", "transcripts_considered", "transcripts_not_classified")
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPTS_CONSIDERED_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPTS_NOT_CLASSIFIED_FIELD_NUMBER: _ClassVar[int]
    namespace: TranscriptNamespace
    transcripts_considered: int
    transcripts_not_classified: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, namespace: _Optional[_Union[TranscriptNamespace, str]] = ..., transcripts_considered: _Optional[int] = ..., transcripts_not_classified: _Optional[_Iterable[str]] = ...) -> None: ...

class ManeSelectPair(_message.Message):
    __slots__ = ("refseq", "ensembl")
    REFSEQ_FIELD_NUMBER: _ClassVar[int]
    ENSEMBL_FIELD_NUMBER: _ClassVar[int]
    refseq: str
    ensembl: str
    def __init__(self, refseq: _Optional[str] = ..., ensembl: _Optional[str] = ...) -> None: ...

class PextTissueValue(_message.Message):
    __slots__ = ("tissue", "value")
    TISSUE_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    tissue: str
    value: float
    def __init__(self, tissue: _Optional[str] = ..., value: _Optional[float] = ...) -> None: ...

class ExonPext(_message.Message):
    __slots__ = ("exon", "mean", "tissues", "covered_bases", "exon_bases")
    EXON_FIELD_NUMBER: _ClassVar[int]
    MEAN_FIELD_NUMBER: _ClassVar[int]
    TISSUES_FIELD_NUMBER: _ClassVar[int]
    COVERED_BASES_FIELD_NUMBER: _ClassVar[int]
    EXON_BASES_FIELD_NUMBER: _ClassVar[int]
    exon: int
    mean: float
    tissues: _containers.RepeatedCompositeFieldContainer[PextTissueValue]
    covered_bases: int
    exon_bases: int
    def __init__(self, exon: _Optional[int] = ..., mean: _Optional[float] = ..., tissues: _Optional[_Iterable[_Union[PextTissueValue, _Mapping]]] = ..., covered_bases: _Optional[int] = ..., exon_bases: _Optional[int] = ...) -> None: ...

class AssessExonRelevanceResponse(_message.Message):
    __slots__ = ("in_mane_select", "in_mane_plus_clinical", "pext", "loeuf", "clinvar_plp_density", "gtex_expression", "tissues_without_pext", "tissues_without_expression", "transcript_inventory", "inventory_denominators", "transcripts_without_structure", "transcripts_without_expression", "pext_mane_select", "raw", "provenance")
    IN_MANE_SELECT_FIELD_NUMBER: _ClassVar[int]
    IN_MANE_PLUS_CLINICAL_FIELD_NUMBER: _ClassVar[int]
    PEXT_FIELD_NUMBER: _ClassVar[int]
    LOEUF_FIELD_NUMBER: _ClassVar[int]
    CLINVAR_PLP_DENSITY_FIELD_NUMBER: _ClassVar[int]
    GTEX_EXPRESSION_FIELD_NUMBER: _ClassVar[int]
    TISSUES_WITHOUT_PEXT_FIELD_NUMBER: _ClassVar[int]
    TISSUES_WITHOUT_EXPRESSION_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPT_INVENTORY_FIELD_NUMBER: _ClassVar[int]
    INVENTORY_DENOMINATORS_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPTS_WITHOUT_STRUCTURE_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPTS_WITHOUT_EXPRESSION_FIELD_NUMBER: _ClassVar[int]
    PEXT_MANE_SELECT_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    in_mane_select: bool
    in_mane_plus_clinical: bool
    pext: _containers.RepeatedCompositeFieldContainer[ExonPext]
    loeuf: float
    clinvar_plp_density: int
    gtex_expression: _containers.RepeatedCompositeFieldContainer[TranscriptExpression]
    tissues_without_pext: _containers.RepeatedScalarFieldContainer[str]
    tissues_without_expression: _containers.RepeatedScalarFieldContainer[str]
    transcript_inventory: _containers.RepeatedCompositeFieldContainer[TranscriptExonMembership]
    inventory_denominators: _containers.RepeatedCompositeFieldContainer[TranscriptInventoryDenominator]
    transcripts_without_structure: _containers.RepeatedScalarFieldContainer[str]
    transcripts_without_expression: _containers.RepeatedScalarFieldContainer[str]
    pext_mane_select: ManeSelectPair
    raw: _struct_pb2.Struct
    provenance: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.Provenance]
    def __init__(self, in_mane_select: _Optional[bool] = ..., in_mane_plus_clinical: _Optional[bool] = ..., pext: _Optional[_Iterable[_Union[ExonPext, _Mapping]]] = ..., loeuf: _Optional[float] = ..., clinvar_plp_density: _Optional[int] = ..., gtex_expression: _Optional[_Iterable[_Union[TranscriptExpression, _Mapping]]] = ..., tissues_without_pext: _Optional[_Iterable[str]] = ..., tissues_without_expression: _Optional[_Iterable[str]] = ..., transcript_inventory: _Optional[_Iterable[_Union[TranscriptExonMembership, _Mapping]]] = ..., inventory_denominators: _Optional[_Iterable[_Union[TranscriptInventoryDenominator, _Mapping]]] = ..., transcripts_without_structure: _Optional[_Iterable[str]] = ..., transcripts_without_expression: _Optional[_Iterable[str]] = ..., pext_mane_select: _Optional[_Union[ManeSelectPair, _Mapping]] = ..., raw: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., provenance: _Optional[_Iterable[_Union[_evidence_pb2.Provenance, _Mapping]]] = ...) -> None: ...

class GetStructureRequest(_message.Message):
    __slots__ = ("transcript", "genome_build", "cds_position", "genomic_position")
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    GENOME_BUILD_FIELD_NUMBER: _ClassVar[int]
    CDS_POSITION_FIELD_NUMBER: _ClassVar[int]
    GENOMIC_POSITION_FIELD_NUMBER: _ClassVar[int]
    transcript: str
    genome_build: str
    cds_position: int
    genomic_position: int
    def __init__(self, transcript: _Optional[str] = ..., genome_build: _Optional[str] = ..., cds_position: _Optional[int] = ..., genomic_position: _Optional[int] = ...) -> None: ...

class Exon(_message.Message):
    __slots__ = ("number", "genomic_start", "genomic_end", "transcript_start", "transcript_end", "cds_start", "cds_end", "length", "coding_length", "frame_shift_if_skipped")
    NUMBER_FIELD_NUMBER: _ClassVar[int]
    GENOMIC_START_FIELD_NUMBER: _ClassVar[int]
    GENOMIC_END_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPT_START_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPT_END_FIELD_NUMBER: _ClassVar[int]
    CDS_START_FIELD_NUMBER: _ClassVar[int]
    CDS_END_FIELD_NUMBER: _ClassVar[int]
    LENGTH_FIELD_NUMBER: _ClassVar[int]
    CODING_LENGTH_FIELD_NUMBER: _ClassVar[int]
    FRAME_SHIFT_IF_SKIPPED_FIELD_NUMBER: _ClassVar[int]
    number: int
    genomic_start: int
    genomic_end: int
    transcript_start: int
    transcript_end: int
    cds_start: int
    cds_end: int
    length: int
    coding_length: int
    frame_shift_if_skipped: int
    def __init__(self, number: _Optional[int] = ..., genomic_start: _Optional[int] = ..., genomic_end: _Optional[int] = ..., transcript_start: _Optional[int] = ..., transcript_end: _Optional[int] = ..., cds_start: _Optional[int] = ..., cds_end: _Optional[int] = ..., length: _Optional[int] = ..., coding_length: _Optional[int] = ..., frame_shift_if_skipped: _Optional[int] = ...) -> None: ...

class TranscriptPosition(_message.Message):
    __slots__ = ("exon", "nt_from_exon_start", "nt_to_exon_end", "intron", "nt_from_intron_start", "nt_to_intron_end", "cds_position")
    EXON_FIELD_NUMBER: _ClassVar[int]
    NT_FROM_EXON_START_FIELD_NUMBER: _ClassVar[int]
    NT_TO_EXON_END_FIELD_NUMBER: _ClassVar[int]
    INTRON_FIELD_NUMBER: _ClassVar[int]
    NT_FROM_INTRON_START_FIELD_NUMBER: _ClassVar[int]
    NT_TO_INTRON_END_FIELD_NUMBER: _ClassVar[int]
    CDS_POSITION_FIELD_NUMBER: _ClassVar[int]
    exon: int
    nt_from_exon_start: int
    nt_to_exon_end: int
    intron: int
    nt_from_intron_start: int
    nt_to_intron_end: int
    cds_position: int
    def __init__(self, exon: _Optional[int] = ..., nt_from_exon_start: _Optional[int] = ..., nt_to_exon_end: _Optional[int] = ..., intron: _Optional[int] = ..., nt_from_intron_start: _Optional[int] = ..., nt_to_intron_end: _Optional[int] = ..., cds_position: _Optional[int] = ...) -> None: ...

class GetStructureResponse(_message.Message):
    __slots__ = ("transcript", "gene", "genome_build", "chromosome_accession", "strand", "mane_select", "mane_plus_clinical", "transcript_length", "cds_transcript_start", "cds_transcript_end", "coding_length", "exons", "position", "raw", "provenance")
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    GENE_FIELD_NUMBER: _ClassVar[int]
    GENOME_BUILD_FIELD_NUMBER: _ClassVar[int]
    CHROMOSOME_ACCESSION_FIELD_NUMBER: _ClassVar[int]
    STRAND_FIELD_NUMBER: _ClassVar[int]
    MANE_SELECT_FIELD_NUMBER: _ClassVar[int]
    MANE_PLUS_CLINICAL_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPT_LENGTH_FIELD_NUMBER: _ClassVar[int]
    CDS_TRANSCRIPT_START_FIELD_NUMBER: _ClassVar[int]
    CDS_TRANSCRIPT_END_FIELD_NUMBER: _ClassVar[int]
    CODING_LENGTH_FIELD_NUMBER: _ClassVar[int]
    EXONS_FIELD_NUMBER: _ClassVar[int]
    POSITION_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    transcript: str
    gene: str
    genome_build: str
    chromosome_accession: str
    strand: int
    mane_select: bool
    mane_plus_clinical: bool
    transcript_length: int
    cds_transcript_start: int
    cds_transcript_end: int
    coding_length: int
    exons: _containers.RepeatedCompositeFieldContainer[Exon]
    position: TranscriptPosition
    raw: _struct_pb2.Struct
    provenance: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.Provenance]
    def __init__(self, transcript: _Optional[str] = ..., gene: _Optional[str] = ..., genome_build: _Optional[str] = ..., chromosome_accession: _Optional[str] = ..., strand: _Optional[int] = ..., mane_select: _Optional[bool] = ..., mane_plus_clinical: _Optional[bool] = ..., transcript_length: _Optional[int] = ..., cds_transcript_start: _Optional[int] = ..., cds_transcript_end: _Optional[int] = ..., coding_length: _Optional[int] = ..., exons: _Optional[_Iterable[_Union[Exon, _Mapping]]] = ..., position: _Optional[_Union[TranscriptPosition, _Mapping]] = ..., raw: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., provenance: _Optional[_Iterable[_Union[_evidence_pb2.Provenance, _Mapping]]] = ...) -> None: ...
