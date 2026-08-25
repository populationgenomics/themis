"""Typed per-variant-type path builders for SVCv4.

Each `build_<type>` takes the judgement inputs a caller/model supplies for one SVCv4 variant type
and returns a `classify.ClassificationInput` with the framework structure baked in: the PFD path(s)
the variant type has (per the workflow transcription `data.meta.CITED_DOCUMENTS` pins,
`svcv4-docs/workflow-images/<type>.decision-tree.md`), the
scaling axes, and the combine/parent caps.

Division of labour: the caller supplies judgement — the initial-PRD tier (its own decision-tree
call), the mechanism level, the exon-relevance call, the FXN/INF points, the splice-assay reading,
the critical-residue award, and the POP/CLN/LOC code points. The builder supplies structure — the
caps, the scaling mode, which paths the type has, and (on a splice path) the `SPL_SPA` points its
assay reading implies. No builder invents a tier value; the initial PRD is always a caller input.

Where the caps come from: `ref` for everything a code, concept or category cap covers, and a cell
table for the two variant types whose paths the reference states only as a union over them —
`splice_tree` (its `SPL_PRD` and `SPL_PRD_SPA` ranges span the three workflows that reach a splice
tier, so enforcing them alone admits a canonical +6.0 tier on a predicted-splice path) and
`duplication_tree` (its `NUL_`/`CDS_` caps span every supplement reaching those families, so
enforcing them alone admits a not-tandem duplication two bands above its own parent cap).

Missense is the one type with two paths (amino-acid and splice) that `classify` selects between by
the max-path rule; every other type has a single PFD path. The mechanism-below-Moderate precondition
(SM18) is enforced by `classify.classify` on the returned input, not re-checked here.

`for_consequence` is the same set of builders reached without naming one: the framework picks a tree
by molecular consequence, and the agent holds that consequence typed on the wire, so routing on it
removes a step that can only be got wrong. What the consequence does not settle is which arm of a LoF
tree the variant took — NMD or not, rescued or not — and the evidence family the caller supplies is
how that judgement arrives. `classify_variant` is that routing and the scoring together, which is
what an agent classifying one variant calls.
"""

from __future__ import annotations

import dataclasses
import decimal
from collections.abc import Mapping, Sequence

from themis.evidence.models import evidence_pb2
from themis.rpc import gene_disease_pb2
from themis.svcv4 import classify, duplication_tree, provenance, reference, scoring, splice_tree

_ZERO = decimal.Decimal(0)

IndependentCodes = Mapping[str, decimal.Decimal] | Sequence[classify.ScoredCode]
"""How a builder takes the codes scored outside the variant-type path.

The mapping is for a caller that reduced them itself; the sequence is what the doors return, and it
carries each code's derivation and releases onto the tally with it.
"""


@dataclasses.dataclass(frozen=True)
class AminoAcidEvidence:
    """The missense amino-acid (green) path: predictor points scaled by exon relevance only (SM6).

    Attributes:
        mis_prd: MIS_PRD initial points from the pre-selected predictor (`predictors.py`), pre-matrix.
        exon: Exon-relevance call, or SM18 §17's `scoring.ExonRelevanceWaiver` (scales positive
            MIS_PRD; mechanism is not applied on this path). §17 is written for `MIS_PRD`, so this is
            the one path whose exon axis takes the waiver.
        mis_fxn: MIS_FXN points (OddsPath-calibrated), combined with MIS_PRD under the MIS concept cap.
        mis_inf: MIS_INF points (`grantham.mis_inf_points`), added after the matrix.
        critical_residue: SM7's critical-amino-acid award — predictive evidence beyond the tier, so
            the exon matrix scales it as it scales MIS_PRD and the MIS concept cap bounds their sum.
            `grantham.mis_inf_points`' motif rule scores the same judgement as MIS_INF where it
            stands in for an absent informative variant (SM6 reaching it through SM7); one residue
            funds one of the two, never both. The caps that bound the path bound this as any other
            contribution; what is refused rather than trimmed is an award SM7's own conditions
            withhold (`_check_critical_residue`).
        releases: The upstream releases behind the values on this path, as each door returned them.
            The points are the caller's own reduction of those answers, so nothing else on the path
            can carry them to the tally.
    """

    mis_prd: decimal.Decimal
    exon: scoring.ExonAxis
    mis_fxn: decimal.Decimal | None = None
    mis_inf: decimal.Decimal = _ZERO
    critical_residue: decimal.Decimal = _ZERO
    releases: tuple[provenance.Release, ...] = ()


