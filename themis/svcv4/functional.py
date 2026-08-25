"""Functional-assay evidence: an assay's result to FXN points, by each of SM20's routes.

Functional evidence (^^^_FXN) is calibrated by the Brnich OddsPath / likelihood-ratio method
(PMID 31892348; Tavtigian PMID 29300386), which requires both pathogenic and benign controls. Three
routes reach points from there, and which one applies is a property of the experiment rather than a
choice: a **deposited calibration** carries its own OddsPath, which `oddspath_points` bins on the
Tavtigian scale and `fxn_from_mavedb` reads off a MaveDB deposit; a **small experiment** with no
false calls is scored straight off SM20's control-count grids (`fxn_from_controls`), which state the
evidence strength for a count of pathogenic and benign controls without an OddsPath in between; and
an **animal model** is scored off SM20 Table 3 (`animal_model_points`). An experiment outside all
three — false calls, a trichotomised readout, a MAVE — is calibrated mathematically, which SM20 puts
beyond its own scope and this module refuses rather than approximates.

What stays the model's is the judgement SM20 puts ahead of every route: whether the assay measures
the function the MDE's mechanism runs through. It arrives as an answer rather than being inferred,
and a False one scores 0.0 with the judgement named — distinct from FXN_ND, which is data nothing
calibrates and no determination at all.
"""

from __future__ import annotations

import dataclasses
import decimal
import enum

from themis.rpc import mavedb_pb2
from themis.svcv4 import provenance, reference

_ANIMAL_MODEL_RANGE = (decimal.Decimal(0), decimal.Decimal(4))


class PhenotypicConsistency(enum.Enum):
    """Animal-model phenotype match to the human MDE (SM20 Table 3)."""

    HIGH = 'high'  # high replication of the human phenotype
    KEY_FEATURES = 'key_features'  # key features replicated
    SIMILAR = 'similar'  # similar phenotype
    NONE = 'none'  # no phenotypic consistency


def oddspath_points(ref: reference.Reference, odds_path: decimal.Decimal) -> decimal.Decimal:
    """Map an OddsPath value to FXN points via the Tavtigian calibration (SM20).

    An OddsPath above 1 favours pathogenicity, below 1 favours benignity; the value selects the
    strongest calibration step it reaches (e.g. >= 18.7:1 -> +4.0, <= 1:18.7 -> -4.0).

    Args:
        ref: The loaded reference (supplies the calibration scale).
        odds_path: The computed OddsPath (a positive likelihood ratio).

    Returns:
        The FXN points (0.0 in the indeterminate middle). The caller applies the path's FXN cap.

    Raises:
        ValueError: If `odds_path` is not positive.
    """
    if odds_path <= 0:
        raise ValueError(f'OddsPath must be positive, got {odds_path}')
    pathogenic = sorted(((s.odds, s.points) for s in ref.oddspath if s.odds is not None and s.odds > 1), reverse=True)
    benign = sorted((s.odds, s.points) for s in ref.oddspath if s.odds is not None and s.odds < 1)
    if odds_path >= 1:
        for threshold, points in pathogenic:
            if odds_path >= threshold:
                return points
        return decimal.Decimal(0)
    for threshold, points in benign:
        if odds_path <= threshold:
            return points
    return decimal.Decimal(0)


def animal_model_points(
    phenotypic_consistency: PhenotypicConsistency,
    *,
    same_inheritance: bool,
    high_protein_similarity: bool,
) -> decimal.Decimal:
    """Score a genetically-engineered (knock-in) animal model (SM20 Table 3; 0.0 to +4.0).

    High phenotype replication with the same inheritance and high protein similarity is +4.0;
    differing inheritance drops it one level; key-features-only drops it further; anything without
    high protein similarity or with no phenotypic consistency is 0.0.

    Args:
        phenotypic_consistency: How closely the model's phenotype matches the human MDE.
        same_inheritance: Whether the model's inheritance pattern matches the human MDE.
        high_protein_similarity: Whether protein-level similarity local to the variant is high.

    Returns:
        The animal-model FXN points (0.0 to +4.0).
    """
    if not high_protein_similarity or phenotypic_consistency is PhenotypicConsistency.NONE:
        return decimal.Decimal(0)
    table = {
        (PhenotypicConsistency.HIGH, True): decimal.Decimal(4),
        (PhenotypicConsistency.HIGH, False): decimal.Decimal(3),
        (PhenotypicConsistency.KEY_FEATURES, True): decimal.Decimal(2),
        (PhenotypicConsistency.KEY_FEATURES, False): decimal.Decimal(1),
        (PhenotypicConsistency.SIMILAR, True): decimal.Decimal(1),
        (PhenotypicConsistency.SIMILAR, False): decimal.Decimal(0),
    }
    return table[(phenotypic_consistency, same_inheritance)]


