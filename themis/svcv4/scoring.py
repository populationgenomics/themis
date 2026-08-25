"""The SVCv4 combining engine: per-code points to a class band.

Implements the framework's combining rules, all as exact decimal arithmetic:

  - the molecular-mechanism x exon-relevance matrix multiplier, applied to *positive* initial
    predictive points only (SM18); the missense amino-acid path scales by exon relevance alone
    (predictors already capture LoF and GoF, so mechanism is not applied there), and on that path
    the exon axis also takes SM18 §17's waiver of the reduction (`ExonRelevanceWaiver`);
  - informative-variant points added *after* the matrix and *not* reduced by it (SM19);
  - per-code caps and per-path combine/parent caps;
  - the missense amino-acid vs splice "more positive path wins" comparison (SM6 Table 1);
  - the sum-to-band mapping with VUS sub-bands;
  - the gene-disease-validity gate cap (Moderate caps at LP, Limited at VUS, below caps out).

Both the raw and matrix-adjusted predictive values are retained on every `PathResult`, as SVCv4
requires curation software to store both. Judgement (which mechanism level, which exon-relevance
call, which initial predictive tier, which informative variants are eligible) is the model's; this
module only combines the values it is handed.
"""

from __future__ import annotations

import dataclasses
import decimal
import enum

from themis.rpc import gene_disease_pb2
from themis.svcv4 import reference


class MechanismLevel(enum.Enum):
    """LoF molecular-mechanism level driving the matrix's mechanism axis (SM18)."""

    ESTABLISHED = 'Established'
    LIKELY = 'Likely'
    SUSPECTED = 'Suspected'
    UNLIKELY = 'Unlikely'
    UNKNOWN = 'Unknown'
    UNCERTAIN = 'Uncertain'


class ExonRelevance(enum.Enum):
    """Transcript/exon-relevance level driving the matrix's exon axis (SM18)."""

    ALL = 'All'
    MOST = 'Most'
    FEW = 'Few'


class PathogenicVariantBasis(enum.Enum):
    """On which of SM18 §17's two grounds a pathogenic variant in the exon is established."""

    EXPERT_CLASSIFIED = 'expert-classified'
    WELL_ESTABLISHED = 'well-established'


EXPERT_PANEL_REVIEW_STARS = 3
"""ClinVar's review-status rung at which a classification is an expert panel's (4 is a guideline)."""

_MAX_REVIEW_STARS = 4
WAIVING_CLASSIFICATION = 'P'
"""The one classification SM18 §17 rests its waiver on; it writes "P variants", not P/LP."""


