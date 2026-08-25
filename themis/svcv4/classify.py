"""The top-level model-facing SVCv4 API: judgement inputs and evidence in, tally and band out.

This is what the agent calls in code mode. It assembles the pieces the other modules compute into
one reproducible classification:

  - one or two variant-type decision-tree paths (two for missense: the amino-acid and splice
    paths, compared by the max-path rule; one for every other variant type), each scored by
    `scoring.score_path`;
  - the independent evidence codes already reduced to points (POP_FRQ, POP_HMZ,
    CLN_*, LOC_*), each clamped to its per-code range, with the LOC family held to the reference's
    LOC concept cap and the clinical codes SM4 conditions on POP_FRQ refused outside the assignments
    it admits — a POP_FRQ the framework did not determine among them;
  - the gene-disease-validity gate cap on the resulting class.

Inputs the MODEL must supply (judgement): the decision-tree path(s) with their initial predictive
tiers, the mechanism level and exon-relevance call per path, the FXN/SPA/INF points, and the
independent-code points (POP/CLN/LOC). Inputs that are RETRIEVED: the gene-disease-validity gate level of
the chosen MDE, which `GeneDisease.DescribeGene` states per curated entity. This module supplies neither —
it only combines, deterministically, what it is handed.

Each of those inputs arrives already scored: the doors in `frequency`, `predictor_policy`,
`functional`, `splice_tree` and `nmd` turn a service's own answer into a `ScoredCode` or a value a
path takes, so nothing here reads a payload. Assembling the paths is `builders`', and
`builders.classify_variant` is routing and scoring in one call — the entry point for one variant.
"""

from __future__ import annotations

import collections
import dataclasses
import decimal
from collections.abc import Sequence
from typing import Protocol

from themis.rpc import gene_disease_pb2
from themis.svcv4 import provenance, reference, scoring

_LOC_CODES = ('LOC_PHE', 'LOC_SEG')


class ScoredCode(Protocol):
    """One evidence code reduced to points, in the shape every door returns it.

    Structural rather than a base class: a door's value object carries its own fields beside these
    four — `frequency.PopFrq` the FAF and the threshold it was binned against,
    `predictors.PredictorScore` the score and the predictor that produced it — and the tally reads
    only these.

    Attributes:
        code: The evidence code the points are filed under. Usually one the reference names; SM20's
            functional code is family-agnostic (`^^^_FXN`) because the family is fixed by the path
            the points are handed to rather than by the assay.
        points: The points, or None where the framework made no determination — a POP_HMZ below
            SM3's observation floor, a POP_FRQ over a FAF that could not be scored. A not-determined
            code is left out of the tally rather than filed at zero, which would assert it was
            assessed and contributed nothing.
        derivation: What the points were read from, for the audit trail.
        releases: The upstream releases the value rests on; empty where no retrieval produced it.
    """

    @property
    def code(self) -> str: ...

    @property
    def points(self) -> decimal.Decimal | None: ...

    @property
    def derivation(self) -> str: ...

    @property
    def releases(self) -> tuple[provenance.Release, ...]: ...


@dataclasses.dataclass(frozen=True)
class IndependentCode:
    """An evidence code already reduced to points, scored outside the variant-type path.

    Covers the codes with no matrix/combine interaction: POP_FRQ, POP_HMZ, CLN_*, LOC_*. The points
    are clamped to the code's per-code range from the reference. Which families those are is the
    reference's to state (`reference.Reference.independent_families`), so the family is held on the
    way into the tally, where the reference is at hand, rather than here.

    What a door returns already satisfies `ScoredCode`, so this is the constructor for a code the
    caller reduced itself — a clinical or locus observation the analyst scored off SM4's tables.
    """

    code: str
    points: decimal.Decimal
    derivation: str = ''
    releases: tuple[provenance.Release, ...] = ()

    def __post_init__(self) -> None:
        # Checked at runtime because the model composes this untyped in code mode, where the two
        # slips are a not-determined finding's None and a whole finding passed instead of its points.
        if self.points is None:
            raise ValueError(
                f'{self.code} was not determined; a code the framework did not determine is left out of '
                'the tally, not filed at zero'
            )
        if not isinstance(self.points, decimal.Decimal):
            raise ValueError(f'{self.code} points must be a Decimal, got {type(self.points).__name__} {self.points!r}')


