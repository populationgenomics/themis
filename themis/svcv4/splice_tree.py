"""The splice (`SPL_`) decision tree: the score trichotomy it enters on, and each cell's own bounds.

Three workflows can produce a splice tier — canonical splice (SM11), missense (SM6) and
intronic/synonymous (SM12) — and each colour path within them bounds `SPL_PRD`, `SPL_SPA` and the
two combine caps differently. `data/svcv4_scoring_reference.json` carries only the union over all of
them (`SPL_PRD` [-1.0, 6.0], `SPL_PRD_SPA` [-3.0, 6.0]), so a canonical +6.0 NMD tier passes its
validation on a predicted-splice path whose NMD tier is +3.0. This module holds the cell structure
the reference does not encode; the workflow transcriptions `meta.cited_documents` pins are its
authority, not the reference.

The flow axis has two members, not three. `CANONICAL` is SM11, entered only where the wild-type
dinucleotide is GT at +1,+2 or AG at -2,-1: disruption follows from the position, the NMD tier
starts at +6.0, and the splice assay can only walk the tier back. `PREDICTED` is SM6 and SM12, which
reach a splice effect through an in-silico prediction: the NMD tier starts at +3.0 and the assay
amplifies it. SM6 and SM12 agree in every bound below once the first two conflicts are resolved.

Framework conflicts resolved here:

  1. Orange (`SPL_PRD` + `SPL_SPA`) floor. SM12 §2c reads -1.0; SM11 §2c and SM6 (upper-orange
     step 2) read 0.0. -1.0 is used: a 0.0 floor erases the -1.0 alternate-in-frame-start tier the
     same path awards two steps earlier.
  2. Violet (`SPL_PRD` + `SPL_SPA`) + `SPL_FXN` ceiling. SM11 §5c and SM12 §5c read 0.0 (upper end
     capped at 0 for discordance risk); SM6's violet step 3 quotes the generic module's +9.0 while
     recording that its own diagram box reads 0.0. 0.0 is used.
  3. Blue (`SPL_PRD` + `SPL_SPA`) + `SPL_FXN` ceiling. SM11's diagram box reads +9.0 against its
     supplement's +8.0, and SM11's discrepancy note rules +8.0 authoritative, reading the +9.0 as a
     carry-over from the yellow/orange rows. SM12 §4c and SM6 (blue step 3) read +9.0. +8.0 is used
     on both flows: SM11 §4e and SM12 §4e cap the blue `SPL_` parent at +8.0 and SM6 (blue step 5)
     at 0.0, so no tree admits +9.0 as a blue *output*, and SM11's carry-over reading applies to the
     other two verbatim.

Two readings taken without a conflict entry, because one tree is softer rather than different: the
predicted flow's incomplete-assay award, where SM6 (yellow step 2) reads "+0 (reduce PRD to 0 /
reconsider evidence)" and SM12 §1c/§2c the softer "+0 (may need to reconsider PRD points)" — the
softer reading is scored, so an incomplete assay leaves `SPL_PRD` standing; and the violet middle
bucket, which SM11/SM12 §5b label "only low-level" where SM6 pairs it "low-level/substantial".

The entry to all of this is a splice-predictor score, and `entry_tier` bins it: the SVI trichotomy
the three flows state identically, over SpliceAI's stronger delta. What the tier is not is the
colour — every flow asks a second question about the predicted product, and that answer selects the
path — so the binning is the library's and the colour stays the analyst's.

Not modelled, and the caller's to make before choosing a label: the concordance diamond SM11 §1c/§2c
and SM12 §1c/§2c put ahead of the proportion question on the scaled colours ("are splicing data and
`SPL_PRD` concordant? NO -> reconsider evidence"). Only violet's contradicted reading is a value in
this module's vocabulary.

The violet `SPL_INF` bound is [-8.0, 0.0] on the strength of SM11 §5d, SM12 §5d and SM6 (violet step
4) alike: that path's informative-variant module scores B/LB only, and routes a P/LP variant of the
same predicted impact to a re-evaluation rather than to points. SM12's discrepancy note 2 concerns
whether the module applies on that path at all — §103 says informative variants are not considered,
against §117 and the diagram, which define it — and resolves itself in favour of applying it, so
nothing about the bound was left open.

Left on the union: the per-colour `SPL_` parent cap, against the `SPL_PFD` [-8.0, 10.0] the builder
bakes — that one needs the blue and violet parent ranges settled first, and the three trees do not
agree on either.
"""

