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

class SpliceProduct(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SPLICE_PRODUCT_UNSPECIFIED: _ClassVar[SpliceProduct]
    SPLICE_PRODUCT_INFRAME_DELETION: _ClassVar[SpliceProduct]
    SPLICE_PRODUCT_PREMATURE_STOP: _ClassVar[SpliceProduct]
    SPLICE_PRODUCT_EXTENDED_TERMINATION: _ClassVar[SpliceProduct]
    SPLICE_PRODUCT_NO_TERMINATION: _ClassVar[SpliceProduct]
    SPLICE_PRODUCT_START_LOST: _ClassVar[SpliceProduct]
SPLICE_PRODUCT_UNSPECIFIED: SpliceProduct
SPLICE_PRODUCT_INFRAME_DELETION: SpliceProduct
SPLICE_PRODUCT_PREMATURE_STOP: SpliceProduct
SPLICE_PRODUCT_EXTENDED_TERMINATION: SpliceProduct
SPLICE_PRODUCT_NO_TERMINATION: SpliceProduct
SPLICE_PRODUCT_START_LOST: SpliceProduct

class PredictDeltasRequest(_message.Message):
    __slots__ = ("variant",)
    VARIANT_FIELD_NUMBER: _ClassVar[int]
    variant: str
    def __init__(self, variant: _Optional[str] = ...) -> None: ...

class PredictDeltasResponse(_message.Message):
    __slots__ = ("spliceai_gain", "spliceai_loss", "pangolin_gain", "pangolin_loss", "raw", "provenance")
    SPLICEAI_GAIN_FIELD_NUMBER: _ClassVar[int]
    SPLICEAI_LOSS_FIELD_NUMBER: _ClassVar[int]
    PANGOLIN_GAIN_FIELD_NUMBER: _ClassVar[int]
    PANGOLIN_LOSS_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    spliceai_gain: float
    spliceai_loss: float
    pangolin_gain: float
    pangolin_loss: float
    raw: _struct_pb2.Struct
    provenance: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.Provenance]
    def __init__(self, spliceai_gain: _Optional[float] = ..., spliceai_loss: _Optional[float] = ..., pangolin_gain: _Optional[float] = ..., pangolin_loss: _Optional[float] = ..., raw: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., provenance: _Optional[_Iterable[_Union[_evidence_pb2.Provenance, _Mapping]]] = ...) -> None: ...

class PredictSkipOutcomeRequest(_message.Message):
    __slots__ = ("transcript", "genome_build", "exon", "cds_position")
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    GENOME_BUILD_FIELD_NUMBER: _ClassVar[int]
    EXON_FIELD_NUMBER: _ClassVar[int]
    CDS_POSITION_FIELD_NUMBER: _ClassVar[int]
    transcript: str
    genome_build: str
    exon: int
    cds_position: int
    def __init__(self, transcript: _Optional[str] = ..., genome_build: _Optional[str] = ..., exon: _Optional[int] = ..., cds_position: _Optional[int] = ...) -> None: ...

class PredictedSkip(_message.Message):
    __slots__ = ("skipped_exons", "coding_nt_removed", "frame_shift", "product", "ptc_cds_position", "ptc_codon", "nt_upstream_of_last_junction", "nmd_predicted")
    SKIPPED_EXONS_FIELD_NUMBER: _ClassVar[int]
    CODING_NT_REMOVED_FIELD_NUMBER: _ClassVar[int]
    FRAME_SHIFT_FIELD_NUMBER: _ClassVar[int]
    PRODUCT_FIELD_NUMBER: _ClassVar[int]
    PTC_CDS_POSITION_FIELD_NUMBER: _ClassVar[int]
    PTC_CODON_FIELD_NUMBER: _ClassVar[int]
    NT_UPSTREAM_OF_LAST_JUNCTION_FIELD_NUMBER: _ClassVar[int]
    NMD_PREDICTED_FIELD_NUMBER: _ClassVar[int]
    skipped_exons: _containers.RepeatedScalarFieldContainer[int]
    coding_nt_removed: int
    frame_shift: int
    product: SpliceProduct
    ptc_cds_position: int
    ptc_codon: int
    nt_upstream_of_last_junction: int
    nmd_predicted: bool
    def __init__(self, skipped_exons: _Optional[_Iterable[int]] = ..., coding_nt_removed: _Optional[int] = ..., frame_shift: _Optional[int] = ..., product: _Optional[_Union[SpliceProduct, str]] = ..., ptc_cds_position: _Optional[int] = ..., ptc_codon: _Optional[int] = ..., nt_upstream_of_last_junction: _Optional[int] = ..., nmd_predicted: _Optional[bool] = ...) -> None: ...

class PredictSkipOutcomeResponse(_message.Message):
    __slots__ = ("transcript", "affected_exon", "gene", "genome_build", "skips", "raw", "provenance")
    TRANSCRIPT_FIELD_NUMBER: _ClassVar[int]
    AFFECTED_EXON_FIELD_NUMBER: _ClassVar[int]
    GENE_FIELD_NUMBER: _ClassVar[int]
    GENOME_BUILD_FIELD_NUMBER: _ClassVar[int]
    SKIPS_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    transcript: str
    affected_exon: int
    gene: str
    genome_build: str
    skips: _containers.RepeatedCompositeFieldContainer[PredictedSkip]
    raw: _struct_pb2.Struct
    provenance: _containers.RepeatedCompositeFieldContainer[_evidence_pb2.Provenance]
    def __init__(self, transcript: _Optional[str] = ..., affected_exon: _Optional[int] = ..., gene: _Optional[str] = ..., genome_build: _Optional[str] = ..., skips: _Optional[_Iterable[_Union[PredictedSkip, _Mapping]]] = ..., raw: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., provenance: _Optional[_Iterable[_Union[_evidence_pb2.Provenance, _Mapping]]] = ...) -> None: ...