@dataclasses.dataclass(frozen=True)
class ClassificationInput:
    """Everything needed to score one variant for one MDE.

    Attributes:
        variant_type_paths: The scored variant-type path(s): `[amino_acid, splice]` for missense
            (compared by the max-path rule), or a single path for any other variant type.
        independent_codes: The POP/CLN/LOC codes already reduced to points, one entry per code —
            a code covering several observations carries their summed points.
        gate_level: The gene-disease-validity gate level, for the gate cap and the mechanism
            precondition. A curator's classification is not one — `gene_disease_validity.gate_level`
            maps it.
        releases: The releases behind the values on the path(s). The independent codes carry their
            own, so this is what the path builders collect off the evidence they were handed —
            a path reduces its doors' answers to points before `classify` sees them.
    """

    variant_type_paths: Sequence[scoring.PathInput]
    independent_codes: Sequence[ScoredCode]
    gate_level: gene_disease_pb2.GateLevel
    releases: tuple[provenance.Release, ...] = ()


@dataclasses.dataclass(frozen=True)
class Classification:
    """The transparent SVCv4 result: the audit trail, the total, and the (gated) class band.

    Attributes:
        contributions: Additive per-line trail; the points column sums to `total`, cap lines
            included — each carries the adjustment it made, never the bounded value (`cap_line`).
        selected_path: The scored variant-type path that was used (post max-path selection).
        alternate_path: The non-selected missense path, retained for re-evaluation (None otherwise).
        total: The summed point total.
        band: The pre-gate class band (B/LB/VUS/LP/P).
        vus_subband: The VUS sub-band when `band` is VUS, else None.
        final_class: The class after the gene-disease-validity gate cap (may be a terminal gate
            result such as "Variant in Gene of Uncertain Significance").
        gate_capped: Whether the gate changed the class.
        releases: Every upstream release the tally rests on, de-duplicated: the union over the
            independent codes and the paths. A total is reproducible only against the releases its
            retrievals were made at, so they travel with it rather than being re-derived from the
            responses afterwards.
    """

    contributions: tuple[scoring.Contribution, ...]
    selected_path: scoring.PathResult | None
    alternate_path: scoring.PathResult | None
    total: decimal.Decimal
    band: str
    vus_subband: str | None
    final_class: str
    gate_capped: bool
    releases: tuple[provenance.Release, ...] = ()


def _validity_at_least_moderate(ref: reference.Reference, gate_level: gene_disease_pb2.GateLevel) -> bool:
    """A level is Moderate-or-higher iff its gate permits LP (SM18 mechanism precondition)."""
    return 'LP' in scoring.gate_row(ref, gate_level).allows


def _check_mechanism_precondition(
    ref: reference.Reference, path: scoring.PathInput, gate_level: gene_disease_pb2.GateLevel
) -> None:
    """Fail loud if a positive mechanism multiplier is claimed below Moderate validity (SM18).

    Mechanism > Uncertain requires gene-disease validity Moderate or higher; a path that scales by
    mechanism with Established/Likely/Suspected under a lesser level is a contradiction the model
    must resolve, not something to silently score.
    """
    uses_mechanism = path.scaling in (scoring.Scaling.MECHANISM_AND_EXON, scoring.Scaling.MECHANISM_ONLY)
    positive_mechanism = path.mechanism in (
        scoring.MechanismLevel.ESTABLISHED,
        scoring.MechanismLevel.LIKELY,
        scoring.MechanismLevel.SUSPECTED,
    )
    if uses_mechanism and positive_mechanism and not _validity_at_least_moderate(ref, gate_level):
        raise ValueError(
            f'path {path.label!r} claims mechanism {path.mechanism.value if path.mechanism else None!r} '
            f'but gene-disease validity {reference.gate_level_name(gate_level)} is below Moderate (SM18)'
        )


def _determined(codes: Sequence[ScoredCode]) -> list[IndependentCode]:
    """The codes as tally lines, refusing one the framework made no determination for.

    A code with no points is a finding — SM3 determines no POP_HMZ below two eligible observations —
    and it has no tally line: a line reading `POP_HMZ 0.0` asserts the code was assessed and
    contributed nothing. So it is refused rather than filed at zero, naming what the door concluded,
    since a finding the framework did not determine is the caller's to report and not the tally's to
    carry.
    """
    determined = []
    for entry in codes:
        if entry.points is None:
            derivation = f' ({entry.derivation})' if entry.derivation else ''
            raise ValueError(
                f'{entry.code} was not determined{derivation}; a code the framework did not determine is '
                'left out of the tally, not filed at zero'
            )
        determined.append(
            IndependentCode(code=entry.code, points=entry.points, derivation=entry.derivation, releases=entry.releases)
        )
    return determined