@dataclasses.dataclass(frozen=True)
class SpliceEvidence:
    """A splice (SPL) PFD path (the missense splice branch, canonical splice, or intronic; SM6/11/12).

    The colour selects the decision-tree cell together with the flow the builder knows, and the cell
    fixes every bound: which initial tiers the path admits, which way the splice assay runs, and the
    two combine caps. Scaling is the cell's, not a caller input — the blue and violet paths state
    "MM x ER NOT considered", so a `mechanism` or `exon` call on one of them is rejected rather than
    ignored.

    Attributes:
        colour: The colour path the splice-predictor score and predicted consequence selected.
        spl_prd: SPL_PRD initial points, pre-matrix, from the colour's own tier table.
        mechanism: Mechanism level; required on yellow/orange, rejected on blue/violet.
        exon: Exon-relevance call; required on yellow/orange, rejected on blue/violet.
        spl_spa: The splice-assay reading in the cell's vocabulary — a `Proportion` on
            yellow/orange/violet, an `AssayStrength` on blue. The builder derives the points.
        spl_spa_points: A raw SPL_SPA value for a caller holding a measured proportion the labels do
            not name, bounded by what the cell's strongest reading awards at this path's adjusted
            PRD (so a value between two labels passes, one beyond them does not); excludes
            `spl_spa`.
        spl_fxn: SPL_FXN (protein-assay) points, combined with (SPL_PRD + SPL_SPA).
        spl_inf: SPL_INF points, added after the matrix. Checked against the cell's range rather
            than clamped to it, unlike the other families' INF: a value the colour cannot award is
            a miscounted informative variant, not a total to trim.
        releases: The upstream releases behind the values on this path, as each door returned them.
            The points are the caller's own reduction of those answers, so nothing else on the path
            can carry them to the tally.
    """

    colour: splice_tree.SpliceColour
    spl_prd: decimal.Decimal
    mechanism: scoring.MechanismLevel | None = None
    exon: scoring.ExonRelevance | None = None
    spl_spa: splice_tree.Proportion | splice_tree.AssayStrength | None = None
    spl_spa_points: decimal.Decimal | None = None
    spl_fxn: decimal.Decimal | None = None
    spl_inf: decimal.Decimal = _ZERO
    releases: tuple[provenance.Release, ...] = ()


@dataclasses.dataclass(frozen=True)
class NullEvidence:
    """The NUL PFD path (nonsense / frameshift / exon-deletion NMD or whole-gene branch; SM8/9/13).

    Attributes:
        nul_prd: NUL_PRD initial points (e.g. +6 NMD, +4 NSD, +10 whole-gene deletion).
        mechanism: Mechanism level (LoF paths always scale by mechanism).
        exon: Exon-relevance call; None marks a whole-gene deletion (mechanism-only, no exon axis).
        nul_fxn: NUL_FXN points, combined with NUL_PRD under the NUL concept cap.
        nul_inf: NUL_INF points, added after the matrix.
        releases: The upstream releases behind the values on this path, as each door returned them.
            The points are the caller's own reduction of those answers, so nothing else on the path
            can carry them to the tally.
    """

    nul_prd: decimal.Decimal
    mechanism: scoring.MechanismLevel
    exon: scoring.ExonRelevance | None = None
    nul_fxn: decimal.Decimal | None = None
    nul_inf: decimal.Decimal = _ZERO
    releases: tuple[provenance.Release, ...] = ()


@dataclasses.dataclass(frozen=True)
class CodingEvidence:
    """The CDS PFD path (in-frame indel / start-lost / stop-lost; SM10/15/16).

    Attributes:
        cds_prd: CDS_PRD initial points (protein-fraction / critical-domain tier, the caller's call).
        mechanism: Mechanism level.
        exon: Exon-relevance call.
        cds_fxn: CDS_FXN points, combined with CDS_PRD under the CDS concept cap.
        cds_inf: CDS_INF points, added after the matrix.
        releases: The upstream releases behind the values on this path, as each door returned them.
            The points are the caller's own reduction of those answers, so nothing else on the path
            can carry them to the tally.
    """

    cds_prd: decimal.Decimal
    mechanism: scoring.MechanismLevel
    exon: scoring.ExonRelevance
    cds_fxn: decimal.Decimal | None = None
    cds_inf: decimal.Decimal = _ZERO
    releases: tuple[provenance.Release, ...] = ()


@dataclasses.dataclass(frozen=True)
class DuplicationEvidence:
    """A single-/multi-exon duplication/gain path (SM14); the path fixes the family and every bound.

    The tandem, breakpoint and NMD decisions select the path, and the path — not the caller — decides
    whether the evidence codes under `NUL_` or `CDS_`, which initial tiers it admits, whether the
    matrix applies, whether functional data is weighed at all, and the two caps a not-tandem path
    holds tighter than its family's.

    Attributes:
        path: The flow-diagram path those decisions selected.
        prd: The initial predictive tier, pre-matrix, from the path's own tier table.
        mechanism: Mechanism level; required on the scaled paths, rejected on the unscaled ones.
        exon: Exon-relevance call; likewise. SM18 §17's waiver has no scope here.
        fxn: Functional points. The not-tandem paths and the lower orange one score `FXN_NA` and
            reject a value rather than scoring it.
        inf: Informative-variant points, added after the matrix.
        releases: The upstream releases behind the values on this path, as each door returned them.
            The points are the caller's own reduction of those answers, so nothing else on the path
            can carry them to the tally.
    """

    path: duplication_tree.DuplicationPath
    prd: decimal.Decimal
    mechanism: scoring.MechanismLevel | None = None
    exon: scoring.ExonRelevance | None = None
    fxn: decimal.Decimal | None = None
    inf: decimal.Decimal = _ZERO
    releases: tuple[provenance.Release, ...] = ()