class FxnSupport(enum.Enum):
    """What a `^^^_FXN` finding rests on — SM20's three outcomes for an assay."""

    CALIBRATED = 'calibrated'  # a calibration produced the points: an OddsPath, or the control grids
    NOT_CONCORDANT = 'not_concordant'  # the assay does not measure the mechanism's function: FXN_0.0
    NO_CALIBRATION = 'no_calibration'  # data exist that nothing here calibrates: FXN_ND, no points


@dataclasses.dataclass(frozen=True)
class Fxn:
    """The functional-assay finding: the points where one was calibrated, and what they rest on.

    A `classify.ScoredCode` whose code is family-agnostic, because SM20's is: the assay scores
    `^^^_FXN`, and which family it lands in — `MIS_`, `NUL_`, `CDS_`, `SPL_` — is decided by the path
    the points are handed to, not by the assay.

    An unmeasured assay and a non-concordant one are not the same finding and must not arrive as the
    same number: SM20 scores an assay that does not measure the mechanism's function at 0.0, a
    determination, and one nothing calibrates at FXN_ND, which is no determination at all.

    Attributes:
        points: The FXN points, or None under `NO_CALIBRATION`.
        support: Which of SM20's outcomes produced them.
        derivation: The calibration and the inputs behind it, for the audit trail.
        releases: The releases behind the deposit the points were read from; empty where the caller
            supplied the counts itself.
    """

    points: decimal.Decimal | None
    support: FxnSupport
    derivation: str
    releases: tuple[provenance.Release, ...] = ()

    def __post_init__(self) -> None:
        if (self.points is None) is not (self.support is FxnSupport.NO_CALIBRATION):
            raise ValueError(f'a {self.support.value} FXN carries points iff something calibrated it')

    @property
    def code(self) -> str:
        """SM20's own family-agnostic spelling; a path fixes the family when it takes the points.

        Deliberately not a code the reference carries: `MIS_FXN` and `NUL_FXN` are the same assay
        read on two paths, and naming one here would be a claim the assay does not make.
        """
        return '^^^_FXN'


def _not_concordant(what: str) -> Fxn:
    return Fxn(
        points=decimal.Decimal(0),
        support=FxnSupport.NOT_CONCORDANT,
        derivation=f"{what}, judged not to measure the function the MDE's mechanism runs through (SM20)",
    )


def fxn_from_mavedb(
    ref: reference.Reference,
    response: mavedb_pb2.DescribeVariantResponse,
    *,
    measures_disease_relevant_function: bool,
) -> Fxn:
    """Read the `^^^_FXN` points off a `MaveDb.DescribeVariant` deposit.

    Reads the depositor's own calibration off the typed fields — `oddspath_ratio`, and
    `acmg_criterion` / `acmg_strength` for what it asserts — and nothing out of `raw`, which carries
    the score set the analyst reads to judge what the assay measured.

    Two of SM20's three outcomes are decided here and the third is the caller's. Whether the assay
    measures the function the MDE's mechanism runs through is a judgement no field settles — two
    deposits can both be right about different questions — so it arrives as an answer, and a False
    one scores 0.0 with the judgement named rather than removing the code silently. Where the
    depositor deposited no calibration there is no OddsPath to bin, which is FXN_ND: MaveDB runs no
    Brnich calibration and exposes no control counts, so nothing here can compute one.

    Args:
        ref: The loaded reference (supplies the Tavtigian calibration scale).
        response: The rpc's answer. MaveDB holding no deposit at all does not arrive here — that is
            the rpc's NOT_FOUND, and it removes the code rather than setting a value.
        measures_disease_relevant_function: Whether the assay measures the disease-relevant function
            and agrees with the mechanism.

    Returns:
        The `Fxn`, stamped with the releases the response names.

    Raises:
        ValueError: If the deposited OddsPath is not positive, or the response states no provenance.
    """
    releases = provenance.releases_of(response.provenance)
    if not measures_disease_relevant_function:
        return dataclasses.replace(_not_concordant('the deposited assay'), releases=releases)
    if not response.HasField('oddspath_ratio'):
        return Fxn(
            points=None,
            support=FxnSupport.NO_CALIBRATION,
            derivation='the deposit carries no calibration for its score, so no OddsPath is derivable (FXN_ND)',
            releases=releases,
        )
    odds_path = decimal.Decimal(str(response.oddspath_ratio))
    criterion = ' '.join(part for part in (response.acmg_criterion, response.acmg_strength) if part)
    asserted = f', asserting {criterion}' if criterion else ''
    return Fxn(
        points=oddspath_points(ref, odds_path),
        support=FxnSupport.CALIBRATED,
        derivation=f"the depositor's OddsPath {odds_path}{asserted}, on the Tavtigian calibration",
        releases=releases,
    )


