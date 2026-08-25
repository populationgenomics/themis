"""The classification bands: the point interval each class occupies, and the VUS sub-bands.

The standard states a class as a printed interval ("> -1.0 to < +6.0"). The intervals are kept as
the standard prints them beside the band each is read as, and every class holds the two together at
import: the printed form is what a reader checks a boundary against, and the band is what
`scoring.band_for_total` decides with, so the two disagreeing would put a total in a class the
standard does not.
"""

from __future__ import annotations

import dataclasses
import decimal

from themis.svcv4 import reference


def _signed(points: decimal.Decimal) -> str:
    """A boundary as the standard prints it, which signs a positive value."""
    return f'+{points}' if points > 0 else str(points)


def _printed_range(band: reference.Band) -> str:
    """The band as a comparator clause per finite bound, joined the way the standard joins them."""
    clauses = []
    if band.lower is not None:
        clauses.append(f'{">=" if band.lower_inclusive else ">"} {_signed(band.lower)}')
    if band.upper is not None:
        clauses.append(f'{"<=" if band.upper_inclusive else "<"} {_signed(band.upper)}')
    return ' to '.join(clauses)


def _validate_printed_range(band: reference.Band, printed: str) -> None:
    rendered = _printed_range(band)
    if rendered != printed:
        raise reference.ReferenceDataError(
            f'the {band.code} band spans {rendered!r}, and the standard prints {printed!r}'
        )


@dataclasses.dataclass(frozen=True)
class ClassificationClass:
    """One of the five classes a total maps to.

    Attributes:
        band: The point interval the class occupies.
        printed_range: That interval as the standard prints it.
        label: The standard's name for the class.
        posterior_prob_pathogenic: The posterior probability of pathogenicity the class stands for.
    """

    band: reference.Band
    printed_range: str
    label: str
    posterior_prob_pathogenic: str

    def __post_init__(self) -> None:
        _validate_printed_range(self.band, self.printed_range)


@dataclasses.dataclass(frozen=True)
class VusSubclass:
    """One of the three sub-bands the standard divides VUS into.

    Attributes:
        band: The point interval the sub-band occupies.
        printed_range: That interval as the standard prints it.
        posterior_prob_pathogenic: The posterior probability of pathogenicity it stands for.
        meaning: Which of LB and LP the sub-band sits closer to.
    """

    band: reference.Band
    printed_range: str
    posterior_prob_pathogenic: str
    meaning: str

    def __post_init__(self) -> None:
        _validate_printed_range(self.band, self.printed_range)


CLASSES = (
    ClassificationClass(
        band=reference.Band(
            code='B',
            lower=None,
            lower_inclusive=False,
            upper=decimal.Decimal('-4.0'),
            upper_inclusive=True,
        ),
        printed_range='<= -4.0',
        label='Benign',
        posterior_prob_pathogenic='< 1%',
    ),
    ClassificationClass(
        band=reference.Band(
            code='LB',
            lower=decimal.Decimal('-4.0'),
            lower_inclusive=False,
            upper=decimal.Decimal('-1.0'),
            upper_inclusive=True,
        ),
        printed_range='> -4.0 to <= -1.0',
        label='Likely Benign',
        posterior_prob_pathogenic='1% to < 10%',
    ),
    ClassificationClass(
        band=reference.Band(
            code='VUS',
            lower=decimal.Decimal('-1.0'),
            lower_inclusive=False,
            upper=decimal.Decimal('6.0'),
            upper_inclusive=False,
        ),
        printed_range='> -1.0 to < +6.0',
        label='Variant of Uncertain Significance',
        posterior_prob_pathogenic='10% to < 90%',
    ),
    ClassificationClass(
        band=reference.Band(
            code='LP',
            lower=decimal.Decimal('6.0'),
            lower_inclusive=True,
            upper=decimal.Decimal('10.0'),
            upper_inclusive=False,
        ),
        printed_range='>= +6.0 to < +10.0',
        label='Likely Pathogenic',
        posterior_prob_pathogenic='> 90% to < 99%',
    ),
    ClassificationClass(
        band=reference.Band(
            code='P',
            lower=decimal.Decimal('10.0'),
            lower_inclusive=True,
            upper=None,
            upper_inclusive=False,
        ),
        printed_range='>= +10.0',
        label='Pathogenic',
        posterior_prob_pathogenic='> 99%',
    ),
)

VUS_SUBCLASSES = (
    VusSubclass(
        band=reference.Band(
            code='VUS-low',
            lower=decimal.Decimal('-1.0'),
            lower_inclusive=False,
            upper=decimal.Decimal('2.0'),
            upper_inclusive=False,
        ),
        printed_range='> -1.0 to < +2.0',
        posterior_prob_pathogenic='10% to < 34%',
        meaning='closer to LB than LP',
    ),
    VusSubclass(
        band=reference.Band(
            code='VUS-mid',
            lower=decimal.Decimal('2.0'),
            lower_inclusive=True,
            upper=decimal.Decimal('4.0'),
            upper_inclusive=False,
        ),
        printed_range='>= +2.0 to < +4.0',
        posterior_prob_pathogenic='34% to < 66%',
        meaning='equivocal between LB and LP',
    ),
    VusSubclass(
        band=reference.Band(
            code='VUS-high',
            lower=decimal.Decimal('4.0'),
            lower_inclusive=True,
            upper=decimal.Decimal('6.0'),
            upper_inclusive=False,
        ),
        printed_range='>= +4.0 to < +6.0',
        posterior_prob_pathogenic='66% to < 90%',
        meaning='closer to LP than LB',
    ),
)

BANDS = tuple(entry.band for entry in CLASSES)
VUS_SUBBANDS = tuple(entry.band for entry in VUS_SUBCLASSES)
CLASS_ORDER = tuple(entry.band.code for entry in CLASSES)

reference.validate_bands(BANDS, 'classification', full_line=True)
reference.validate_bands(VUS_SUBBANDS, 'classification.vus_subclasses', full_line=False)
reference.validate_subbands(VUS_SUBBANDS, reference.band_for(BANDS, 'VUS'), 'classification.vus_subclasses')