def _check_code_range(ref: reference.Reference, code: str, value: decimal.Decimal) -> None:
    spec = ref.code(code)
    if not spec.low <= value <= spec.high:
        raise ValueError(f'{code} value {value} outside its cap [{spec.low}, {spec.high}]')


def _check_prd(family: str, value: decimal.Decimal, bound: reference.CapRange, where: str) -> None:
    if not bound.low <= value <= bound.high:
        raise ValueError(f'{family}_PRD initial {value} outside [{bound.low}, {bound.high}] on the {where}')


def _check_range(code: str, value: decimal.Decimal, bound: reference.CapRange, where: str) -> None:
    if not bound.low <= value <= bound.high:
        raise ValueError(f'{code} value {value} outside [{bound.low}, {bound.high}] on the {where}')


def _code_range(ref: reference.Reference, code: str) -> reference.CapRange:
    spec = ref.code(code)
    return reference.CapRange(low=spec.low, high=spec.high)


def _concept_stage(
    ref: reference.Reference, family: str, fxn: decimal.Decimal | None
) -> tuple[scoring.CombineStage, ...]:
    """The single PRD + FXN combine layer of a non-splice family, under the family concept cap."""
    if fxn is None:
        return ()
    _check_code_range(ref, f'{family}_FXN', fxn)
    cap = ref.concept_cap(family)
    return (
        scoring.CombineStage(
            label=f'{family}_PRD + {family}_FXN',
            items=(scoring.PointItem(f'{family}_FXN', fxn),),
            cap=(cap.low, cap.high),
        ),
    )


def _pfd_path(
    ref: reference.Reference,
    *,
    family: str,
    label: str,
    prd_initial: decimal.Decimal,
    scaling: scoring.Scaling,
    mechanism: scoring.MechanismLevel | None,
    exon: scoring.ExonAxis | None,
    inf: decimal.Decimal,
    prd_bound: reference.CapRange,
    where: str,
    critical_residue: decimal.Decimal = _ZERO,
    combine_stages: tuple[scoring.CombineStage, ...] = (),
    clamp_inf: bool = True,
    inf_bound: reference.CapRange | None = None,
    parent_bound: reference.CapRange | None = None,
) -> scoring.PathInput:
    """Assemble one PFD predictive path, validating the initial tier and carrying the INF/parent caps.

    `prd_bound` is the path's own range for the initial predictive tier — the code range for most
    families, the NUL_PFD category cap for the whole-gene deletion whose +10 tier exceeds the
    NUL_PRD code cap, and the cell's range on a splice or duplication path.

    The INF sum is passed through as given, with its cap alongside, so `score_path` reports a sum
    above the cap against what the caller passed rather than as the bound. `clamp_inf=False`
    withholds the cap for a caller that has already range-checked INF against a narrower bound.
    `inf_bound` and `parent_bound` state a decision-tree cell's own bounds where it holds one tighter
    than the family's; unset, both come from the reference.
    """
    _check_prd(family, prd_initial, prd_bound, where)
    parent = ref.category_cap(f'{family}_PFD') if parent_bound is None else parent_bound
    inf_cap = None
    if clamp_inf:
        inf_cap = ref.concept_cap(f'{family}_INF') if inf_bound is None else inf_bound
    return scoring.PathInput(
        label=label,
        parent_code=f'{family}_',
        prd_initial=prd_initial,
        scaling=scaling,
        mechanism=mechanism,
        exon=exon,
        critical_residue=critical_residue,
        combine_stages=combine_stages,
        inf=inf,
        inf_cap=None if inf_cap is None else (inf_cap.low, inf_cap.high),
        parent_cap=(parent.low, parent.high),
    )


def _check_critical_residue(ref: reference.Reference, amino_acid: AminoAcidEvidence) -> None:
    """Hold SM7's critical-residue award to the two conditions the supplement states for it.

    The award is additional predictive evidence above the predictor's own points (SM7 §5), so it
    needs points to sit on: a predictor tier at or below zero is a call the supplement never
    contemplates awarding against. And it is withheld once the predictive and informative evidence
    reach their combined maximum without it. SM7 leaves that maximum an unfilled placeholder, so the
    bound used is the MIS_PFD category cap, the only one the framework states over PRD and INF
    together.

    Raises:
        ValueError: If the award is outside SM7's range, sits on a non-positive predictor tier, or
            its combined-maximum precondition is unmet.
    """
    award = amino_acid.critical_residue
    if not _ZERO <= award <= ref.critical_residue_max:
        raise ValueError(f'critical-residue award {award} outside [0, {ref.critical_residue_max}] (SM7)')
    if amino_acid.mis_prd <= _ZERO:
        raise ValueError(
            f'a critical-residue award needs a positive predictor tier to sit on top of; MIS_PRD is '
            f'{amino_acid.mis_prd} (SM7)'
        )
    multiplier = scoring.matrix_multiplier(ref, scoring.Scaling.EXON_ONLY, None, amino_acid.exon)
    inf_cap = ref.concept_cap('MIS_INF')
    combined = scoring.apply_matrix(amino_acid.mis_prd, multiplier) + scoring.clamp(
        amino_acid.mis_inf, inf_cap.low, inf_cap.high
    )
    ceiling = ref.category_cap('MIS_PFD').high
    if combined >= ceiling:
        raise ValueError(
            f'the critical-residue award is withheld: MIS_PRD + MIS_INF reach {combined} without it, at or '
            f'above the {ceiling} maximum stated over the two (SM7)'
        )