@dataclasses.dataclass(frozen=True)
class ExonPathogenicVariant:
    """One pathogenic variant the exon under assessment is known to harbour (SM18 §17).

    §17's antecedent is two conjuncts — the variant is classified pathogenic, *and* that
    classification is expert-made or otherwise well established — and both are held here. Only
    whether the variant lies in the exon is left to the caller, being a placement judgement no
    classification field can settle. `EXPERT_CLASSIFIED` is a review-status claim, checked against
    ClinVar's expert-panel rung; `WELL_ESTABLISHED` is the caller's own and must name what
    establishes it, so neither basis reaches the waiver on the strength of the classification alone.

    Attributes:
        variant: How the variant is identified in the audit trail (HGVS, ClinVar id).
        classification: The variant's own classification; §17 admits `WAIVING_CLASSIFICATION` alone.
        basis: Which of §17's two grounds establishes that classification.
        review_stars: The ClinVar review-status stars; required for an `EXPERT_CLASSIFIED` claim.
        evidence: What establishes it; required for a `WELL_ESTABLISHED` claim.

    Raises:
        ValueError: If the variant is unnamed, is not classified pathogenic, or the basis claimed is
            not backed by the field it rests on.
    """

    variant: str
    classification: str
    basis: PathogenicVariantBasis
    review_stars: int | None = None
    evidence: str = ''

    def __post_init__(self) -> None:
        if not self.variant.strip():
            raise ValueError('a waiving variant must be named, so the trail records what the waiver rests on')
        if self.classification != WAIVING_CLASSIFICATION:
            raise ValueError(
                f'{self.variant} is classified {self.classification!r}; SM18 §17 defeats the reduction on '
                f'{WAIVING_CLASSIFICATION} variants in the exon, so nothing else establishes its antecedent'
            )
        if self.review_stars is not None and not 0 <= self.review_stars <= _MAX_REVIEW_STARS:
            raise ValueError(f'review stars {self.review_stars} outside ClinVar range [0, {_MAX_REVIEW_STARS}]')
        if self.basis is PathogenicVariantBasis.EXPERT_CLASSIFIED and (
            self.review_stars is None or self.review_stars < EXPERT_PANEL_REVIEW_STARS
        ):
            raise ValueError(
                f'{self.variant} is claimed expert-classified at review_stars={self.review_stars}; SM18 §17 '
                f'reads that as an expert panel, which is {EXPERT_PANEL_REVIEW_STARS} stars or more'
            )
        if self.basis is PathogenicVariantBasis.WELL_ESTABLISHED and not self.evidence.strip():
            raise ValueError(f'{self.variant} is claimed well-established with no evidence naming what establishes it')

    def describe(self) -> str:
        """The variant, its classification and the whole ground it qualifies on, for the audit trail."""
        ground = self.basis.value if self.review_stars is None else f'{self.basis.value}, {self.review_stars}-star'
        if self.evidence:
            ground = f'{ground}: {self.evidence}'
        return f'{self.variant} {self.classification} ({ground})'


@dataclasses.dataclass(frozen=True)
class ExonRelevanceWaiver:
    """SM18 §17's alternative to a tier: the exon-relevance reduction defeated, not All asserted.

    §14's All/Most/Few measure the abundance of the transcripts carrying the exon. §17 answers a
    different question — the exon harbours established pathogenic variants, so "score reduction is
    not required" — and leaves the exon factor at 1 without any claim about abundance. Passing All
    for it would put a tier in the trail nobody called.

    §17 is written for `MIS_PRD`, so `matrix_multiplier` admits the waiver on the exon-only scaling
    of the missense amino-acid path and refuses it elsewhere.

    Attributes:
        variants: The established pathogenic variants in the exon; at least one, since they are the
            antecedent the waiver rests on.

    Raises:
        ValueError: If no variant is supplied.
    """

    variants: tuple[ExonPathogenicVariant, ...]

    def __post_init__(self) -> None:
        if not self.variants:
            raise ValueError(
                'the SM18 §17 waiver rests on established pathogenic variants in the exon; with none supplied '
                'there is no antecedent and the exon-relevance tier stands'
            )

    def describe(self) -> str:
        """What was waived and what defeated it, for the audit trail."""
        established = ', '.join(variant.describe() for variant in self.variants)
        return f'exon-relevance reduction defeated (SM18 §17) by {established}'


ExonAxis = ExonRelevance | ExonRelevanceWaiver
"""What the matrix's exon axis may be handed: a §14 tier, or §17's waiver of the reduction."""


class Scaling(enum.Enum):
    """Which matrix axes scale a path's positive initial predictive points."""

    NONE = 'none'
    EXON_ONLY = 'exon_only'  # missense amino-acid MIS_PRD: exon relevance only, no mechanism
    MECHANISM_ONLY = 'mechanism_only'  # whole-gene deletion: mechanism only, no exon axis (SM13)
    MECHANISM_AND_EXON = 'mechanism_and_exon'  # LoF / splice PRD: full matrix


@dataclasses.dataclass(frozen=True)
class PointItem:
    """A named point value combined with PRD in a combine stage (e.g. FXN, splice-assay SPA).

    `note` carries the judgement behind the value — the proportion label an analyst read off the
    splice assay, say — into the audit trail.
    """

    name: str
    points: decimal.Decimal
    note: str = ''


