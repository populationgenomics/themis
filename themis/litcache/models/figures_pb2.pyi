from buf.validate import validate_pb2 as _validate_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FigureKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FIGURE_KIND_UNSPECIFIED: _ClassVar[FigureKind]
    FIGURE_KIND_PEDIGREE: _ClassVar[FigureKind]
    FIGURE_KIND_CLINICAL_PHOTOGRAPH: _ClassVar[FigureKind]
    FIGURE_KIND_MEDICAL_IMAGING: _ClassVar[FigureKind]
    FIGURE_KIND_MICROSCOPY: _ClassVar[FigureKind]
    FIGURE_KIND_SEQUENCE_CHROMATOGRAM: _ClassVar[FigureKind]
    FIGURE_KIND_GEL_OR_BLOT: _ClassVar[FigureKind]
    FIGURE_KIND_SEQUENCE_ALIGNMENT: _ClassVar[FigureKind]
    FIGURE_KIND_GENE_OR_PROTEIN_SCHEMATIC: _ClassVar[FigureKind]
    FIGURE_KIND_PROTEIN_STRUCTURE: _ClassVar[FigureKind]
    FIGURE_KIND_CHART: _ClassVar[FigureKind]
    FIGURE_KIND_TABLE_IMAGE: _ClassVar[FigureKind]
    FIGURE_KIND_DIAGRAM: _ClassVar[FigureKind]
    FIGURE_KIND_OTHER: _ClassVar[FigureKind]
FIGURE_KIND_UNSPECIFIED: FigureKind
FIGURE_KIND_PEDIGREE: FigureKind
FIGURE_KIND_CLINICAL_PHOTOGRAPH: FigureKind
FIGURE_KIND_MEDICAL_IMAGING: FigureKind
FIGURE_KIND_MICROSCOPY: FigureKind
FIGURE_KIND_SEQUENCE_CHROMATOGRAM: FigureKind
FIGURE_KIND_GEL_OR_BLOT: FigureKind
FIGURE_KIND_SEQUENCE_ALIGNMENT: FigureKind
FIGURE_KIND_GENE_OR_PROTEIN_SCHEMATIC: FigureKind
FIGURE_KIND_PROTEIN_STRUCTURE: FigureKind
FIGURE_KIND_CHART: FigureKind
FIGURE_KIND_TABLE_IMAGE: FigureKind
FIGURE_KIND_DIAGRAM: FigureKind
FIGURE_KIND_OTHER: FigureKind

class FigureAnnotationConstruction(_message.Message):
    __slots__ = ("model", "harness_version")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    HARNESS_VERSION_FIELD_NUMBER: _ClassVar[int]
    model: str
    harness_version: str
    def __init__(self, model: _Optional[str] = ..., harness_version: _Optional[str] = ...) -> None: ...

class FigureSet(_message.Message):
    __slots__ = ("figures",)
    FIGURES_FIELD_NUMBER: _ClassVar[int]
    figures: _containers.RepeatedCompositeFieldContainer[Figure]
    def __init__(self, figures: _Optional[_Iterable[_Union[Figure, _Mapping]]] = ...) -> None: ...

class Figure(_message.Message):
    __slots__ = ("label", "legend", "panels")
    LABEL_FIELD_NUMBER: _ClassVar[int]
    LEGEND_FIELD_NUMBER: _ClassVar[int]
    PANELS_FIELD_NUMBER: _ClassVar[int]
    label: str
    legend: Place
    panels: _containers.RepeatedCompositeFieldContainer[Panel]
    def __init__(self, label: _Optional[str] = ..., legend: _Optional[_Union[Place, _Mapping]] = ..., panels: _Optional[_Iterable[_Union[Panel, _Mapping]]] = ...) -> None: ...

class Panel(_message.Message):
    __slots__ = ("label", "kinds", "place")
    LABEL_FIELD_NUMBER: _ClassVar[int]
    KINDS_FIELD_NUMBER: _ClassVar[int]
    PLACE_FIELD_NUMBER: _ClassVar[int]
    label: str
    kinds: _containers.RepeatedScalarFieldContainer[FigureKind]
    place: Place
    def __init__(self, label: _Optional[str] = ..., kinds: _Optional[_Iterable[_Union[FigureKind, str]]] = ..., place: _Optional[_Union[Place, _Mapping]] = ...) -> None: ...

class Place(_message.Message):
    __slots__ = ("pdf_page", "box")
    PDF_PAGE_FIELD_NUMBER: _ClassVar[int]
    BOX_FIELD_NUMBER: _ClassVar[int]
    pdf_page: int
    box: Box
    def __init__(self, pdf_page: _Optional[int] = ..., box: _Optional[_Union[Box, _Mapping]] = ...) -> None: ...

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