from __future__ import annotations

import dataclasses
import decimal
import enum
from collections.abc import Mapping

from themis.rpc import splice_pb2
from themis.svcv4 import provenance, reference, scoring

_ZERO = decimal.Decimal(0)


class SpliceFlow(enum.Enum):
    """Which workflow produced the splice tier, and so which direction the splice assay runs."""

    CANONICAL = 'canonical'
    PREDICTED = 'predicted'


class SpliceColour(enum.Enum):
    """The colour path the splice-predictor score and the predicted consequence select."""

    YELLOW = 'yellow'  # splice likely, frameshift and NMD predicted
    ORANGE = 'orange'  # splice likely, NMD not predicted; upper and lower orange share every bound
    BLUE = 'blue'  # splice impact uncertain
    VIOLET = 'violet'  # splice impact unlikely; SM12 names this path lilac


class Proportion(enum.Enum):
    """The proportion of alternative transcripts an RNA splice assay shows (yellow/orange/violet).

    SM6's violet step 2 pairs "low-level/substantial" against the same 0.0 award SM11/SM12 give
    "only low-level", which is what lets the violet path reuse these three labels.
    """

    NEAR_TO_COMPLETE = 'near to complete'
    SUBSTANTIAL = 'substantial'
    INCOMPLETE = 'incomplete'


class AssayStrength(enum.Enum):
    """How strongly a splice assay evidences a disruptive effect; the blue path scores this directly."""

    CLEAR_DISRUPTION = 'clear evidence of a disruptive splice effect'
    SOME_DISRUPTION = 'some evidence of a disruptive splice effect'
    UNCONVINCING = 'unconvincing evidence of a splice effect'
    SOME_NO_EFFECT = 'some evidence of no splice effect'
    CONVINCING_NO_EFFECT = 'convincing evidence of no splice effect'


class ReconsiderEvidenceError(Exception):
    """The assay contradicts the colour path: the routing is wrong and must be redone, not scored."""


def _check_total(covered: frozenset[Proportion] | frozenset[AssayStrength], vocabulary: type[enum.Enum]) -> None:
    """Fail loud on an assay rule that leaves one of its vocabulary's readings unscored."""
    missing = set(vocabulary) - set(covered)
    if missing:
        raise ValueError(f'assay rule scores no outcome for {sorted(m.name for m in missing)}')


@dataclasses.dataclass(frozen=True)
class ScaledAssay:
    """`SPL_SPA` is a fraction of the matrix-adjusted *positive* `SPL_PRD` (yellow, orange).

    The fractions are signed by flow: canonical reduces the tier, predicted amplifies it. Taking the
    fraction of the negative alternate-in-frame-start tier would put `SPL_SPA` outside its own
    declared range on both flows, which is what fixes the base to `max(adjusted, 0)`.
    """

    factors: Mapping[Proportion, decimal.Decimal]

    def __post_init__(self) -> None:
        _check_total(frozenset(self.factors), Proportion)


@dataclasses.dataclass(frozen=True)
class FixedAssay:
    """`SPL_SPA` is a fixed award per proportion label (violet); `reconsider` labels are terminal."""

    points: Mapping[Proportion, decimal.Decimal]
    reconsider: frozenset[Proportion]

    def __post_init__(self) -> None:
        _check_total(frozenset(self.points) | self.reconsider, Proportion)


@dataclasses.dataclass(frozen=True)
class StrengthAssay:
    """`SPL_SPA` scores the assay's own strength rather than a proportion of `SPL_PRD` (blue)."""

    points: Mapping[AssayStrength, decimal.Decimal]

    def __post_init__(self) -> None:
        _check_total(frozenset(self.points), AssayStrength)


AssayRule = ScaledAssay | FixedAssay | StrengthAssay