def _amino_acid_path(ref: reference.Reference, amino_acid: AminoAcidEvidence) -> scoring.PathInput:
    if amino_acid.critical_residue:
        _check_critical_residue(ref, amino_acid)
    return _pfd_path(
        ref,
        family='MIS',
        label='amino-acid (MIS_)',
        prd_initial=amino_acid.mis_prd,
        prd_bound=_code_range(ref, 'MIS_PRD'),
        where='missense amino-acid path',
        scaling=scoring.Scaling.EXON_ONLY,
        mechanism=None,
        exon=amino_acid.exon,
        critical_residue=amino_acid.critical_residue,
        combine_stages=_concept_stage(ref, 'MIS', amino_acid.mis_fxn),
        inf=amino_acid.mis_inf,
    )


def _check_matrix_axes(
    mechanism: scoring.MechanismLevel | None,
    exon: scoring.ExonAxis | None,
    scaling: scoring.Scaling,
    where: str,
) -> None:
    """Fail loud when the caller's mechanism/exon calls and the path's scaling disagree."""
    if scaling is scoring.Scaling.NONE:
        if mechanism is not None or exon is not None:
            raise ValueError(f'the {where} does not apply the mechanism x exon matrix; leave both None')
    elif mechanism is None or exon is None:
        raise ValueError(f'the {where} scales by the mechanism x exon matrix; both calls are required')


def _splice_spa(
    evidence: SpliceEvidence, cell: splice_tree.SpliceCell, adjusted_prd: decimal.Decimal, where: str
) -> scoring.PointItem | None:
    """The SPL_SPA item: derived from the assay reading, or the caller's raw value, or absent.

    Raises:
        ValueError: If both forms are supplied, or the value lies beyond what the cell's strongest
            reading awards at this adjusted PRD.
        splice_tree.ReconsiderEvidenceError: If the reading contradicts the colour path.
    """
    if evidence.spl_spa is not None and evidence.spl_spa_points is not None:
        raise ValueError('pass spl_spa (the assay reading) or spl_spa_points (a raw value), not both')
    if evidence.spl_spa is not None:
        points, note = splice_tree.spa_points(cell, evidence.spl_spa, adjusted_prd), evidence.spl_spa.value
    elif evidence.spl_spa_points is not None:
        points, note = evidence.spl_spa_points, 'caller-supplied'
    else:
        return None
    bounds = splice_tree.spa_bounds(cell, adjusted_prd)
    if not bounds.low <= points <= bounds.high:
        raise ValueError(
            f'SPL_SPA {points} outside [{bounds.low}, {bounds.high}] on the {where} at an adjusted '
            f'SPL_PRD of {adjusted_prd}'
        )
    return scoring.PointItem('SPL_SPA', points, note=note)


def _splice_path(
    ref: reference.Reference, evidence: SpliceEvidence, *, flow: splice_tree.SpliceFlow
) -> scoring.PathInput:
    """Assemble the SPL path for one flow x colour cell, deriving SPL_SPA from the assay reading."""
    cell = splice_tree.cell_for(flow, evidence.colour)
    where = f'{flow.value} {evidence.colour.value} path'
    _check_matrix_axes(evidence.mechanism, evidence.exon, cell.scaling, where)
    _check_prd('SPL', evidence.spl_prd, cell.prd, where)  # before the assay takes a proportion of it
    multiplier = scoring.matrix_multiplier(ref, cell.scaling, evidence.mechanism, evidence.exon)
    spa = _splice_spa(evidence, cell, scoring.apply_matrix(evidence.spl_prd, multiplier), where)
    fxn: tuple[scoring.PointItem, ...] = ()
    if evidence.spl_fxn is not None:
        _check_range('SPL_FXN', evidence.spl_fxn, cell.fxn, where)
        fxn = (scoring.PointItem('SPL_FXN', evidence.spl_fxn),)
    # A splice path's INF is checked, not clamped: on the benign-only violet module positive points
    # mean a P/LP informative variant was counted, which its tree routes to a re-evaluation.
    _check_range('SPL_INF', evidence.spl_inf, cell.inf, where)
    return _pfd_path(
        ref,
        family='SPL',
        label=f'splice {evidence.colour.value} (SPL_)',
        prd_initial=evidence.spl_prd,
        prd_bound=cell.prd,
        where=where,
        scaling=cell.scaling,
        mechanism=evidence.mechanism,
        exon=evidence.exon,
        combine_stages=(
            scoring.CombineStage(
                label='SPL_PRD + SPL_SPA',
                items=() if spa is None else (spa,),
                cap=(cell.prd_plus_spa.low, cell.prd_plus_spa.high),
            ),
            scoring.CombineStage(
                label='(SPL_PRD + SPL_SPA) + SPL_FXN', items=fxn, cap=(cell.plus_fxn.low, cell.plus_fxn.high)
            ),
        ),
        inf=evidence.spl_inf,
        clamp_inf=False,
    )


