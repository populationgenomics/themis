from google.protobuf import struct_pb2 as _struct_pb2
from themis.evidence.models import evidence_pb2 as _evidence_pb2
from themis.rpc import sandbox_options_pb2 as _sandbox_options_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class NormalizeRequest(_message.Message):
    __slots__ = ("variant", "genome_build")
    VARIANT_FIELD_NUMBER: _ClassVar[int]
    GENOME_BUILD_FIELD_NUMBER: _ClassVar[int]
    variant: str
    genome_build: str
    def __init__(self, variant: _Optional[str] = ..., genome_build: _Optional[str] = ...) -> None: ...

class NormalizeResponse(_message.Message):
    __slots__ = ("caid", "gnomad_v4_id", "gnomad_v2_id", "consequence", "transcripts", "gene_symbol", "hgnc_id", "clinvar_variations", "clinvar_alleles", "raw", "provenance")
    CAID_FIELD_NUMBER: _ClassVar[int]
    GNOMAD_V4_ID_FIELD_NUMBER: _ClassVar[int]
    GNOMAD_V2_ID_FIELD_NUMBER: _ClassVar[int]
    CONSEQUENCE_FIELD_NUMBER: _ClassVar[int]
    TRANSCRIPTS_FIELD_NUMBER: _ClassVar[int]
    GENE_SYMBOL_FIELD_NUMBER: _ClassVar[int]
    HGNC_ID_FIELD_NUMBER: _ClassVar[int]
    CLINVAR_VARIATIONS_FIELD_NUMBER: _ClassVar[int]
    CLINVAR_ALLELES_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    caid: str
    gnomad_v4_id: str
    gnomad_v2_id: str
    consequence: _evidence_pb2.Consequence
    transcripts: _containers.RepeatedCompositeFieldContainer[TranscriptProjection]
    gene_symbol: str
    hgnc_id: str
    clinvar_variations: _containers.RepeatedCompositeFieldContainer[ClinVarVariation]
    clinvar_alleles: _containers.RepeatedCompositeFieldContainer[ClinVarAllele]
    raw: _struct_pb2.Struct
    provenance: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.Provenance]
    def __init__(self, caid: _Optional[str] = ..., gnomad_v4_id: _Optional[str] = ..., gnomad_v2_id: _Optional[str] = ..., consequence: _Optional[_Union[_evidence_pb2.Consequence, str]] = ..., transcripts: _Optional[_Iterable[_Union[TranscriptProjection, _Mapping]]] = ..., gene_symbol: _Optional[str] = ..., hgnc_id: _Optional[str] = ..., clinvar_variations: _Optional[_Iterable[_Union[ClinVarVariation, _Mapping]]] = ..., clinvar_alleles: _Optional[_Iterable[_Union[ClinVarAllele, _Mapping]]] = ..., raw: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., provenance: _Optional[_Iterable[_Union[_evidence_pb2.Provenance, _Mapping]]] = ...) -> None: ...

class TranscriptProjection(_message.Message):
    __slots__ = ("transcript", "hgvs_c", "hgvs_p", "mane_select", "mane_plus_clinical", "sources", "accession_base")
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    HGVS_C_FIELD_NUMBER: _ClassVar[int]
    HGVS_P_FIELD_NUMBER: _ClassVar[int]
    MANE_SELECT_FIELD_NUMBER: _ClassVar[int]
    MANE_PLUS_CLINICAL_FIELD_NUMBER: _ClassVar[int]
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    ACCESSION_BASE_FIELD_NUMBER: _ClassVar[int]
    transcript: str
    hgvs_c: str
    hgvs_p: str
    mane_select: bool
    mane_plus_clinical: bool
    sources: _containers.RepeatedScalarFieldContainer[str]
    accession_base: str
    def __init__(self, transcript: _Optional[str] = ..., hgvs_c: _Optional[str] = ..., hgvs_p: _Optional[str] = ..., mane_select: _Optional[bool] = ..., mane_plus_clinical: _Optional[bool] = ..., sources: _Optional[_Iterable[str]] = ..., accession_base: _Optional[str] = ...) -> None: ...

class ClinVarVariation(_message.Message):
    __slots__ = ("variation_id", "vcv", "rcv")
    VARIATION_ID_FIELD_NUMBER: _ClassVar[int]
    VCV_FIELD_NUMBER: _ClassVar[int]
    RCV_FIELD_NUMBER: _ClassVar[int]
    variation_id: int
    vcv: str
    rcv: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, variation_id: _Optional[int] = ..., vcv: _Optional[str] = ..., rcv: _Optional[_Iterable[str]] = ...) -> None: ...

class ClinVarAllele(_message.Message):
    __slots__ = ("allele_id", "preferred_name")
    ALLELE_ID_FIELD_NUMBER: _ClassVar[int]
    PREFERRED_NAME_FIELD_NUMBER: _ClassVar[int]
    allele_id: int
    preferred_name: str
    def __init__(self, allele_id: _Optional[int] = ..., preferred_name: _Optional[str] = ...) -> None: ...
