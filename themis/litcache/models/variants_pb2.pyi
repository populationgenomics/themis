from buf.validate import validate_pb2 as _validate_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Disposition(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DISPOSITION_UNSPECIFIED: _ClassVar[Disposition]
    DISPOSITION_HAS_CANDIDATES: _ClassVar[Disposition]
    DISPOSITION_NOT_WRITABLE: _ClassVar[Disposition]
    DISPOSITION_REGISTRY: _ClassVar[Disposition]
    DISPOSITION_NOT_SEQUENCE_CHANGE: _ClassVar[Disposition]

class Species(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SPECIES_UNSPECIFIED: _ClassVar[Species]
    SPECIES_BUDDING_YEAST: _ClassVar[Species]
    SPECIES_NEMATODE: _ClassVar[Species]
    SPECIES_FRUIT_FLY: _ClassVar[Species]
    SPECIES_ZEBRAFISH: _ClassVar[Species]
    SPECIES_HUMAN: _ClassVar[Species]
    SPECIES_MOUSE: _ClassVar[Species]
    SPECIES_RAT: _ClassVar[Species]

class Context(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONTEXT_UNSPECIFIED: _ClassVar[Context]
    CONTEXT_OBSERVED: _ClassVar[Context]
    CONTEXT_CITED: _ClassVar[Context]
    CONTEXT_ENGINEERED: _ClassVar[Context]
    CONTEXT_UNCLEAR: _ClassVar[Context]

class RewriteKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    REWRITE_KIND_UNSPECIFIED: _ClassVar[RewriteKind]
    REWRITE_KIND_NOTATION: _ClassVar[RewriteKind]
    REWRITE_KIND_NUMBERING: _ClassVar[RewriteKind]
    REWRITE_KIND_REFERENCE: _ClassVar[RewriteKind]
    REWRITE_KIND_TRANSCRIPT_FIT: _ClassVar[RewriteKind]
    REWRITE_KIND_VERSION_UPDATE: _ClassVar[RewriteKind]

class Outcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OUTCOME_UNSPECIFIED: _ClassVar[Outcome]
    OUTCOME_VALIDATED: _ClassVar[Outcome]
    OUTCOME_NOTHING_TO_CHECK: _ClassVar[Outcome]
    OUTCOME_REFERENCE_DISAGREES: _ClassVar[Outcome]
    OUTCOME_NOT_CHECKED: _ClassVar[Outcome]
    OUTCOME_UNPARSEABLE: _ClassVar[Outcome]
    OUTCOME_REFERENCE_NOT_HELD: _ClassVar[Outcome]
    OUTCOME_REVERSES_PRINTED: _ClassVar[Outcome]

class Anchor(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ANCHOR_UNSPECIFIED: _ClassVar[Anchor]
    ANCHOR_STATED_ACCESSION: _ClassVar[Anchor]
    ANCHOR_MANE: _ClassVar[Anchor]
    ANCHOR_GENOMIC_STATED_BUILD: _ClassVar[Anchor]
    ANCHOR_STATED_OTHER_VERSION: _ClassVar[Anchor]
    ANCHOR_FLANKING: _ClassVar[Anchor]
    ANCHOR_OTHER_TRANSCRIPT: _ClassVar[Anchor]
    ANCHOR_GENOMIC_UNSTATED_BUILD: _ClassVar[Anchor]
    ANCHOR_GENE_FIT: _ClassVar[Anchor]

class Choice(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CHOICE_UNSPECIFIED: _ClassVar[Choice]
    CHOICE_CHOSEN: _ClassVar[Choice]
    CHOICE_AMBIGUOUS: _ClassVar[Choice]
    CHOICE_NONE: _ClassVar[Choice]
DISPOSITION_UNSPECIFIED: Disposition
DISPOSITION_HAS_CANDIDATES: Disposition
DISPOSITION_NOT_WRITABLE: Disposition
DISPOSITION_REGISTRY: Disposition
DISPOSITION_NOT_SEQUENCE_CHANGE: Disposition
SPECIES_UNSPECIFIED: Species
SPECIES_BUDDING_YEAST: Species
SPECIES_NEMATODE: Species
SPECIES_FRUIT_FLY: Species
SPECIES_ZEBRAFISH: Species
SPECIES_HUMAN: Species
SPECIES_MOUSE: Species
SPECIES_RAT: Species
CONTEXT_UNSPECIFIED: Context
CONTEXT_OBSERVED: Context
CONTEXT_CITED: Context
CONTEXT_ENGINEERED: Context
CONTEXT_UNCLEAR: Context
REWRITE_KIND_UNSPECIFIED: RewriteKind
REWRITE_KIND_NOTATION: RewriteKind
REWRITE_KIND_NUMBERING: RewriteKind
REWRITE_KIND_REFERENCE: RewriteKind
REWRITE_KIND_TRANSCRIPT_FIT: RewriteKind
REWRITE_KIND_VERSION_UPDATE: RewriteKind
OUTCOME_UNSPECIFIED: Outcome
OUTCOME_VALIDATED: Outcome
OUTCOME_NOTHING_TO_CHECK: Outcome
OUTCOME_REFERENCE_DISAGREES: Outcome
OUTCOME_NOT_CHECKED: Outcome
OUTCOME_UNPARSEABLE: Outcome
OUTCOME_REFERENCE_NOT_HELD: Outcome
OUTCOME_REVERSES_PRINTED: Outcome
ANCHOR_UNSPECIFIED: Anchor
ANCHOR_STATED_ACCESSION: Anchor
ANCHOR_MANE: Anchor
ANCHOR_GENOMIC_STATED_BUILD: Anchor
ANCHOR_STATED_OTHER_VERSION: Anchor
ANCHOR_FLANKING: Anchor
ANCHOR_OTHER_TRANSCRIPT: Anchor
ANCHOR_GENOMIC_UNSTATED_BUILD: Anchor
ANCHOR_GENE_FIT: Anchor
CHOICE_UNSPECIFIED: Choice
CHOICE_CHOSEN: Choice
CHOICE_AMBIGUOUS: Choice
CHOICE_NONE: Choice

class VariantExtractionConstruction(_message.Message):
    __slots__ = ("model", "harness_version")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    HARNESS_VERSION_FIELD_NUMBER: _ClassVar[int]
    model: str
    harness_version: str
    def __init__(self, model: _Optional[str] = ..., harness_version: _Optional[str] = ...) -> None: ...

class StoreVersion(_message.Message):
    __slots__ = ("name", "release")
    NAME_FIELD_NUMBER: _ClassVar[int]
    RELEASE_FIELD_NUMBER: _ClassVar[int]
    name: str
    release: str
    def __init__(self, name: _Optional[str] = ..., release: _Optional[str] = ...) -> None: ...

class VariantValidationConstruction(_message.Message):
    __slots__ = ("weaver_version", "stores", "validator_version")
    WEAVER_VERSION_FIELD_NUMBER: _ClassVar[int]
    STORES_FIELD_NUMBER: _ClassVar[int]
    VALIDATOR_VERSION_FIELD_NUMBER: _ClassVar[int]
    weaver_version: str
    stores: _containers.RepeatedCompositeFieldContainer[StoreVersion]
    validator_version: str
    def __init__(self, weaver_version: _Optional[str] = ..., stores: _Optional[_Iterable[_Union[StoreVersion, _Mapping]]] = ..., validator_version: _Optional[str] = ...) -> None: ...

class DeclaredReference(_message.Message):
    __slots__ = ("accession", "gene", "sentence")
    ACCESSION_FIELD_NUMBER: _ClassVar[int]
    GENE_FIELD_NUMBER: _ClassVar[int]
    SENTENCE_FIELD_NUMBER: _ClassVar[int]
    accession: str
    gene: str
    sentence: str
    def __init__(self, accession: _Optional[str] = ..., gene: _Optional[str] = ..., sentence: _Optional[str] = ...) -> None: ...

class DeclaredBuild(_message.Message):
    __slots__ = ("build", "sentence")
    BUILD_FIELD_NUMBER: _ClassVar[int]
    SENTENCE_FIELD_NUMBER: _ClassVar[int]
    build: str
    sentence: str
    def __init__(self, build: _Optional[str] = ..., sentence: _Optional[str] = ...) -> None: ...

class DeclaredConvention(_message.Message):
    __slots__ = ("gene", "sentence")
    GENE_FIELD_NUMBER: _ClassVar[int]
    SENTENCE_FIELD_NUMBER: _ClassVar[int]
    gene: str
    sentence: str
    def __init__(self, gene: _Optional[str] = ..., sentence: _Optional[str] = ...) -> None: ...

class Rewrite(_message.Message):
    __slots__ = ("kind", "declared", "note", "source")
    KIND_FIELD_NUMBER: _ClassVar[int]
    DECLARED_FIELD_NUMBER: _ClassVar[int]
    NOTE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    kind: RewriteKind
    declared: bool
    note: str
    source: str
    def __init__(self, kind: _Optional[_Union[RewriteKind, str]] = ..., declared: _Optional[bool] = ..., note: _Optional[str] = ..., source: _Optional[str] = ...) -> None: ...

class Candidate(_message.Message):
    __slots__ = ("hgvs", "rewrites", "flanking")
    HGVS_FIELD_NUMBER: _ClassVar[int]
    REWRITES_FIELD_NUMBER: _ClassVar[int]
    FLANKING_FIELD_NUMBER: _ClassVar[int]
    hgvs: str
    rewrites: _containers.RepeatedCompositeFieldContainer[Rewrite]
    flanking: str
    def __init__(self, hgvs: _Optional[str] = ..., rewrites: _Optional[_Iterable[_Union[Rewrite, _Mapping]]] = ..., flanking: _Optional[str] = ...) -> None: ...

class Box(_message.Message):
    __slots__ = ("left", "top", "right", "bottom")
    LEFT_FIELD_NUMBER: _ClassVar[int]
    TOP_FIELD_NUMBER: _ClassVar[int]
    RIGHT_FIELD_NUMBER: _ClassVar[int]
    BOTTOM_FIELD_NUMBER: _ClassVar[int]
    left: float
    top: float
    right: float
    bottom: float
    def __init__(self, left: _Optional[float] = ..., top: _Optional[float] = ..., right: _Optional[float] = ..., bottom: _Optional[float] = ...) -> None: ...

class Occurrence(_message.Message):
    __slots__ = ("context", "line", "page", "box")
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    BOX_FIELD_NUMBER: _ClassVar[int]
    context: Context
    line: int
    page: int
    box: Box
    def __init__(self, context: _Optional[_Union[Context, str]] = ..., line: _Optional[int] = ..., page: _Optional[int] = ..., box: _Optional[_Union[Box, _Mapping]] = ...) -> None: ...

class Mention(_message.Message):
    __slots__ = ("verbatim", "gene", "disposition", "reason", "candidates", "occurrences", "known_species", "printed_species")
    VERBATIM_FIELD_NUMBER: _ClassVar[int]
    GENE_FIELD_NUMBER: _ClassVar[int]
    DISPOSITION_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    OCCURRENCES_FIELD_NUMBER: _ClassVar[int]
    KNOWN_SPECIES_FIELD_NUMBER: _ClassVar[int]
    PRINTED_SPECIES_FIELD_NUMBER: _ClassVar[int]
    verbatim: str
    gene: str
    disposition: Disposition
    reason: str
    candidates: _containers.RepeatedCompositeFieldContainer[Candidate]
    occurrences: _containers.RepeatedCompositeFieldContainer[Occurrence]
    known_species: Species
    printed_species: str
    def __init__(self, verbatim: _Optional[str] = ..., gene: _Optional[str] = ..., disposition: _Optional[_Union[Disposition, str]] = ..., reason: _Optional[str] = ..., candidates: _Optional[_Iterable[_Union[Candidate, _Mapping]]] = ..., occurrences: _Optional[_Iterable[_Union[Occurrence, _Mapping]]] = ..., known_species: _Optional[_Union[Species, str]] = ..., printed_species: _Optional[str] = ...) -> None: ...

class Dismissal(_message.Message):
    __slots__ = ("text", "line", "reason")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    text: str
    line: int
    reason: str
    def __init__(self, text: _Optional[str] = ..., line: _Optional[int] = ..., reason: _Optional[str] = ...) -> None: ...

class MentionSet(_message.Message):
    __slots__ = ("references", "builds", "conventions", "mentions", "dismissals")
    REFERENCES_FIELD_NUMBER: _ClassVar[int]
    BUILDS_FIELD_NUMBER: _ClassVar[int]
    CONVENTIONS_FIELD_NUMBER: _ClassVar[int]
    MENTIONS_FIELD_NUMBER: _ClassVar[int]
    DISMISSALS_FIELD_NUMBER: _ClassVar[int]
    references: _containers.RepeatedCompositeFieldContainer[DeclaredReference]
    builds: _containers.RepeatedCompositeFieldContainer[DeclaredBuild]
    conventions: _containers.RepeatedCompositeFieldContainer[DeclaredConvention]
    mentions: _containers.RepeatedCompositeFieldContainer[Mention]
    dismissals: _containers.RepeatedCompositeFieldContainer[Dismissal]
    def __init__(self, references: _Optional[_Iterable[_Union[DeclaredReference, _Mapping]]] = ..., builds: _Optional[_Iterable[_Union[DeclaredBuild, _Mapping]]] = ..., conventions: _Optional[_Iterable[_Union[DeclaredConvention, _Mapping]]] = ..., mentions: _Optional[_Iterable[_Union[Mention, _Mapping]]] = ..., dismissals: _Optional[_Iterable[_Union[Dismissal, _Mapping]]] = ...) -> None: ...

class CandidateVerdict(_message.Message):
    __slots__ = ("outcome", "anchor", "detail")
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    ANCHOR_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    outcome: Outcome
    anchor: Anchor
    detail: str
    def __init__(self, outcome: _Optional[_Union[Outcome, str]] = ..., anchor: _Optional[_Union[Anchor, str]] = ..., detail: _Optional[str] = ...) -> None: ...

class MentionVerdict(_message.Message):
    __slots__ = ("candidates", "choice", "chosen", "nucleotide_vrs_id", "protein_vrs_id", "derived")
    CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    CHOICE_FIELD_NUMBER: _ClassVar[int]
    CHOSEN_FIELD_NUMBER: _ClassVar[int]
    NUCLEOTIDE_VRS_ID_FIELD_NUMBER: _ClassVar[int]
    PROTEIN_VRS_ID_FIELD_NUMBER: _ClassVar[int]
    DERIVED_FIELD_NUMBER: _ClassVar[int]
    candidates: _containers.RepeatedCompositeFieldContainer[CandidateVerdict]
    choice: Choice
    chosen: int
    nucleotide_vrs_id: str
    protein_vrs_id: str
    derived: _containers.RepeatedCompositeFieldContainer[Candidate]
    def __init__(self, candidates: _Optional[_Iterable[_Union[CandidateVerdict, _Mapping]]] = ..., choice: _Optional[_Union[Choice, str]] = ..., chosen: _Optional[int] = ..., nucleotide_vrs_id: _Optional[str] = ..., protein_vrs_id: _Optional[str] = ..., derived: _Optional[_Iterable[_Union[Candidate, _Mapping]]] = ...) -> None: ...

class Verdicts(_message.Message):
    __slots__ = ("mentions",)
    MENTIONS_FIELD_NUMBER: _ClassVar[int]
    mentions: _containers.RepeatedCompositeFieldContainer[MentionVerdict]
    def __init__(self, mentions: _Optional[_Iterable[_Union[MentionVerdict, _Mapping]]] = ...) -> None: ...