@dataclasses.dataclass(frozen=True)
class CombineStage:
    """One combine layer: `items` are added to the running subtotal, which is then clamped to `cap`.

    Stages apply in order, each seeing the previous stage's clamped subtotal. A splice path has two
    (SPL_PRD + SPL_SPA under the colour's cap, then + SPL_FXN under its own); every other family has
    one, PRD + FXN under the family concept cap.
    """

    label: str
    items: tuple[PointItem, ...]
    cap: tuple[decimal.Decimal, decimal.Decimal]


@dataclasses.dataclass(frozen=True)
class Contribution:
    """One line of the audit trail: a component's raw and post-adjustment points.

    The `points` column is **additive** — it sums to the total the trail explains. Where a cap lands
    follows from that. A cap bounding a single component collapses onto that component's own line,
    `raw_points` holding what the caller passed and `points` the bounded value, so the reduction is
    both visible and counted once. A cap bounding a running subtotal cannot: the lines it bounds are
    already in the column, so it takes a line of its own carrying the signed ADJUSTMENT (`cap_line`)
    — an absolute there would be counted on top of them.

    `basis` and `note` answer different questions and neither substitutes for the other: `basis` is
    where the value came from — the FAF and threshold behind a POP_FRQ, the predictor and score
    behind a MIS_PRD — and `note` is what this engine did to it, the cap or the matrix.
    """

    label: str
    raw_points: decimal.Decimal
    points: decimal.Decimal
    note: str = ''
    basis: str = ''


def cap_line(label: str, subtotal: decimal.Decimal, capped: decimal.Decimal, note: str) -> Contribution:
    """The trail line for a cap that fired: the adjustment, against the subtotal it was applied to.

    Args:
        label: What capped, e.g. `"parent cap"`.
        subtotal: The value the cap was applied to, before it fired.
        capped: The value after.
        note: The bound, as the trail should show it.

    Returns:
        The `Contribution`, its `points` the (signed) adjustment so the column stays additive.
    """
    return Contribution(label=label, raw_points=subtotal, points=capped - subtotal, note=note)


@dataclasses.dataclass(frozen=True)
class PathInput:
    """One decision-tree path producing a parent code (MIS_, SPL_, NUL_, CDS_).

    The caller (the model, having made the tier judgement) supplies the initial predictive points
    and the FXN/SPA additions; this module applies the matrix, the combine stages, INF, and the
    parent cap. Every cap the path is subject to is carried here and applied by `score_path`, so one
    that fires reaches the trail against the value the caller passed rather than standing in for it.
    """

    label: str
    parent_code: str
    prd_initial: decimal.Decimal
    scaling: Scaling = Scaling.NONE
    mechanism: MechanismLevel | None = None
    exon: ExonAxis | None = None
    # SM7's critical-residue award: predictive evidence beyond what the predictor scored, so the
    # matrix scales it as it scales the tier, and it joins the combine stages under the same caps.
    critical_residue: decimal.Decimal = decimal.Decimal(0)
    combine_stages: tuple[CombineStage, ...] = ()  # FXN / SPA layers, applied in order before INF
    inf: decimal.Decimal = decimal.Decimal(0)  # added after the matrix, exempt from it
    inf_cap: tuple[decimal.Decimal, decimal.Decimal] | None = None  # None leaves INF as passed
    parent_cap: tuple[decimal.Decimal, decimal.Decimal] | None = None


@dataclasses.dataclass(frozen=True)
class PathResult:
    """The scored result of one path, with its raw and matrix-adjusted predictive values retained."""

    label: str
    parent_code: str
    raw_prd: decimal.Decimal
    adjusted_prd: decimal.Decimal
    multiplier: decimal.Decimal
    total: decimal.Decimal
    contributions: tuple[Contribution, ...]


@dataclasses.dataclass(frozen=True)
class GateOutcome:
    """The result of applying the gene-disease-validity gate to a class."""

    final_class: str
    capped: bool


