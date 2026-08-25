"""Missense predictor score to SVCv4 MIS_PRD bin (SM6 Figure 2).

SVCv4 requires the MIS_PRD score to come from ONE calibrated predictor chosen before the variant is
scored (SM6): evaluating several and taking the best is multiple-testing over correlated
metapredictors. What SM6 bans is choosing per VBC, not choosing per gene — it encourages a distinct
predictor for a specific gene "so long as [predictors] are selected in advance of the evaluation of
a given VBC" — so the choice is frozen policy (`predictor_policy`), not classification-time
judgement. This module enforces that at the API: one predictor and its one score go in, one bin
comes out. There is no entry point that accepts several predictors and picks a winner.

Scored here are the two predictors the policy names: BayesDel (the default; the score is
`BayesDel_noAF`, the build ClinGen calibrated) and AlphaMissense (PKD1). The other five have no
table here, for three different reasons. VEST4 has no lookup service — Ensembl's dbNSFP build
answers its column with "invalid_field". ESM1b's Figure 2 row cannot be transcribed at all: its -3.0
and -2.0 cells overlap at 8.8/8.9, ClinGen's own vci-v4 (cited at the BayesDel table below)
reproduces that overlap exactly, and two independent readings agreeing means the defect is the
figure's rather than a mis-reading — settling it needs the Working Group or ESM1b's published
calibration, not another attempt at the cell. VARITY_R's row prints 0.063 as the upper bound of the
-3.0 cell AND the lower bound of the -2.0 cell, which vci-v4 does settle: it states the -3.0 cell as
[0.037, 0.062], so 0.063 scores -2.0. REVEL and MutPred2 are served and their rows unambiguous; they
are simply not what the policy names.
"""

from __future__ import annotations

import dataclasses
import decimal
import enum
from collections.abc import Callable, Mapping

Score = decimal.Decimal | float | str
"""A predictor score as VEP serves it — a JSON number — or as an exact decimal string or `Decimal`."""

# A float carries at most 17 significant decimal digits; every published bin bound has six or fewer.
# Longer than this is the binary expansion of a float, not a score anyone quoted.
_MAX_SIGNIFICANT_DIGITS = 17


def _exact(score: Score) -> decimal.Decimal:
    """`score` as an exact decimal, a float read through its shortest round-trip repr.

    `decimal.Decimal(0.791)` is 0.791000000000000036…, which compares above the 0.791 bin bound, so
    converting a float directly bins a boundary score one step toward pathogenic — and does so at
    most of the bounds in the tables below. Going through `str` recovers the decimal the float was
    parsed from, which is the form the bounds are written in.

    A `Decimal` the caller built that way arrives carrying the expansion already, which is why an
    over-long one is refused rather than binned: it is the one form in which this error is silent.

    Raises:
        ValueError: If `score` is not a finite number, or carries more significant digits than a
            quoted score can.
    """
    if isinstance(score, decimal.Decimal):
        exact = score
    else:
        try:
            exact = decimal.Decimal(str(score))
        except decimal.InvalidOperation as e:
            raise ValueError(f'predictor score is not a number: {score!r}') from e
    if not exact.is_finite():
        raise ValueError(f'predictor score must be finite, got {score!r}')
    if len(exact.as_tuple().digits) > _MAX_SIGNIFICANT_DIGITS:
        raise ValueError(
            f'predictor score carries {len(exact.as_tuple().digits)} significant digits, over the '
            f'{_MAX_SIGNIFICANT_DIGITS} a score can be quoted to: {score!r}. A Decimal built from a float '
            'reads this way, and binning it puts a bin-bound score one step high; pass the float itself.'
        )
    return exact


class Predictor(enum.Enum):
    """The seven SVCv4-calibrated missense predictors (SM6). Only the policy's are scored."""

    ALPHAMISSENSE = 'AlphaMissense'
    BAYESDEL = 'BayesDel'
    ESM1B = 'ESM1b'
    MUTPRED2 = 'MutPred2'
    REVEL = 'REVEL'
    VARITY_R = 'VARITY_R'
    VEST4 = 'VEST4'