def _null_path(ref: reference.Reference, null: NullEvidence, *, whole_gene: bool) -> scoring.PathInput:
    if whole_gene:
        if null.exon is not None:
            raise ValueError('whole-gene deletion scales by mechanism only; exon relevance must be None')
        return _pfd_path(
            ref,
            family='NUL',
            label='whole-gene (NUL_)',
            prd_initial=null.nul_prd,
            # the +10 whole-gene tier exceeds the NUL_PRD code cap [0,6]
            prd_bound=ref.category_cap('NUL_PFD'),
            where='whole-gene deletion path',
            scaling=scoring.Scaling.MECHANISM_ONLY,
            mechanism=null.mechanism,
            exon=None,
            combine_stages=_concept_stage(ref, 'NUL', null.nul_fxn),
            inf=null.nul_inf,
        )
    if null.exon is None:
        raise ValueError('subgenic NUL path requires an exon-relevance call')
    return _pfd_path(
        ref,
        family='NUL',
        label='NUL_',
        prd_initial=null.nul_prd,
        prd_bound=_code_range(ref, 'NUL_PRD'),
        where='subgenic NUL path',
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
        mechanism=null.mechanism,
        exon=null.exon,
        combine_stages=_concept_stage(ref, 'NUL', null.nul_fxn),
        inf=null.nul_inf,
    )


def _coding_path(ref: reference.Reference, coding: CodingEvidence) -> scoring.PathInput:
    return _pfd_path(
        ref,
        family='CDS',
        label='CDS_',
        prd_initial=coding.cds_prd,
        prd_bound=_code_range(ref, 'CDS_PRD'),
        where='CDS path',
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
        mechanism=coding.mechanism,
        exon=coding.exon,
        combine_stages=_concept_stage(ref, 'CDS', coding.cds_fxn),
        inf=coding.cds_inf,
    )


def _duplication_stages(
    evidence: DuplicationEvidence, cell: duplication_tree.DuplicationCell, where: str
) -> tuple[scoring.CombineStage, ...]:
    """The PRD + FXN combine layer, or none where SM14 scores this path's functional data `NA`.

    Raises:
        ValueError: If functional points are supplied on a path that scores `FXN_NA`, or lie outside
            the cell's range.
    """
    if cell.functional is None:
        if evidence.fxn is not None:
            raise ValueError(
                f'the {where} scores {cell.family}_FXN as NA (SM14); pass no functional points rather than 0.0, '
                'which would read as an assay that measured nothing'
            )
        return ()
    if evidence.fxn is None:
        return ()
    _check_range(f'{cell.family}_FXN', evidence.fxn, cell.functional.fxn, where)
    return (
        scoring.CombineStage(
            label=f'{cell.family}_PRD + {cell.family}_FXN',
            items=(scoring.PointItem(f'{cell.family}_FXN', evidence.fxn),),
            cap=(cell.functional.combined.low, cell.functional.combined.high),
        ),
    )


def _duplication_path(ref: reference.Reference, evidence: DuplicationEvidence) -> scoring.PathInput:
    """Assemble the SM14 path for one duplication/gain cell, under the cell's own caps."""
    cell = duplication_tree.cell_for(evidence.path)
    where = f'SM14 {evidence.path.value} path'
    _check_matrix_axes(evidence.mechanism, evidence.exon, cell.scaling, where)
    # A benign-only module's INF is checked, not clamped: clamping a pathogenic informative variant
    # to 0.0 would score the path the tree has just routed away from.
    if cell.benign_informative_only:
        if evidence.inf > cell.inf.high:
            raise ValueError(
                f'the {where} scores B/LB informative variants only; SM14 routes a pathogenic informative '
                f'variant of a similarly altered region to "reconsider the use of this path", not to '
                f'{evidence.inf} points'
            )
        _check_range(f'{cell.family}_INF', evidence.inf, cell.inf, where)
    return _pfd_path(
        ref,
        family=cell.family,
        label=f'exon duplication {evidence.path.value} ({cell.family}_)',
        prd_initial=evidence.prd,
        prd_bound=cell.prd,
        where=where,
        scaling=cell.scaling,
        mechanism=evidence.mechanism,
        exon=evidence.exon,
        combine_stages=_duplication_stages(evidence, cell, where),
        inf=evidence.inf,
        clamp_inf=not cell.benign_informative_only,
        inf_bound=cell.inf,
        parent_bound=cell.parent,
    )


@dataclasses.dataclass(frozen=True)
class MissensePaths:
    """The missense workflow's paths (SM6): the amino-acid one, and the splice one where it applies.

    The two are supplied together because `classify` selects between them by the max-path rule, which
    needs both scored. A nucleotide change with no predicted splice effect has the amino-acid path
    alone.

    Attributes:
        amino_acid: The amino-acid (green) path.
        splice: The splice path, when the nucleotide change may affect splicing.
    """

    amino_acid: AminoAcidEvidence
    splice: SpliceEvidence | None = None

    @property
    def releases(self) -> tuple[provenance.Release, ...]:
        """The releases behind both paths."""
        if self.splice is None:
            return self.amino_acid.releases
        return provenance.union(self.amino_acid.releases, self.splice.releases)