def clamp(value: decimal.Decimal, low: decimal.Decimal, high: decimal.Decimal) -> decimal.Decimal:
    """Clamp `value` to the inclusive `[low, high]` interval."""
    return max(low, min(value, high))


def matrix_multiplier(
    ref: reference.Reference,
    scaling: Scaling,
    mechanism: MechanismLevel | None,
    exon: ExonAxis | None,
) -> decimal.Decimal:
    """Return the fraction that scales positive initial predictive points for a path.

    Args:
        ref: The loaded reference (supplies the mechanism and exon factor tables).
        scaling: Which axes apply (none, exon-only, or the full mechanism x exon matrix).
        mechanism: The mechanism level; required when `scaling` uses the mechanism axis.
        exon: The exon axis — a §14 relevance tier, or §17's `ExonRelevanceWaiver`; required when
            `scaling` uses the exon axis.

    Returns:
        The multiplier (1 for `NONE`).

    Raises:
        ValueError: If a level required by `scaling` is not supplied, or the §17 waiver is passed on
            a path it has no scope over.
    """
    if isinstance(exon, ExonRelevanceWaiver) and scaling is not Scaling.EXON_ONLY:
        raise ValueError(
            f'SM18 §17 waives the exon-relevance reduction on MIS_PRD; scaling {scaling.value} is not the '
            'missense amino-acid path, so the waiver has no scope there and the exon-relevance tier stands'
        )
    if scaling is Scaling.NONE:
        return decimal.Decimal(1)
    if scaling is Scaling.MECHANISM_ONLY:
        if mechanism is None:
            raise ValueError('mechanism level required for mechanism_only scaling')
        return ref.mechanism_factors[mechanism.value]
    if exon is None:
        raise ValueError(f'exon relevance required for scaling {scaling.value}')
    if isinstance(exon, ExonRelevanceWaiver):
        return decimal.Decimal(1)
    exon_factor = ref.exon_factors[exon.value]
    if scaling is Scaling.EXON_ONLY:
        return exon_factor
    if mechanism is None:
        raise ValueError('mechanism level required for mechanism_and_exon scaling')
    if (mechanism.value, exon.value) == ref.matrix_omitted_cell:
        return decimal.Decimal(0)  # SM18 Figure 1 states 0% for this cell, not the axis product
    return ref.mechanism_factors[mechanism.value] * exon_factor


def matrix_note(scaling: Scaling, exon: ExonAxis | None, multiplier: decimal.Decimal) -> str:
    """The audit trail's account of the matrix adjustment: the multiplier, and what set it.

    A `1` from the §14 All tier and a `1` from §17's waiver are different findings, so the waiver
    states its antecedent here rather than leaving the trail to read as an abundance call.
    """
    if scaling is Scaling.NONE:
        return ''
    if isinstance(exon, ExonRelevanceWaiver):
        return f'matrix x{multiplier}; {exon.describe()}'
    return f'matrix x{multiplier}'


def apply_matrix(initial: decimal.Decimal, multiplier: decimal.Decimal) -> decimal.Decimal:
    """Scale `initial` by `multiplier` only if it is positive; negatives and zero pass through."""
    return initial * multiplier if initial > 0 else initial


def _tiered(primary: int, secondary: int, strong: bool) -> decimal.Decimal:
    """Sum one direction of informative-variant points (P/B are primary, LP/LB secondary)."""
    total = primary + secondary
    if total == 0:
        return decimal.Decimal(0)
    if primary >= 1:
        first = decimal.Decimal(4 if strong else 2)
        rest = decimal.Decimal(2 if strong else 1)
        return first + rest * (total - 1)
    each = decimal.Decimal(2 if strong else 1)
    return each * total