@dataclasses.dataclass(frozen=True)
class _Row:
    """One predictor's SM6 Figure 2 row: the range its scores live on, and the bins over it.

    `thresholds` pair each bin's INCLUSIVE upper bound with that bin's points, ascending; `top` is
    what a score above the last bound earns. The figure prints the cells with a rounding gap between
    adjacent ones (AlphaMissense's 0.070 / 0.071), closed here to the lower bin's upper bound — so
    every score in [low, high] maps to exactly one bin, and a score inside a gap takes the higher
    bin.
    """

    score: str
    low: decimal.Decimal
    high: decimal.Decimal
    thresholds: tuple[tuple[decimal.Decimal, decimal.Decimal], ...]
    top: decimal.Decimal

    def points(self, score: Score) -> decimal.Decimal:
        """The bin `score` falls in.

        Raises:
            ValueError: If `score` is not a finite number, or is outside the predictor's range —
                which is a score on another predictor's scale, not a low or high one.
        """
        exact = _exact(score)
        if not self.low <= exact <= self.high:
            raise ValueError(f'{self.score} score must be in [{self.low}, {self.high}], got {score}')
        for upper, points in self.thresholds:
            if exact <= upper:
                return points
        return self.top


# AlphaMissense reaches -3.0 (not -4.0); `am_pathogenicity` is a probability, hence the [0, 1] range.
_ALPHAMISSENSE = _Row(
    score='AlphaMissense',
    low=decimal.Decimal('0'),
    high=decimal.Decimal('1'),
    thresholds=(
        (decimal.Decimal('0.070'), decimal.Decimal('-3.0')),
        (decimal.Decimal('0.099'), decimal.Decimal('-2.0')),
        (decimal.Decimal('0.169'), decimal.Decimal('-1.0')),
        (decimal.Decimal('0.791'), decimal.Decimal('0.0')),
        (decimal.Decimal('0.905'), decimal.Decimal('1.0')),
        (decimal.Decimal('0.971'), decimal.Decimal('2.0')),
        (decimal.Decimal('0.989'), decimal.Decimal('3.0')),
    ),
    top=decimal.Decimal('4.0'),
)

# BayesDel reaches -3.0 (not -4.0). The range is the one BayesDel's author publishes for the score
# (-1.29334 to 0.75731) — not a probability, so the guard is not [0, 1].
#
# The -2.0 cell is printed "-0.0519 to -0.360". -0.0519 sits above the -1.0 cell's lower bound
# (-0.359), so the row cannot be read in the order every other cell runs in. -0.519 is both the only
# monotone reading and what ClinGen's own implementation carries: ClinGen/vci-v4 (MIT),
# frontend/src/contexts/ScoringContext.tsx, states this row's -2.0 bin as [-0.519, -0.36], and every
# other bound below matches that table too. The two rounding gaps the row leaves (-0.520 / -0.519
# and +0.499 / +0.50) are ours to close, the same way AlphaMissense's are — to the lower bin's upper
# bound; vci-v4 leaves a score inside either gap matching no bin at all.
_BAYESDEL = _Row(
    score='BayesDel_noAF',
    low=decimal.Decimal('-1.29334'),
    high=decimal.Decimal('0.75731'),
    thresholds=(
        (decimal.Decimal('-0.520'), decimal.Decimal('-3.0')),
        (decimal.Decimal('-0.360'), decimal.Decimal('-2.0')),
        (decimal.Decimal('-0.180'), decimal.Decimal('-1.0')),
        (decimal.Decimal('0.129'), decimal.Decimal('0.0')),
        (decimal.Decimal('0.269'), decimal.Decimal('1.0')),
        (decimal.Decimal('0.409'), decimal.Decimal('2.0')),
        (decimal.Decimal('0.499'), decimal.Decimal('3.0')),
    ),
    top=decimal.Decimal('4.0'),
)