@dataclasses.dataclass(frozen=True)
class SpliceCell:
    """One flow x colour cell of the splice trees: what it admits and how its assay scores.

    Attributes:
        prd: The initial `SPL_PRD` tier range, pre-matrix.
        spa: The `SPL_SPA` range. Nothing validates against it directly — `spa_bounds` derives the
            operative bound from `assay` — so it stands as an independent transcription of the same
            fact, and the tests hold the two to each other.
        prd_plus_spa: The cap on the first combine layer.
        fxn: The `SPL_FXN` code range, which the violet path bounds below the generic module's.
        plus_fxn: The cap on the second combine layer, `(SPL_PRD + SPL_SPA) + SPL_FXN`.
        inf: The `SPL_INF` range; the violet path's module scores B/LB only, so it cannot go
            positive there.
        scaling: Which matrix axes scale positive `SPL_PRD` on this path.
        assay: How an analyst's splice-assay judgement becomes `SPL_SPA` points.
    """

    prd: reference.CapRange
    spa: reference.CapRange
    prd_plus_spa: reference.CapRange
    fxn: reference.CapRange
    plus_fxn: reference.CapRange
    inf: reference.CapRange
    scaling: scoring.Scaling
    assay: AssayRule


def _range(low: str, high: str) -> reference.CapRange:
    return reference.CapRange(low=decimal.Decimal(low), high=decimal.Decimal(high))


_CANONICAL_SCALED = ScaledAssay(
    factors={
        Proportion.NEAR_TO_COMPLETE: decimal.Decimal('0'),
        Proportion.SUBSTANTIAL: decimal.Decimal('-0.25'),
        Proportion.INCOMPLETE: decimal.Decimal('-1'),
    }
)

_PREDICTED_SCALED = ScaledAssay(
    factors={
        Proportion.NEAR_TO_COMPLETE: decimal.Decimal('1'),
        Proportion.SUBSTANTIAL: decimal.Decimal('0.5'),
        Proportion.INCOMPLETE: decimal.Decimal('0'),
    }
)

_STRENGTH = StrengthAssay(
    points={
        AssayStrength.CLEAR_DISRUPTION: decimal.Decimal('2'),
        AssayStrength.SOME_DISRUPTION: decimal.Decimal('1'),
        AssayStrength.UNCONVINCING: decimal.Decimal('0'),
        AssayStrength.SOME_NO_EFFECT: decimal.Decimal('-1'),
        AssayStrength.CONVINCING_NO_EFFECT: decimal.Decimal('-2'),
    }
)

_VIOLET_FIXED = FixedAssay(
    points={Proportion.SUBSTANTIAL: _ZERO, Proportion.INCOMPLETE: decimal.Decimal('-2')},
    reconsider=frozenset({Proportion.NEAR_TO_COMPLETE}),
)