def informative_points(classifications: tuple[str, ...], *, strong: bool = False) -> decimal.Decimal:
    """Sum informative-variant points over distinct classified variants (SM19).

    Counts distinct variants only (observation count is irrelevant). Pathogenic and benign
    informative variants sum; the first full-strength variant scores more than each additional one.

    Args:
        classifications: The classification of each distinct informative variant, each one of
            'P', 'LP', 'B', 'LB'.
        strong: Use the same-amino-acid weights (first +/-4, additional +/-2) instead of the
            default (first +/-2, additional +/-1). See the MIS_INF same-codon sub-rule (SM6).

    Returns:
        The summed points (uncapped; the caller applies the INF code range).

    Raises:
        ValueError: On an unrecognised classification token.
    """
    counts = {'P': 0, 'LP': 0, 'B': 0, 'LB': 0}
    for token in classifications:
        if token not in counts:
            raise ValueError(f'unrecognised informative-variant classification {token!r}')
        counts[token] += 1
    pathogenic = _tiered(counts['P'], counts['LP'], strong)
    benign = -_tiered(counts['B'], counts['LB'], strong)
    return pathogenic + benign


def score_path(ref: reference.Reference, path: PathInput) -> PathResult:
    """Score one decision-tree path to its parent-code total, retaining raw and adjusted PRD.

    Applies the matrix to positive initial predictive points and to the critical-residue award, runs
    the combine stages in order (each adding its items then clamping to its cap), adds INF after the
    matrix (exempt from it, and bounded by `inf_cap` where the path carries one), then clamps to the
    parent cap.

    Args:
        ref: The loaded reference.
        path: The path's inputs.

    Returns:
        The `PathResult` with the parent total and a per-component audit trail.
    """
    multiplier = matrix_multiplier(ref, path.scaling, path.mechanism, path.exon)
    adjusted_prd = apply_matrix(path.prd_initial, multiplier)
    note = matrix_note(path.scaling, path.exon, multiplier)

    contributions = [
        Contribution(
            label=f'{path.parent_code}PRD',
            raw_points=path.prd_initial,
            points=adjusted_prd,
            note=note,
        )
    ]
    combined = adjusted_prd
    if path.critical_residue:
        awarded = apply_matrix(path.critical_residue, multiplier)
        contributions.append(
            Contribution(
                label='critical residue',
                raw_points=path.critical_residue,
                points=awarded,
                note=note,
            )
        )
        combined += awarded
    for stage in path.combine_stages:
        for item in stage.items:
            combined += item.points
            contributions.append(
                Contribution(label=item.name, raw_points=item.points, points=item.points, note=item.note)
            )
        capped = clamp(combined, *stage.cap)
        if capped != combined:
            contributions.append(cap_line(f'{stage.label} cap', combined, capped, str(stage.cap)))
        combined = capped

    inf = path.inf
    inf_note = 'after matrix; exempt'
    if path.inf_cap is not None:
        inf = clamp(path.inf, *path.inf_cap)
        if inf != path.inf:
            inf_note = f'{inf_note}; cap [{path.inf_cap[0]}, {path.inf_cap[1]}]'
    total = combined + inf
    if path.inf != 0 or inf != 0:
        contributions.append(
            Contribution(label=f'{path.parent_code}INF', raw_points=path.inf, points=inf, note=inf_note)
        )
    if path.parent_cap is not None:
        capped_total = clamp(total, *path.parent_cap)
        if capped_total != total:
            contributions.append(cap_line('parent cap', total, capped_total, str(path.parent_cap)))
        total = capped_total

    return PathResult(
        label=path.label,
        parent_code=path.parent_code,
        raw_prd=path.prd_initial,
        adjusted_prd=adjusted_prd,
        multiplier=multiplier,
        total=total,
        contributions=tuple(contributions),
    )


def select_path(amino_acid: PathResult, splice: PathResult) -> tuple[PathResult, PathResult]:
    """Choose between the missense amino-acid and splice paths (SM6 Table 1).

    A negative splice score means the effect is via the amino-acid change, so the amino-acid path
    is used; otherwise the more positive path wins, with ties favouring the amino-acid path (higher
    prior that the effect is via the amino-acid change). The non-selected path is retained.

    Returns:
        `(selected, alternate)`.
    """
    # Splice is picked iff splice >= 0 and splice > amino-acid; ties and negative splice keep the
    # amino-acid path. Confirmed against ClinGen calc-phase3.js getMaxOrMin.
    if splice.total < 0 or amino_acid.total >= splice.total:
        return amino_acid, splice
    return splice, amino_acid