def _check_filed_once(codes: Sequence[ScoredCode]) -> None:
    """Fail loud on a code filed twice, which reaches the tally scored twice over.

    Every entry is clamped to its per-code range on its own and the clamped values are then summed,
    so two POP_FRQ lines at -6 arrive as -12 against a stated range of [-6, 0]: the range bounds a
    line, not the code. Nor does the rarity precondition SM4 conditions the clinical codes on
    notice — it reads the POP_FRQ assignments as a set, which -6 twice over and -6 once are alike in.

    A code covering several observations is one line whose points the caller summed first;
    `observations.total` is that arithmetic.
    """
    filings = collections.Counter(entry.code for entry in codes)
    repeated = sorted(code for code, count in filings.items() if count > 1)
    if repeated:
        raise ValueError(
            f'{", ".join(repeated)} filed more than once; a code takes one tally line, carrying the points '
            'summed over the observations behind it (observations.total does that arithmetic) — a second '
            'line puts the code past the range that bounds it'
        )


def _check_independent_families(ref: reference.Reference, codes: Sequence[ScoredCode]) -> None:
    """Fail loud on a code of a family a variant-type path carries, filed as an independent one.

    A path's code is bounded twice over on the path — by its family's concept cap over PRD + FXN,
    and by the category cap over the parent total — and an independent code meets neither: it is
    clamped to its per-code range and added to the tally. So the same points the parent cap would
    cut back on the path score in full beside it, a band higher on identical evidence. Refused
    rather than trimmed, since nothing on this side reproduces the caps it escaped.
    """
    for entry in codes:
        family = ref.code(entry.code).family
        if family not in ref.independent_families:
            raise ValueError(
                f'{entry.code} is a {family} code, which the {family}_ variant-type path carries under its own '
                f'caps; score it there rather than as an independent code. The independent codes are the '
                f'{", ".join(sorted(ref.independent_families))} families'
            )


def _check_clinical_precondition(ref: reference.Reference, codes: Sequence[ScoredCode]) -> None:
    """Fail loud on a clinical code awarding points outside the POP_FRQ assignments SM4 conditions it on.

    SM4 orders the clinical assessment after population frequency because its scoring tables are
    conditioned on the POP_FRQ points already assigned; outside those the framework withdraws the
    code rather than reducing it. A tally carrying no POP_FRQ at all has not established the
    precondition either, and scoring the code there would read the absence as a rare variant.

    What the condition governs is awarding points, so a conditioned code at zero passes: SM4 assigns
    that to a proband its tables weigh at nothing, and refusing it would reward leaving an assessed
    code out of the tally. The POP_FRQ value is read as the caller passed it, not as the per-code
    range would clamp it — a value outside that range is a caller slip, and clamping it first would
    turn one into the rarity the gate is looking for.

    A POP_FRQ the framework did not determine does not satisfy the condition either, so the gate
    needs the finding and not only its points. Neither SM4 nor the ClinGen calculator says what an
    unscoreable frequency — one over gnomAD calls that failed variant QC — does to the condition;
    the rule that settles it is the library's own, that a frequency which could not be scored is no
    determination and 0.0 belongs to the variant that is genuinely rare. Reading the points alone
    would pass the commonest variant in the tally as the rarest.
    """
    precondition = ref.clinical_pop_frq_precondition
    awarded = sorted({entry.code for entry in codes if entry.points} & precondition.conditioned_codes)
    if not awarded:
        return
    admissible = ', '.join(str(points) for points in sorted(precondition.admissible_points))
    filed = [entry for entry in codes if entry.code == 'POP_FRQ']
    if not filed:
        raise ValueError(
            f'{", ".join(awarded)} awards points with no POP_FRQ in the tally; SM4 conditions it on a POP_FRQ '
            f'of {admissible}, assessed first'
        )
    scored: set[decimal.Decimal] = set()
    for entry in filed:
        if entry.points is None:
            derivation = f' ({entry.derivation})' if entry.derivation else ''
            raise ValueError(
                f'{", ".join(awarded)} awards points under a POP_FRQ the framework did not determine'
                f'{derivation}; SM4 conditions it on a POP_FRQ of {admissible}, and a frequency that could '
                'not be scored is neither one of them nor the rarity they stand for'
            )
        scored.add(entry.points)
    withdrawn = sorted(scored - precondition.admissible_points)
    if withdrawn:
        raise ValueError(
            f'{", ".join(awarded)} awards points at POP_FRQ {", ".join(str(points) for points in withdrawn)}; '
            f'SM4 conditions it on {admissible} and withdraws the code outside them'
        )