_CELLS: Mapping[tuple[SpliceFlow, SpliceColour], SpliceCell] = {
    (SpliceFlow.CANONICAL, SpliceColour.YELLOW): SpliceCell(
        prd=_range('6', '6'),
        spa=_range('-6', '0'),
        prd_plus_spa=_range('0', '6'),
        fxn=_range('-8', '8'),
        plus_fxn=_range('-8', '9'),
        inf=_range('-8', '8'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
        assay=_CANONICAL_SCALED,
    ),
    (SpliceFlow.CANONICAL, SpliceColour.ORANGE): SpliceCell(
        prd=_range('-1', '6'),
        spa=_range('-6', '0'),
        prd_plus_spa=_range('-1', '6'),
        fxn=_range('-8', '8'),
        plus_fxn=_range('-8', '9'),
        inf=_range('-8', '8'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
        assay=_CANONICAL_SCALED,
    ),
    (SpliceFlow.CANONICAL, SpliceColour.BLUE): SpliceCell(
        prd=_range('0', '0'),
        spa=_range('-2', '2'),
        prd_plus_spa=_range('-2', '2'),
        fxn=_range('-8', '8'),
        plus_fxn=_range('-8', '8'),
        inf=_range('-8', '8'),
        scaling=scoring.Scaling.NONE,
        assay=_STRENGTH,
    ),
    (SpliceFlow.CANONICAL, SpliceColour.VIOLET): SpliceCell(
        prd=_range('-1', '-1'),
        spa=_range('-2', '0'),
        prd_plus_spa=_range('-3', '0'),
        fxn=_range('-8', '0'),
        plus_fxn=_range('-8', '0'),
        inf=_range('-8', '0'),
        scaling=scoring.Scaling.NONE,
        assay=_VIOLET_FIXED,
    ),
    (SpliceFlow.PREDICTED, SpliceColour.YELLOW): SpliceCell(
        prd=_range('3', '3'),
        spa=_range('0', '3'),
        prd_plus_spa=_range('0', '6'),
        fxn=_range('-8', '8'),
        plus_fxn=_range('-8', '9'),
        inf=_range('-8', '8'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
        assay=_PREDICTED_SCALED,
    ),
    (SpliceFlow.PREDICTED, SpliceColour.ORANGE): SpliceCell(
        prd=_range('-1', '3'),
        spa=_range('0', '3'),
        prd_plus_spa=_range('-1', '6'),
        fxn=_range('-8', '8'),
        plus_fxn=_range('-8', '9'),
        inf=_range('-8', '8'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
        assay=_PREDICTED_SCALED,
    ),
    (SpliceFlow.PREDICTED, SpliceColour.BLUE): SpliceCell(
        prd=_range('0', '0'),
        spa=_range('-2', '2'),
        prd_plus_spa=_range('-2', '2'),
        fxn=_range('-8', '8'),
        plus_fxn=_range('-8', '8'),
        inf=_range('-8', '8'),
        scaling=scoring.Scaling.NONE,
        assay=_STRENGTH,
    ),
    (SpliceFlow.PREDICTED, SpliceColour.VIOLET): SpliceCell(
        prd=_range('-1', '-1'),
        spa=_range('-2', '0'),
        prd_plus_spa=_range('-3', '0'),
        fxn=_range('-8', '0'),
        plus_fxn=_range('-8', '0'),
        inf=_range('-8', '0'),
        scaling=scoring.Scaling.NONE,
        assay=_VIOLET_FIXED,
    ),
}


def cell_for(flow: SpliceFlow, colour: SpliceColour) -> SpliceCell:
    """Return the decision-tree cell for one flow x colour path.

    Raises:
        ValueError: If the table has no such cell.
    """
    try:
        return _CELLS[(flow, colour)]
    except KeyError as e:
        raise ValueError(f'no splice cell for the {flow.value} {colour.value} path') from e


def spa_bounds(cell: SpliceCell, adjusted_prd: decimal.Decimal) -> reference.CapRange:
    """The interval the readings of this cell's assay span at `adjusted_prd`.

    The span, not the set: a caller holding a measured proportion the three labels do not name needs
    to be able to pass it, so the bound is what the strongest reading awards, not the readings
    themselves. On the scaled paths it tightens with the matrix, which is what a raw `SPL_SPA` value
    has to be checked against — the cell's own SPA range is the span at the cell's largest tier, so
    it would still admit a proportion taken off the pre-matrix tier the caller passed. On the fixed
    and strength paths, where the assay does not scale `SPL_PRD`, it is the cell's SPA range.
    """
    rule = cell.assay
    if isinstance(rule, ScaledAssay):
        reachable = [factor * max(adjusted_prd, _ZERO) for factor in rule.factors.values()]
    else:
        reachable = list(rule.points.values())
    return reference.CapRange(low=min(reachable), high=max(reachable))


def _as_proportion(judgement: Proportion | AssayStrength) -> Proportion:
    if not isinstance(judgement, Proportion):
        raise ValueError(f'this path scores the proportion of alternative transcripts, not {judgement!r}')
    return judgement


def spa_points(
    cell: SpliceCell, judgement: Proportion | AssayStrength, adjusted_prd: decimal.Decimal
) -> decimal.Decimal:
    """Turn an analyst's splice-assay judgement into `SPL_SPA` points for one cell.

    Args:
        cell: The flow x colour cell the path is on.
        judgement: The assay reading in the cell's own vocabulary — a `Proportion` on the scaled
            (yellow, orange) and violet paths, an `AssayStrength` on blue.
        adjusted_prd: The matrix-adjusted `SPL_PRD`; the scaled paths take a fraction of its
            positive part, the others ignore it.

    Returns:
        The `SPL_SPA` points.

    Raises:
        ValueError: If `judgement` is not in the cell's vocabulary.
        ReconsiderEvidenceError: If the reading contradicts the colour path (a violet path whose assay
            shows a near-to-complete aberrant product).
    """
    rule = cell.assay
    if isinstance(rule, StrengthAssay):
        if not isinstance(judgement, AssayStrength):
            raise ValueError(f"this path scores the splice assay's own strength, not {judgement!r}")
        return rule.points[judgement]
    proportion = _as_proportion(judgement)
    if isinstance(rule, FixedAssay):
        if proportion in rule.reconsider:
            raise ReconsiderEvidenceError(
                f'a {proportion.value} aberrant product contradicts a splice-unlikely path; re-choose the path'
            )
        return rule.points[proportion]
    return rule.factors[proportion] * max(adjusted_prd, _ZERO)


class EntryTier(enum.Enum):
    """The trichotomy a splice-predictor score falls in, at the entry to the colour paths.

    The tier is not the colour. Every splice flow puts a second question after this one — what the
    predicted product is, and whether it is frameshifting and NMD-triggering — and that answer, with
    this tier, selects the colour. So the library bins the score and the analyst picks the path.
    """

    LIKELY = 'likely'  # a splice effect is likely
    UNCERTAIN = 'uncertain'  # the prediction settles nothing either way
    UNLIKELY = 'unlikely'  # a splice effect is unlikely


# The SVI thresholds the three flows enter on, stated for SpliceAI. The trees gloss the middle band
# as two open bounds ("> 0.1 and < 0.2"), which leaves 0.1 and 0.2 themselves in no bin at all; the
# corpus summary states the same band closed ("indeterminate 0.1-0.2"), which is the reading that
# assigns every score, and both endpoints go to the band they bound.
_INDETERMINATE = (decimal.Decimal('0.1'), decimal.Decimal('0.2'))


def _tier(score: decimal.Decimal) -> EntryTier:
    low, high = _INDETERMINATE
    if score > high:
        return EntryTier.LIKELY
    if score >= low:
        return EntryTier.UNCERTAIN
    return EntryTier.UNLIKELY


@dataclasses.dataclass(frozen=True)
class SpliceDeltas:
    """Both splice predictors' strongest gain and loss, on the orientation the rpc reduced them to.

    The pairs are kept apart per predictor because the concordance check compares gain against gain:
    the two hosts do not share a sign convention upstream, and a predicted loss is invisible beside a
    co-reported gain once they are collapsed into one maximum.

    Attributes:
        spliceai_gain: `max(DS_AG, DS_DG)`; None where SpliceAI scored nothing at the position.
        spliceai_loss: `max(DS_AL, DS_DL)`, likewise.
        pangolin_gain: `DS_SG`.
        pangolin_loss: `-DS_SL`, so a larger number is a stronger predicted loss on both hosts.
        releases: The releases behind the predictions.
    """

    spliceai_gain: decimal.Decimal | None
    spliceai_loss: decimal.Decimal | None
    pangolin_gain: decimal.Decimal | None
    pangolin_loss: decimal.Decimal | None
    releases: tuple[provenance.Release, ...] = ()

    def __post_init__(self) -> None:
        for host, gain, loss in (
            ('SpliceAI', self.spliceai_gain, self.spliceai_loss),
            ('Pangolin', self.pangolin_gain, self.pangolin_loss),
        ):
            if (gain is None) is not (loss is None):
                raise ValueError(f'{host} states one delta of its pair and not the other')
            for value in (gain, loss):
                if value is not None and not decimal.Decimal(0) <= value <= decimal.Decimal(1):
                    raise ValueError(f'a {host} delta of {value} is outside the [0, 1] the scores are stated on')
        if self.spliceai_gain is None and self.pangolin_gain is None:
            raise ValueError("neither predictor scored the position; that answer is the rpc's NOT_FOUND")

    @property
    def spliceai(self) -> decimal.Decimal | None:
        """SpliceAI's stronger effect of either kind — the score the tier is read off."""
        if self.spliceai_gain is None or self.spliceai_loss is None:
            return None
        return max(self.spliceai_gain, self.spliceai_loss)

    @property
    def pangolin(self) -> decimal.Decimal | None:
        """Pangolin's stronger effect of either kind."""
        if self.pangolin_gain is None or self.pangolin_loss is None:
            return None
        return max(self.pangolin_gain, self.pangolin_loss)

    @property
    def concordant(self) -> bool | None:
        """Whether the two predictors agree, gain against gain and loss against loss.

        Agreement is over the tiers rather than the numbers: two scores either side of a threshold
        disagree about the path where two inside one bin do not, however far apart they are. None
        where a predictor scored nothing, since there is no second opinion to check against — which
        is not the same as the two disagreeing.

        The thresholds are the SVI's, stated for SpliceAI. Applying them to Pangolin is what the
        flows contemplate in naming the two tools interchangeably, and it stays a cross-check: the
        scored tier is SpliceAI's (`entry_tier`).
        """
        if self.spliceai_gain is None or self.spliceai_loss is None:
            return None
        if self.pangolin_gain is None or self.pangolin_loss is None:
            return None
        return _tier(self.spliceai_gain) is _tier(self.pangolin_gain) and _tier(self.spliceai_loss) is _tier(
            self.pangolin_loss
        )

    @property
    def derivation(self) -> str:
        """Both pairs and the concordance verdict, as the trail shows them."""
        agreement = {None: 'no second opinion', True: 'concordant', False: 'discordant'}[self.concordant]
        return (
            f'SpliceAI gain {self.spliceai_gain} / loss {self.spliceai_loss}, '
            f'Pangolin gain {self.pangolin_gain} / loss {self.pangolin_loss} ({agreement})'
        )


def deltas_from_prediction(response: splice_pb2.PredictDeltasResponse) -> SpliceDeltas:
    """Read both predictors' reduced deltas off a `Splice.PredictDeltas` response.

    The rpc has already reduced each predictor's per-transcript, per-position deltas onto one gain
    and one loss on a shared orientation, so this reads the four typed scalars and the releases
    behind them; a predictor that scored nothing states neither of its pair.

    Args:
        response: The rpc's answer. A position neither predictor scores does not arrive here — that
            is the rpc's NOT_FOUND, and the position is unscorable.

    Returns:
        The `SpliceDeltas`.

    Raises:
        ValueError: If a predictor states one delta of its pair and not the other, if a delta lies
            outside [0, 1], if neither predictor scored, or if the response states no provenance.
    """
    return SpliceDeltas(
        spliceai_gain=_delta(response.spliceai_gain, stated=response.HasField('spliceai_gain')),
        spliceai_loss=_delta(response.spliceai_loss, stated=response.HasField('spliceai_loss')),
        pangolin_gain=_delta(response.pangolin_gain, stated=response.HasField('pangolin_gain')),
        pangolin_loss=_delta(response.pangolin_loss, stated=response.HasField('pangolin_loss')),
        releases=provenance.releases_of(response.provenance),
    )


def _delta(value: float, *, stated: bool) -> decimal.Decimal | None:
    # Through `str`: the shortest round-trip decimal of the double is the figure the host published,
    # where the binary expansion would compare either side of a threshold the figure sits on.
    return decimal.Decimal(str(value)) if stated else None


def entry_tier(deltas: SpliceDeltas) -> EntryTier:
    """Bin a splice prediction onto the trichotomy the flows enter on.

    Read off SpliceAI's stronger effect of either kind, since that is the predictor the SVI states
    the thresholds for. The score alone decides the tier; the flows' second limb — a high score whose
    predicted consequence is ambiguous, which the analyst reads as uncertain rather than likely — is
    a judgement over what the product would be, and can only move a `LIKELY` down.

    Args:
        deltas: The reduced deltas (`deltas_from_prediction`).

    Returns:
        The tier.

    Raises:
        ValueError: If SpliceAI scored nothing. The thresholds are stated for it, so a
            Pangolin-only position has no calibrated tier here and the analyst reads it themselves.
    """
    score = deltas.spliceai
    if score is None:
        raise ValueError(
            'SpliceAI scored nothing at this position, and the SVI thresholds are stated for SpliceAI; '
            'a tier read off Pangolin alone is not one this table calibrates'
        )
    return _tier(score)