def band_for_total(ref: reference.Reference, total: decimal.Decimal) -> tuple[str, str | None]:
    """Map a point total to its classification band and (for VUS) sub-band.

    Returns:
        `(class_code, vus_subband)`, where `vus_subband` is one of the VUS sub-band codes when the
        class is VUS, else `None`.

    Raises:
        reference.ReferenceDataError: If no band contains `total` (a broken reference partition).
    """
    band = next((b for b in ref.bands if b.contains(total)), None)
    if band is None:
        raise reference.ReferenceDataError(f'no classification band contains {total}')
    if band.code != 'VUS':
        return band.code, None
    subband = next((s for s in ref.vus_subbands if s.contains(total)), None)
    return band.code, subband.code if subband is not None else None


def gate_row(ref: reference.Reference, gate_level: gene_disease_pb2.GateLevel) -> reference.GateRow:
    """The gate the reference states for a level.

    The model composes this untyped in code mode, so both slips are checked here: a value that is
    not a `GateLevel` at all, and one the reference states no gate for — `GATE_LEVEL_UNSPECIFIED`,
    the absence of a curated level, being the one that arrives from a missed entity lookup.

    Args:
        ref: The loaded reference.
        gate_level: The gene-disease-validity gate level; `gene_disease_validity.gate_level` maps a
            curator's classification onto one.

    Returns:
        That level's row of the gate table.

    Raises:
        ValueError: If `gate_level` is not a `GateLevel` value, or the reference gates no such level.
    """
    if type(gate_level) is not int:  # bool, float and Decimal hash equal to a level; membership alone admits them
        raise ValueError(
            f'gene-disease-validity gate level must be a GateLevel value, got a {type(gate_level).__name__}; '
            f"a curator's classification maps onto one through gene_disease_validity.gate_level"
        )
    if gate_level not in ref.gate:
        carried = [reference.gate_level_name(level) for level in sorted(ref.gate)]
        raise ValueError(
            f'unknown gene-disease-validity gate level {reference.gate_level_name(gate_level)}; the levels are '
            f"{carried} and a curator's classification maps onto one through gene_disease_validity.gate_level"
        )
    return ref.gate[gate_level]


def apply_gate(ref: reference.Reference, class_code: str, gate_level: gene_disease_pb2.GateLevel) -> GateOutcome:
    """Cap a class by the gene-disease-validity gate (SM18 / the gate table).

    Definitive/Strong permit any class; Moderate caps at LP; Limited caps at VUS; below Limited the
    gate yields a terminal result (Variant in Gene of Uncertain Significance, or do-not-report)
    regardless of the computed class.

    Args:
        ref: The loaded reference.
        class_code: The computed class (one of the ordered classes).
        gate_level: The gene-disease-validity gate level; `gene_disease_validity.gate_level` maps a
            curator's classification onto one.

    Returns:
        The gated outcome.

    Raises:
        ValueError: If `gate_level` is not a gate level the reference carries, or `class_code` is
            not one of its classes.
    """
    level = gate_row(ref, gate_level)
    if level.result is not None:
        # A terminal level (Less-than-Limited/Disputed) overrides even a benign call. ClinGen
        # calc-phase3.js implements NO gene-disease-validity gate (it is left to the curator), so this
        # framework-specified gate is unvalidatable against the calculator; its benign-blocking here
        # is a caveat pending the GDV recommendation.
        return GateOutcome(final_class=level.result, capped=True)
    if class_code not in ref.class_order:
        raise ValueError(f'unknown class {class_code!r}')
    computed = ref.class_order.index(class_code)
    allowed_max = max(ref.class_order.index(a) for a in level.allows)
    capped = min(computed, allowed_max)
    return GateOutcome(final_class=ref.class_order[capped], capped=capped != computed)
