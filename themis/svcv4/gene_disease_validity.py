"""Gene-disease validity classifications: the curators' vocabulary and the gate level each reads as.

SM18 states the gate and the mechanism precondition against the ClinGen gene-disease validity
framework's *levels*, which the contract spells as `themis.rpc.gene_disease_pb2.GateLevel` — the
values `GeneDisease.DescribeGene` states per curated entity, and what `reference.Reference.gate` is
keyed by and `scoring.apply_gate` takes. The curators publish *classifications*, and the two
vocabularies are not the same set: ClinGen's `No Known Disease Relationship`, `Disputed` and
`Refuted`, and GenCC's `Supportive`, `Disputed Evidence` and `Refuted Evidence`, are classifications
the enum names no member after.

So this module holds the one mapping between them, plus the strength order the classifications carry
across both curators (GenCC harmonises its submitters onto ClinGen's set and adds `Supportive` below
`Moderate`). It is the evidence service's ranking table as well as the gate's translation: a
classification that reaches either side unmapped raises here rather than being ranked or gated by a
guess.
"""

from __future__ import annotations

import dataclasses

from themis.rpc import gene_disease_pb2
from themis.svcv4 import reference


@dataclasses.dataclass(frozen=True)
class Classification:
    """One curator classification: where it ranks, and what the gate reads it as.

    Attributes:
        rank: Strength on the harmonised scale, higher is stronger. Comparable across curators;
            the absolute value carries no meaning.
        gate_level: The gate level, a key of `reference.Reference.gate`.
    """

    rank: int
    gate_level: gene_disease_pb2.GateLevel


# Strongest to weakest, each with the spellings the curators publish it under and the gate level it
# reads as. `No Known Disease Relationship` (no evidence found) ranks below the contradicted
# `Disputed`/`Refuted`; `Supportive` is GenCC-only and sits between `Moderate` and `Limited` (GenCC's
# own display order — its curie numbering does not track strength). GenCC spells the two contradicted
# ones with an ` Evidence` suffix, which is the same classification and ranks and gates alike.
_ORDER: tuple[tuple[tuple[str, ...], gene_disease_pb2.GateLevel], ...] = (
    (('Definitive',), gene_disease_pb2.GATE_LEVEL_DEFINITIVE),
    (('Strong',), gene_disease_pb2.GATE_LEVEL_STRONG),
    (('Moderate',), gene_disease_pb2.GATE_LEVEL_MODERATE),
    (('Supportive',), gene_disease_pb2.GATE_LEVEL_LIMITED),
    (('Limited',), gene_disease_pb2.GATE_LEVEL_LIMITED),
    (('Disputed', 'Disputed Evidence'), gene_disease_pb2.GATE_LEVEL_DISPUTED_OR_REFUTED),
    (('Refuted', 'Refuted Evidence'), gene_disease_pb2.GATE_LEVEL_DISPUTED_OR_REFUTED),
    (('No Known Disease Relationship',), gene_disease_pb2.GATE_LEVEL_LESS_THAN_LIMITED),
)

CLASSIFICATIONS: dict[str, Classification] = {
    spelling: Classification(rank=len(_ORDER) - position, gate_level=level)
    for position, (spellings, level) in enumerate(_ORDER)
    for spelling in spellings
}


def _classification(classification: str) -> Classification:
    try:
        return CLASSIFICATIONS[classification]
    except KeyError:
        raise ValueError(
            f'unknown gene-disease validity classification {classification!r}; the curated vocabulary '
            f'is {sorted(CLASSIFICATIONS)}'
        ) from None


def validate(classification: str) -> None:
    """Hold a curator's classification to the vocabulary this module ranks and gates.

    Args:
        classification: A ClinGen or GenCC classification, as the curator spells it.

    Raises:
        ValueError: If the classification is outside that vocabulary.
    """
    _classification(classification)


def rank(classification: str) -> int:
    """Strength of a curator's classification on the harmonised scale, higher being stronger.

    Args:
        classification: A ClinGen or GenCC classification, as the curator spells it.

    Returns:
        Its rank, comparable across curators.

    Raises:
        ValueError: If the classification is outside the curated vocabulary — a curator's scale has
            moved, which must surface rather than rank as weakest.
    """
    return _classification(classification).rank


def gate_level(classification: str) -> gene_disease_pb2.GateLevel:
    """The gate level a curator's classification reads as (SM18).

    Args:
        classification: A ClinGen or GenCC classification, as the curator spells it.

    Returns:
        The `GateLevel`, a key of `reference.Reference.gate` — what `scoring.apply_gate` and
        `classify.classify` take.

    Raises:
        ValueError: If the classification is outside the curated vocabulary.
    """
    return _classification(classification).gate_level


def check_gate_levels(ref: reference.Reference) -> None:
    """Fail loud if a classification maps onto a level the loaded reference does not gate.

    `scoring.apply_gate` raises at run time on a level the reference does not carry; this is the
    join between the two tables, which are edited independently — the reference transcribes the
    framework, this module the curators' vocabularies — and is what the library's tests hold.

    Args:
        ref: The loaded reference.

    Raises:
        reference.ReferenceDataError: If any mapped level is absent from `ref.gate`.
    """
    unknown = sorted(
        {reference.gate_level_name(c.gate_level) for c in CLASSIFICATIONS.values() if c.gate_level not in ref.gate}
    )
    if unknown:
        carried = [reference.gate_level_name(level) for level in sorted(ref.gate)]
        raise reference.ReferenceDataError(
            f'gene-disease validity classifications map onto gate level(s) {unknown}, which the '
            f'reference does not carry; its levels are {carried}'
        )