def alphamissense_points(score: Score) -> decimal.Decimal:
    """Map an AlphaMissense pathogenicity score to MIS_PRD points (-3.0 to +4.0; SM6 Figure 2).

    Args:
        score: The AlphaMissense `am_pathogenicity` score, in [0, 1]. Pass the upstream's number as
            it comes; a float is converted exactly.

    Returns:
        The MIS_PRD initial points (before the exon-relevance matrix).

    Raises:
        ValueError: If `score` is not a finite number, or is outside [0, 1].
    """
    return _ALPHAMISSENSE.points(score)


def bayesdel_points(score: Score) -> decimal.Decimal:
    """Map a BayesDel score to MIS_PRD points (-3.0 to +4.0; SM6 Figure 2).

    Args:
        score: The `BayesDel_noAF` score, in [-1.29334, 0.75731]. The no-allele-frequency build is
            the one ClinGen calibrated, and the only one with a GRCh38 release; an `addAF` score is
            on a different scale and this table does not apply to it. Pass the upstream's number as
            it comes; a float is converted exactly.

    Returns:
        The MIS_PRD initial points (before the exon-relevance matrix).

    Raises:
        ValueError: If `score` is not a finite number, or is outside BayesDel's published range.
    """
    return _BAYESDEL.points(score)


_POINTS: Mapping[Predictor, Callable[[Score], decimal.Decimal]] = {
    Predictor.ALPHAMISSENSE: alphamissense_points,
    Predictor.BAYESDEL: bayesdel_points,
}


# The key each predictor's score arrives under on one of VEP's transcript consequences. Two wire
# forms end at one key here: AlphaMissense is a first-class VEP field, and BayesDel reaches VEP
# through its dbNSFP plugin, whose per-transcript dotted string `Vep.Annotate` has already resolved
# to one value per transcript.
_SCORE_KEYS: Mapping[Predictor, str] = {
    Predictor.ALPHAMISSENSE: 'am_pathogenicity',
    Predictor.BAYESDEL: 'BayesDel_noAF_score',
}


def score_key(predictor: Predictor) -> str:
    """The key `Vep.Annotate` serves this predictor's score under, on a transcript consequence.

    Args:
        predictor: The pre-selected calibrated predictor.

    Returns:
        The key to read the score at.

    Raises:
        NotImplementedError: For a predictor with no threshold table here, whose score could then be
            read and not binned.
    """
    try:
        return _SCORE_KEYS[predictor]
    except KeyError as e:
        raise NotImplementedError(f'no key is recorded for {predictor.value}, whose score this build cannot bin') from e


def implements(predictor: Predictor) -> bool:
    """Whether this module holds `predictor`'s SM6 threshold table, so a score for it can be binned.

    What `predictor_policy` checks each of its entries against at load: a policy naming a predictor
    with no table here is a gene that would fail on the variant it was written for.
    """
    return predictor in _POINTS


def predictor_points(predictor: Predictor, score: Score) -> decimal.Decimal:
    """Map the pre-selected predictor's score to MIS_PRD points.

    The single-predictor-per-gene rule is enforced by this signature: exactly one predictor and its
    score, never a panel. Which predictor is pre-selected is `predictor_policy`'s answer, not the
    caller's.

    Args:
        predictor: The pre-selected calibrated predictor.
        score: That predictor's score for the variant, as the upstream serves it. A float is
            converted exactly, so a score on a bin bound bins where the table says.

    Returns:
        The MIS_PRD initial points.

    Raises:
        NotImplementedError: For a predictor whose SVCv4 threshold table is not implemented here.
        ValueError: If `score` is not a finite number, or is outside that predictor's range.
    """
    try:
        table = _POINTS[predictor]
    except KeyError as e:
        raise NotImplementedError(
            f'{predictor.value} threshold table not implemented (verify calibration before use)'
        ) from e
    return table(score)
