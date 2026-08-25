"""The Tavtigian odds-to-points calibration: what an OddsPath ratio is worth in points.

The framework prints each step as a ratio and v4 scores in points, so each level carries both: the
ratio as printed, and the scalar it divides out to, which is what an assay's deposited OddsPath is
compared against. The scalar is derived from the printed ratio rather than stated beside it — two
statements of one number drift, and a benign ratio's scalar is a repeating decimal no reader checks
by eye. What the two say has to agree in direction, which `validate_oddspath` holds them to.
"""

from __future__ import annotations

import decimal

from themis.svcv4 import reference

# The context a derived scalar is divided in, fixed here rather than inherited from the caller's:
# an OddsPath comparison has to give the same answer whatever precision, rounding or traps the
# process was left in. A fresh context is 28 digits, round-half-even, and traps nothing but errors.
_ODDS_CONTEXT = decimal.Context(prec=28)

# What the framework prints for the step it states no ratio for.
_NO_RATIO = '-'


def _odds(odds_path: str) -> decimal.Decimal | None:
    """The scalar a printed OddsPath ratio divides out to.

    Args:
        odds_path: The ratio as the framework prints it, "18.7:1" pathogenic or "1:2.08" benign, or
            `-` for the step it prints no ratio for.

    Returns:
        The ratio as a scalar, or None where the framework prints none.

    Raises:
        ReferenceDataError: If the ratio is neither `-` nor two numbers around one colon. Anything
            else read as "no ratio" would be a step `functional.oddspath_points` skips, scoring the
            weight of the step below it instead.
    """
    if odds_path == _NO_RATIO:
        return None
    sides = odds_path.split(':')
    if len(sides) != 2:
        raise _malformed(odds_path)
    try:
        pathogenic, benign = (decimal.Decimal(side) for side in sides)
    except decimal.InvalidOperation as e:
        raise _malformed(odds_path) from e
    return _ODDS_CONTEXT.divide(pathogenic, benign)


def _malformed(odds_path: str) -> reference.ReferenceDataError:
    """The refusal for a ratio that is neither two numbers around one colon nor the no-ratio mark."""
    return reference.ReferenceDataError(
        f'OddsPath {odds_path!r} is neither two numbers around one colon nor {_NO_RATIO!r}, which is what the '
        'framework prints for the step it states no ratio for'
    )


def _level(strength: str, odds_path: str, points: str) -> reference.OddsPathLevel:
    """One calibration step, its scalar divided out of the ratio the framework prints.

    Args:
        strength: The strength label the ratio calibrates.
        odds_path: The ratio as printed.
        points: The points the step is worth.

    Returns:
        The level.
    """
    return reference.OddsPathLevel(
        strength=strength, odds_path=odds_path, odds=_odds(odds_path), points=decimal.Decimal(points)
    )


SCALE = (
    _level('Benign-Strong', '1:18.7', '-4'),
    _level('Benign-Supporting', '1:2.08', '-1'),
    _level('Indeterminate', _NO_RATIO, '0'),
    _level('Pathogenic-Supporting', '2.08:1', '1'),
    _level('Pathogenic-Moderate', '4.33:1', '2'),
    _level('Pathogenic-Strong', '18.7:1', '4'),
    _level('Pathogenic-Very-Strong', '350:1', '8'),
)

NOTES = (
    'v4 uses a continuous point scale rather than discrete strength buckets. Benign boundary set at 1% posterior '
    'probability (v3 Bayesian assumed 0.1%).\n'
)

reference.validate_oddspath(SCALE)
