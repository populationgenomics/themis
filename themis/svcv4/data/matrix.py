"""SM18's molecular-mechanism x exon-relevance matrix: what scales a positive predictive tier.

Each axis states a factor per level, and a cell is the product of the two — except one, and the
exception is a conflict inside SM18. Its narrative elects not to create fractions as small as the
product would give (12.5%), while its Figure 1 states 0% at the cell. The figure is used:
`OMITTED_CELL` names that cell, Suspected x Most, and `scoring` returns 0 for it rather than the
0.25 x 0.5 the axes would give. Naming the coordinate rather than describing the departure in prose
is what leaves nothing for a reader to apply the product to.

The levels are also the vocabulary a caller selects a multiplier with (`scoring.MechanismLevel`,
`scoring.ExonRelevance`), so each axis is held to the framework's levels for it at import.
"""

from __future__ import annotations

import dataclasses
import decimal

from themis.svcv4 import reference

APPLIES_TO = 'positive initial PRD points only'
FINAL_MULTIPLIER = 'mechanism_factor * exon_factor'

MOLECULAR_MECHANISM = {
    'Established': decimal.Decimal('1.0'),
    'Likely': decimal.Decimal('0.5'),
    'Suspected': decimal.Decimal('0.25'),
    'Unlikely': decimal.Decimal('0.0'),
    'Unknown': decimal.Decimal('0.0'),
    'Uncertain': decimal.Decimal('0.0'),
}

EXON_RELEVANCE = {
    'All': decimal.Decimal('1.0'),
    'Most': decimal.Decimal('0.5'),
    'Few': decimal.Decimal('0.0'),
}

OMITTED_CELL = ('Suspected', 'Most')


@dataclasses.dataclass(frozen=True)
class ReferenceData:
    """The transcript and expression resources SM18 names for placing a variant on either axis.

    Attributes:
        mane: The MANE release the exon axis is read against, and its size at the date given.
        tools: What SM18 names for judging how much of the transcript pool an exon reaches.
    """

    mane: str
    tools: tuple[str, ...]


REFERENCE_DATA = ReferenceData(
    mane='tark.ensembl.org - 19,253 MANE Select + 66 MANE Plus Clinical (as of 2026-06-01)',
    tools=(
        'gnomAD pext / Show Transcripts',
        'GTEx',
        'UCSC Determine Exon Relevance track (hg19/hg38)',
    ),
)

reference.validate_factor_axis(MOLECULAR_MECHANISM, reference.MECHANISM_LEVELS, 'the molecular-mechanism axis')
reference.validate_factor_axis(EXON_RELEVANCE, reference.EXON_LEVELS, 'the exon-relevance axis')
reference.validate_omitted_cell(OMITTED_CELL, reference.MECHANISM_LEVELS, reference.EXON_LEVELS)