VariantEvidence = MissensePaths | NullEvidence | CodingEvidence | SpliceEvidence | DuplicationEvidence
"""The evidence families the workflows take, one per shape of predictive path."""


def _independent(codes: IndependentCodes | None) -> list[classify.ScoredCode]:
    if codes is None:
        return []
    if isinstance(codes, Mapping):
        return [classify.IndependentCode(code, points) for code, points in codes.items()]
    return list(codes)


def build_missense(
    ref: reference.Reference,
    *,
    amino_acid: AminoAcidEvidence,
    splice: SpliceEvidence | None = None,
    independent_codes: IndependentCodes | None = None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.ClassificationInput:
    """Build a missense classification: the amino-acid path and (optionally) the splice path (SM6).

    When a splice path is supplied, both go to `classify`, which selects between them by the max-path
    rule; the amino-acid path is used alone otherwise.

    Args:
        ref: The loaded reference.
        amino_acid: The amino-acid (green) path judgement inputs.
        splice: The splice-path inputs, when the nucleotide change may affect splicing.
        independent_codes: POP/CLN/LOC codes already reduced to points, keyed by code.
        gate_level: The gene-disease-validity gate level (for the gate and the mechanism
            precondition); `gene_disease_validity.gate_level` maps a curator's classification onto one.

    Returns:
        The `ClassificationInput` to pass to `classify.classify`.

    Raises:
        ValueError: On a judgement input outside the bound its splice cell states, or a
            critical-residue award SM7 withholds.
        splice_tree.ReconsiderEvidenceError: If the splice assay contradicts the colour path, which
            is a routing verdict to act on rather than a value to score.
    """
    paths = [_amino_acid_path(ref, amino_acid)]
    releases = amino_acid.releases
    if splice is not None:
        paths.append(_splice_path(ref, splice, flow=splice_tree.SpliceFlow.PREDICTED))
        releases = provenance.union(releases, splice.releases)
    return classify.ClassificationInput(paths, _independent(independent_codes), gate_level, releases)


def build_nonsense(
    ref: reference.Reference,
    *,
    null: NullEvidence,
    independent_codes: IndependentCodes | None = None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.ClassificationInput:
    """Build a nonsense classification: the NUL PFD path (SM8)."""
    return classify.ClassificationInput(
        [_null_path(ref, null, whole_gene=False)], _independent(independent_codes), gate_level, null.releases
    )


def build_frameshift(
    ref: reference.Reference,
    *,
    null: NullEvidence,
    independent_codes: IndependentCodes | None = None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.ClassificationInput:
    """Build a frameshift classification: the NUL PFD path (SM9), evaluated at the PTC position."""
    return classify.ClassificationInput(
        [_null_path(ref, null, whole_gene=False)], _independent(independent_codes), gate_level, null.releases
    )


def build_exon_deletion(
    ref: reference.Reference,
    *,
    null: NullEvidence,
    whole_gene: bool = False,
    independent_codes: IndependentCodes | None = None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.ClassificationInput:
    """Build a single-/multi-exon deletion classification: the NUL PFD path (SM13).

    `whole_gene` selects the whole-gene-ablation branch (mechanism-only scaling, no exon axis; the
    initial tier is +10). Subgenic deletions scale by the full mechanism x exon matrix.
    """
    return classify.ClassificationInput(
        [_null_path(ref, null, whole_gene=whole_gene)], _independent(independent_codes), gate_level, null.releases
    )


def build_canonical_splice(
    ref: reference.Reference,
    *,
    splice: SpliceEvidence,
    independent_codes: IndependentCodes | None = None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.ClassificationInput:
    """Build a canonical-splice classification: the SPL PFD path (SM11).

    This is the only flow whose entry condition is positional (wild-type GT at donor +1,+2 or AG at
    acceptor -2,-1), so it is the only one whose NMD tier starts at +6.0 and whose splice assay can
    only reduce the tier.

    Raises:
        ValueError: On a judgement input outside the bound its splice cell states.
        splice_tree.ReconsiderEvidenceError: If the splice assay contradicts the colour path, which
            is a routing verdict to act on rather than a value to score.
    """
    return classify.ClassificationInput(
        [_splice_path(ref, splice, flow=splice_tree.SpliceFlow.CANONICAL)],
        _independent(independent_codes),
        gate_level,
        splice.releases,
    )


def build_intronic_synonymous(
    ref: reference.Reference,
    *,
    splice: SpliceEvidence,
    independent_codes: IndependentCodes | None = None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.ClassificationInput:
    """Build an intronic/synonymous classification: the SPL PFD path (SM12).

    SM12 reaches a splice effect through an in-silico prediction, as SM6's splice branch does, so
    the path shares the predicted flow's tiers and amplifying splice assay.

    Raises:
        ValueError: On a judgement input outside the bound its splice cell states.
        splice_tree.ReconsiderEvidenceError: If the splice assay contradicts the colour path, which
            is a routing verdict to act on rather than a value to score.
    """
    return classify.ClassificationInput(
        [_splice_path(ref, splice, flow=splice_tree.SpliceFlow.PREDICTED)],
        _independent(independent_codes),
        gate_level,
        splice.releases,
    )


def build_inframe_indel(
    ref: reference.Reference,
    *,
    coding: CodingEvidence,
    independent_codes: IndependentCodes | None = None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.ClassificationInput:
    """Build an in-frame indel classification: the CDS PFD path (SM10)."""
    return classify.ClassificationInput(
        [_coding_path(ref, coding)], _independent(independent_codes), gate_level, coding.releases
    )


def build_start_lost(
    ref: reference.Reference,
    *,
    coding: CodingEvidence,
    independent_codes: IndependentCodes | None = None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.ClassificationInput:
    """Build a start-lost classification: the CDS PFD path (SM15).

    Note: SM15's c.1A>C / CTG-initiator informative-variant suppression is the caller's to apply to
    the CDS_INF points it passes; it is not a cap this builder bakes.
    """
    return classify.ClassificationInput(
        [_coding_path(ref, coding)], _independent(independent_codes), gate_level, coding.releases
    )


def build_stop_lost(
    ref: reference.Reference,
    *,
    coding: CodingEvidence,
    independent_codes: IndependentCodes | None = None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.ClassificationInput:
    """Build a stop-lost classification: the CDS PFD path (SM16), for the extension (non-NSD) branch.

    The NSD (yellow) branch is a NUL path (+4 initial); build it with `build_nonsense`/the NUL
    evidence instead. This builder covers the orange extension branch, which is coding (CDS).
    """
    return classify.ClassificationInput(
        [_coding_path(ref, coding)], _independent(independent_codes), gate_level, coding.releases
    )


def build_exon_duplication(
    ref: reference.Reference,
    *,
    duplication: DuplicationEvidence,
    independent_codes: IndependentCodes | None = None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.ClassificationInput:
    """Build a single-/multi-exon duplication/gain classification (SM14).

    Covers all six scored paths, `NUL_` and `CDS_` alike: `duplication.path` selects the family and
    the cell whose bounds the path is held to, including the parent and INF caps the not-tandem paths
    hold tighter than their family's. A whole-gene duplication is not point-scored (SM14 awards
    `CDS_PRD_NA` through `CDS_NA`), so it has no path here.

    Raises:
        ValueError: On a judgement input outside the bound its cell states, functional points on a
            path that scores `FXN_NA`, or a pathogenic informative variant on a benign-only module.
    """
    return classify.ClassificationInput(
        [_duplication_path(ref, duplication)], _independent(independent_codes), gate_level, duplication.releases
    )


_ADMITTED: Mapping[evidence_pb2.Consequence, frozenset[type[VariantEvidence]]] = {
    evidence_pb2.CONSEQUENCE_MISSENSE: frozenset({MissensePaths}),
    evidence_pb2.CONSEQUENCE_NONSENSE: frozenset({NullEvidence, CodingEvidence}),
    evidence_pb2.CONSEQUENCE_FRAMESHIFT: frozenset({NullEvidence, CodingEvidence}),
    evidence_pb2.CONSEQUENCE_EXON_DELETION: frozenset({NullEvidence, CodingEvidence}),
    evidence_pb2.CONSEQUENCE_START_LOST: frozenset({NullEvidence, CodingEvidence}),
    evidence_pb2.CONSEQUENCE_STOP_LOST: frozenset({NullEvidence, CodingEvidence}),
    evidence_pb2.CONSEQUENCE_CANONICAL_SPLICE: frozenset({SpliceEvidence}),
    evidence_pb2.CONSEQUENCE_INTRONIC: frozenset({SpliceEvidence}),
    evidence_pb2.CONSEQUENCE_SYNONYMOUS: frozenset({SpliceEvidence}),
    evidence_pb2.CONSEQUENCE_INFRAME_INDEL: frozenset({CodingEvidence}),
    evidence_pb2.CONSEQUENCE_EXON_DUPLICATION: frozenset({DuplicationEvidence}),
}


def _consequence_name(consequence: evidence_pb2.Consequence) -> str:
    if type(consequence) is int and consequence in evidence_pb2.Consequence.values():
        return evidence_pb2.Consequence.Name(consequence)
    return repr(consequence)


def _admitted(consequence: evidence_pb2.Consequence) -> frozenset[type[VariantEvidence]]:
    """The families the consequence's workflow admits, refusing one that reaches no workflow."""
    if type(consequence) is not int:
        raise ValueError(
            f'the consequence must be an evidence_pb2.Consequence value, got a {type(consequence).__name__}; '
            "it is what Variant.Normalize re-encodes VEP's term onto"
        )
    if consequence == evidence_pb2.CONSEQUENCE_NON_CODING:
        raise ValueError(
            'SM17, the non-coding variant workflow, is unreleased, so this framework scores no non-coding '
            'variant; there is no path to route one onto'
        )
    if consequence == evidence_pb2.CONSEQUENCE_UNSPECIFIED:
        raise ValueError(
            'the consequence is unresolved, and it is what selects the decision tree; Variant.Normalize is '
            'what resolves it'
        )
    admitted = _ADMITTED.get(consequence)
    if admitted is None:
        raise ValueError(f'no SVCv4 workflow is routed to for consequence {_consequence_name(consequence)}')
    return admitted


def for_consequence(
    ref: reference.Reference,
    consequence: evidence_pb2.Consequence,
    evidence: VariantEvidence,
    *,
    independent_codes: IndependentCodes | None = None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.ClassificationInput:
    """Route a variant onto its workflow by the consequence, and build that workflow's path(s).

    The routing is the framework's own: SVCv4 picks a decision tree by molecular consequence, and the
    agent holds that consequence typed on the wire. What the consequence does not settle on the LoF
    trees is which arm of the fork the variant took — NMD or not, rescued or not — and the evidence
    family answers that, so an `evidence` of the wrong family for the consequence is refused rather
    than routed to the workflow it happens to fit.

    **The whole-gene-ablation branch is not reachable here**, and `build_exon_deletion(whole_gene=True)`
    is its route: it is not a fork of the exon-deletion tree but a tier table of its own, so reading
    it off an absent exon-relevance call would score a subgenic deletion as an ablation.

    Args:
        ref: The loaded reference.
        consequence: The routing consequence, as `Variant.Normalize` and `Vep.Annotate` state it.
        evidence: The workflow's judgement inputs, in the family its arm takes.
        independent_codes: The POP/CLN/LOC codes, as a mapping of points or as scored codes.
        gate_level: The gene-disease-validity gate level.

    Returns:
        The `ClassificationInput` to pass to `classify.classify`.

    Raises:
        ValueError: On a consequence that reaches no workflow — an unresolved one, and a non-coding
            one, whose supplement is unreleased — on an evidence family the consequence's workflow
            does not admit, and on anything the workflow's own builder refuses.
    """
    admitted = _admitted(consequence)
    if isinstance(evidence, MissensePaths) and MissensePaths in admitted:
        return build_missense(
            ref,
            amino_acid=evidence.amino_acid,
            splice=evidence.splice,
            independent_codes=independent_codes,
            gate_level=gate_level,
        )
    if isinstance(evidence, NullEvidence) and NullEvidence in admitted:
        return _built([_null_path(ref, evidence, whole_gene=False)], evidence, independent_codes, gate_level)
    if isinstance(evidence, CodingEvidence) and CodingEvidence in admitted:
        return _built([_coding_path(ref, evidence)], evidence, independent_codes, gate_level)
    if isinstance(evidence, SpliceEvidence) and SpliceEvidence in admitted:
        flow = (
            splice_tree.SpliceFlow.CANONICAL
            if consequence == evidence_pb2.CONSEQUENCE_CANONICAL_SPLICE
            else splice_tree.SpliceFlow.PREDICTED
        )
        return _built([_splice_path(ref, evidence, flow=flow)], evidence, independent_codes, gate_level)
    if isinstance(evidence, DuplicationEvidence) and DuplicationEvidence in admitted:
        return _built([_duplication_path(ref, evidence)], evidence, independent_codes, gate_level)
    raise ValueError(
        f'a {type(evidence).__name__} does not score a {_consequence_name(consequence)} variant; that '
        f'workflow takes {sorted(family.__name__ for family in admitted)}'
    )


def _built(
    paths: list[scoring.PathInput],
    evidence: VariantEvidence,
    independent_codes: IndependentCodes | None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.ClassificationInput:
    return classify.ClassificationInput(paths, _independent(independent_codes), gate_level, evidence.releases)


def classify_variant(
    ref: reference.Reference,
    *,
    consequence: evidence_pb2.Consequence,
    evidence: VariantEvidence,
    independent_codes: IndependentCodes | None = None,
    gate_level: gene_disease_pb2.GateLevel,
) -> classify.Classification:
    """Route a variant onto its workflow and score it, in one call.

    The framework picks a decision tree by molecular consequence and the agent holds that consequence
    typed, so naming the workflow by hand is a step that can only be got wrong. This is
    `for_consequence` and `classify.classify` together, over the same path assembly the named
    builders use. Three of the arms it routes to have no named builder of their own — the CDS_ arm a
    nonsense, frameshift or exon-deletion variant takes when a rescue or an escape from NMD puts it
    there — since the six `build_*` functions are named for the supplement each family's arm is
    stated in.

    Args:
        ref: The loaded reference.
        consequence: The routing consequence, as `Variant.Normalize` and `Vep.Annotate` state it.
        evidence: The workflow's judgement inputs, in the family its arm of the tree takes.
        independent_codes: The POP/CLN/LOC codes, as a mapping of points or as scored codes.
        gate_level: The gene-disease-validity gate level; `gene_disease_validity.gate_level` maps a
            curator's classification onto one.

    Returns:
        The `classify.Classification`.

    Raises:
        ValueError: On everything `for_consequence` and `classify.classify` refuse — a consequence
            that reaches no workflow, an evidence family it does not admit, a judgement input outside
            a bound its cell states, or a code the framework did not determine.
    """
    return classify.classify(
        ref,
        for_consequence(ref, consequence, evidence, independent_codes=independent_codes, gate_level=gate_level),
    )