def _score_variant_type(
    ref: reference.Reference, paths: Sequence[scoring.PathInput]
) -> tuple[scoring.PathResult, scoring.PathResult | None]:
    """Score the variant-type path(s) and apply the missense max-path selection.

    Returns:
        `(selected, alternate)`; `alternate` is the non-selected missense path, else None.
    """
    if len(paths) == 1:
        return scoring.score_path(ref, paths[0]), None
    if len(paths) == 2:
        amino_acid, splice = (scoring.score_path(ref, p) for p in paths)
        return scoring.select_path(amino_acid, splice)
    raise ValueError(f'expected 1 path, or 2 for missense; got {len(paths)}')


def _independent_contributions(
    ref: reference.Reference, codes: Sequence[IndependentCode]
) -> tuple[list[scoring.Contribution], decimal.Decimal]:
    """Clamp each independent code to its per-code range and apply the LOC family cap.

    The LOC ceiling is the reference's LOC concept cap. Its floor is not applied: SM5 states the
    concept as 0.0 to +4.0 one paragraph after recommending -4.0 for a non-segregation, the same
    conflict `LOC_SEG`'s own range resolves in favour of the -4.0, and
    clamping the sum at 0.0 here would undo that a layer up.

    Returns:
        `(contributions, subtotal)`.
    """
    loc_ceiling = ref.concept_cap('LOC').high
    contributions = []
    subtotal = decimal.Decimal(0)
    loc_total = decimal.Decimal(0)
    for entry in codes:
        spec = ref.code(entry.code)
        clamped = scoring.clamp(entry.points, spec.low, spec.high)
        note = f'per-code cap [{spec.low}, {spec.high}]' if clamped != entry.points else ''
        contributions.append(
            scoring.Contribution(
                label=entry.code, raw_points=entry.points, points=clamped, note=note, basis=entry.derivation
            )
        )
        if entry.code in _LOC_CODES:
            loc_total += clamped
        else:
            subtotal += clamped
    if loc_total > loc_ceiling:
        contributions.append(
            scoring.cap_line('LOC combined cap', loc_total, loc_ceiling, f'LOC total capped at +{loc_ceiling}')
        )
        loc_total = loc_ceiling
    return contributions, subtotal + loc_total


def classify(ref: reference.Reference, request: ClassificationInput) -> Classification:
    """Combine judgement inputs and evidence into the SVCv4 point tally and class band.

    Args:
        ref: The loaded reference.
        request: The variant-type path(s), independent codes, and gate level.

    Returns:
        The `Classification` with the audit trail, total, band, and gated final class.

    Raises:
        ValueError: On an inconsistent mechanism/validity claim, a code the framework did not
            determine, a code filed more than once, an independent code of a family a variant-type
            path carries, a clinical code scored outside the POP_FRQ assignments SM4 conditions it
            on or under a POP_FRQ the framework did not determine, an unknown code or gate level, or
            a malformed path count.
    """
    for path in request.variant_type_paths:
        _check_mechanism_precondition(ref, path, request.gate_level)
    _check_filed_once(request.independent_codes)
    _check_independent_families(ref, request.independent_codes)
    _check_clinical_precondition(ref, request.independent_codes)
    # Last: the checks above have to see a not-determined finding, which this one turns into a refusal.
    codes = _determined(request.independent_codes)

    selected, alternate = _score_variant_type(ref, request.variant_type_paths)
    parent = scoring.Contribution(
        label=f'{selected.parent_code} ({selected.label})',
        raw_points=selected.raw_prd,
        points=selected.total,
        note=f'adjusted PRD {selected.adjusted_prd}; selected path',
    )
    independent, independent_subtotal = _independent_contributions(ref, codes)

    total = selected.total + independent_subtotal
    band, vus_subband = scoring.band_for_total(ref, total)
    gate = scoring.apply_gate(ref, band, request.gate_level)

    return Classification(
        contributions=(parent, *independent),
        selected_path=selected,
        alternate_path=alternate,
        total=total,
        band=band,
        vus_subband=vus_subband,
        final_class=gate.final_class,
        gate_capped=gate.capped,
        releases=provenance.union(request.releases, *(entry.releases for entry in codes)),
    )