class ControlRange(enum.Enum):
    """Which of an experiment's control ranges the test variant's own result falls in (SM20)."""

    PATHOGENIC = 'pathogenic'  # SM20 Table 1, awarding pathogenicity
    BENIGN = 'benign'  # SM20 Table 2, awarding benignity


def fxn_from_controls(
    ref: reference.Reference,
    *,
    result_range: ControlRange,
    pathogenic_controls: int,
    benign_controls: int,
    measures_disease_relevant_function: bool,
    no_false_calls: bool,
) -> Fxn:
    """Read FXN points off SM20's control-count lookup tables (Tables 1-2).

    The route for an experiment too small to calibrate mathematically: SM20 states the evidence
    strength directly, as a grid over how many pathogenic and how many benign controls the
    experiment used. The asymmetry the grids encode is SM20's own — the benign controls drive the
    strength available for *pathogenicity* and the pathogenic controls that for *benignity* — so
    which table applies is decided by which control range the test variant's result falls in, never
    by which direction the analyst expects.

    Both of the tables' preconditions are required rather than assumed. An assay that does not
    measure the function the mechanism runs through scores 0.0 (SM20's second decision point), and an
    experiment with false positives or false negatives is outside these grids entirely: SM20 sends it
    to a mathematical calibration, so it is refused here rather than scored off a table that does not
    cover it.

    Args:
        ref: The loaded reference (supplies the transcribed grids).
        result_range: Which control range the test variant's result falls in.
        pathogenic_controls: How many pathogenic control variants the experiment used.
        benign_controls: How many benign controls it used. A single wild-type reference is not one,
            and the grids state that themselves: a count of one awards nothing in the direction that
            count drives.
        measures_disease_relevant_function: Whether the assay measures the disease-relevant function
            and agrees with the mechanism.
        no_false_calls: Whether the experiment produced no false positives and no false negatives.

    Returns:
        The `Fxn`, its derivation naming the table and the cell read.

    Raises:
        ValueError: If the experiment carries false calls, or a control count is negative or past
            the extent of SM20's grids — both are experiments the tables do not cover, and clamping
            one onto the last row would award the strongest evidence the small-experiment regime has.
    """
    if not measures_disease_relevant_function:
        return _not_concordant(f'the {pathogenic_controls}P / {benign_controls}B control experiment')
    if not no_false_calls:
        raise ValueError(
            'SM20 Tables 1-2 cover small experiments with no false positives and no false negatives; '
            'an experiment with either is calibrated mathematically instead (SM20 §22)'
        )
    grid = ref.control_count_grid(result_range.value)
    points = grid.cells.get((benign_controls, pathogenic_controls))
    if points is None:
        extent = max(benign for benign, _ in grid.cells)
        raise ValueError(
            f'SM20 Table {grid.number} states cells for 0 to {extent} controls of each kind; '
            f'{pathogenic_controls} pathogenic and {benign_controls} benign reach no cell of it, and an '
            'experiment past its extent is calibrated mathematically (SM20 §22)'
        )
    return Fxn(
        points=points,
        support=FxnSupport.CALIBRATED,
        derivation=(
            f'SM20 Table {grid.number} ({grid.direction}): the test result falls in the '
            f'{result_range.value} control range of an experiment with {pathogenic_controls} pathogenic '
            f'and {benign_controls} benign controls'
        ),
    )
