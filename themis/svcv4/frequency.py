"""Population-frequency evidence: DAFT computation, POP_FRQ binning, POP_HMZ (SM3).

POP_FRQ scores benignity from the filtering allele frequency (FAF) against a Disease Allele
Frequency Threshold (DAFT).

Which FAF, on both sides of that comparison — the variant's own and every FAF entering a DAFT — is
`joint.faf95.popmax`, the gnomAD v4 **joint** (exome + genome) Grpmax FAF. The ClinGen VCEP Review
Committee's gnomAD guidance (v3.0, 23 June 2025; the sentence is unchanged from v1.0) states it is
"the allele frequency VCEPs should use when applying BA1 and BS1 for a given variant". The
exome-only FAF is 0 for any variant called only in genomes, which reads back as absence and inverts
the evidence. Grpmax itself excludes the bottlenecked groups (Amish, Ashkenazi Jewish, European
Finnish, and the remaining individuals), which is why the founder handling below has less to do than
SM3's rule assumes.

Joint does not mean quality-screened, and that is a separate gate. gnomAD computes the joint FAF over
calls that failed variant QC — only chrM, `AS_lowqual` and `AC_raw == 0` are removed — so an
`AS_VQSR`-failing exome call sits in the numerator of a joint FAF that looks clean. The joint block's
own `filters` cannot catch it: on the browser API it is an alias for the joint *flags*
(`discrepant_frequencies` and the like), which structurally never carry a variant-QC filter, so
gating on it admits everything. The verdict is therefore read per callset, from `exome.filters` /
`genome.filters` — `Callset` and `joint_faf` below, and `faf_from_gnomad`, which reads all of it off
a `Gnomad.DescribeVariant` response so that no caller re-derives which path each figure comes from.

A filter-failing observation is not an absence: it counts *against* rarity but never *toward*
benignity (the asymmetry the Monogenic Diabetes VCEP states as GN085). So `Faf` carries its support
alongside its value; a filter-failing FAF determines no POP_FRQ, and such a variant anchors no DAFT.
gnomAD's `flags` ride along unscored, since they are caveats rather than QC verdicts (`lcr`,
`segdup`, `non_par`, `monoallelic`) — the reference names the first as a POP_FRQ caveat and the
analyst weighs it.

Neither code files a zero it did not determine, and that distinction is what every rarity gate
downstream turns on: SM4 conditions the clinical codes on the POP_FRQ *assignment*, so a zero
standing in for "unscoreable" passes those gates exactly as a rare variant would. POP_FRQ bins to
0.0 for a variant rarer than its threshold and for one absent from gnomAD — absence is no benignity
evidence — and returns no points at all where the FAF cannot be scored. `Faf.support` is what tells
those two zeros apart, and names why an unscoreable FAF could not be scored. POP_HMZ carries its
support the same way and declines on two states more: below SM3's floor of two eligible
observations, and under the penetrance/severity gate every row of SM3 Table 7 is conditioned on.

A VCEP that specifies its own frequency rule supersedes this one: the Monogenic Diabetes VCEP's BS1,
for instance, takes "the one with the larger denominator" across the exome and genome Grpmax FAFs
rather than the joint value. `Faf` is constructible directly for that case; `joint_faf` implements
the general rule.

Which records are "known P/LP variants" for the pathogenic-variants DAFT is `known_pathogenic`: an
unqualified pathogenic ClinVar classification (`clinvar_classification`, which owns the vocabulary
and states why this method reads it through a narrower gate than the pool's other consumers), plus
a caller-set review-status floor. **The floor is a required argument and the library supplies no
default**, and `KNOWN_PATHOGENIC_REVIEW_STATUS_FLOOR` is this project's frozen answer to it — 1
star, criteria provided — for a caller to pass explicitly. No registry-wide convention exists to
default to, and the two directions fail differently: admitting unreviewed records raises the DAFT
(SM3's err-high, conservative direction, withholding benign evidence), while a floor thins the pool
toward the >=10 the method needs and lowers the threshold, which is the direction that produces a
false benign call. Naming it per call is what keeps that auditable and keeps a VCEP with its own rule
from being silently overridden. The library takes it per call, refuses to invent one, and stamps it
on the `Daft` it returns.

The DAFT method hierarchy is SM3's own order, and it is not the automatable one. SM3 makes the
calculator the first choice and the pathogenic-variants method "generally the third choice", usable
only "if the prior two methods are not appropriate"; the reference data agrees
(`data.population.CALCULATOR.preferred_when`). Taking a later method where an earlier one's
preconditions hold changes the score, so the order is not cosmetic. Worked evidence for that and for
the floor: `docs/design/evidence-interfaces.md` §Worked evidence behind the library's contracts.

  1. A **curated VCEP DAFT** where one is published (`curated_daft`) — an expert-consensus threshold
     supersedes every computed one.
  2. The **calculator method** (`daft_calculator`; Whiffin/Ware maximum-credible-AF, PMID 28518168)
     on curator-supplied prevalence / heterogeneity / penetrance. First among the computed methods,
     and the empirical maximum below can *under*-estimate the max-credible AF for an under-studied
     gene, where SM3 asks for a high, conservative DAFT.
  3. The **binning method** (SM3 Tables 1-6, `binned_daft`) for X-linked and very sparse MDEs, on a
     curator-supplied prevalence and penetrance rounded onto the tables' coarse bins. The only route
     open to an X-linked MDE, which the calculator refuses.
  4. The **pathogenic-variants method** (`daft_from_pathogenic_variants`), where the gene has >=10
     known P/LP variants: the highest Grpmax FAF among them, after excluding a founder/outlier
     spike. The only method needing no hand-sourced epidemiology, which is why it is tempting and
     why the ranking has to be stated rather than inferred from what automates. ClinGen's current
     gnomAD guidance goes further than SM3's ordering: VCEPs whose rules rest "on using the AFs of
     known pathogenic variants as a comparator … will need to reassess these thresholds", and "the
     calculated frequency approach … should be used".

Every method returns a `Daft` carrying which one produced it and the inputs it rested on, so the
choice is recoverable from the result instead of being implied by the arguments a caller passed.

DAFT calculator conflict: SM3's worked FBN1 example states inputs {prevalence 1/5000, locus
heterogeneity 1.0, penetrance 0.85, allelic heterogeneity 0.10} give DAFT 0.000118. The
population-genetics formula reproduces 0.000118 only with allelic heterogeneity 1.0 (with 0.10 it
gives 1.18e-5); the example's stated 0.10 is inconsistent with its published output. `daft_calculator`
implements the correct formula (allelic heterogeneity in the numerator); a caller reproducing the
FBN1 threshold must pass allelic heterogeneity 1.0.
"""

from __future__ import annotations

import dataclasses
import decimal
import enum
from collections.abc import Iterable, Mapping, Sequence

from themis.evidence.models import evidence_pb2
from themis.rpc import clinvar_pb2, gnomad_pb2
from themis.svcv4 import clinvar_classification, payload, provenance, reference

# SM3: a founder/outlier variant is "significantly higher in frequency than other P/LP variants
# (e.g., >5x higher)"; the top observed FAF is peeled when it exceeds this multiple of the next.
_FOUNDER_OUTLIER_MULTIPLE = decimal.Decimal(5)
MIN_PATHOGENIC_VARIANTS = 10  # SM3's precondition for the method
MAX_REVIEW_STARS = 4  # ClinVar's gold-star scale, the range a review-status floor is stated on

# The frozen curation policy for this project's pathogenic-variants DAFT: a record needs ClinVar
# criteria (>= 1 star). Named rather than defaulted — the argument stays required, so a caller states
# which policy it applied instead of inheriting one silently, and a VCEP-specific floor passes
# straight through. The other half of "known pathogenic" is not a star count and is not here: a
# conflicting-classification record is excluded by `known_pathogenic`'s classification gate at every
# review status, since ClinVar rates such a record 1 star.
KNOWN_PATHOGENIC_REVIEW_STATUS_FLOOR = 1
_HMZ_OBSERVATION_FLOOR = 2  # SM3 Table 7 counts from the 2nd eligible observation
_SHOWN_DIGITS = 3  # significant digits a FAF/DAFT multiple is shown to in the trail


@dataclasses.dataclass(frozen=True)
class Callset:
    """One gnomAD callset's (exome or genome) call of a variant, with its variant-QC verdict.

    Every field is required: the filters are the input this module exists to stop being read from
    the wrong place, and defaulting them to "passed" is the silent failure it is guarding against.

    Attributes:
        allele_count: The callset's `ac`. 0 means it called no allele — the callset then contributes
            no numerator, so neither its frequency nor its filters bear on the joint FAF, and `AC0`
            is itself one of those filters: the dropped genotypes are out of the joint numerator, not
            merely flagged in it. Distinct from the callset block being absent entirely (`None` at
            `joint_faf`), which is gnomAD not having the variant in that callset at all.
        filters: The callset's own `filters`; empty is a pass. Read from `exome.filters` /
            `genome.filters`, never from `joint.filters` (see the module docstring).
        flags: The callset's own `flags` (`lcr`, `segdup`, `non_par`, `monoallelic`). Not filters:
            gnomAD keeps these calls, and a flag does not make the FAF unreadable. They ride onto
            the `Faf` because the SVCv4 reference names one as a POP_FRQ caveat ("beware low
            allele-number distortions (e.g. lcr flag)") — a judgement for the analyst, not a gate
            this module applies.
    """

    allele_count: int
    filters: tuple[str, ...]
    flags: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.allele_count < 0:
            raise ValueError(f'allele count must be non-negative, got {self.allele_count}')


class FafSupport(enum.Enum):
    """What a joint FAF rests on — the gate benign frequency evidence has to pass."""

    PASSING = 'passing'  # every callset carrying the variant passed variant QC
    FILTER_FAILING = 'filter_failing'  # at least one did not, and its calls are in the numerator
    ABSENT = 'absent'  # gnomAD holds no called allele of the variant
    # No frequency was established — the variant was not looked up, or the lookup failed. A real
    # state at the scale this runs: a gene pool of hundreds against a ~10 req/IP/min upstream is not
    # fully resolved, and the alternative is a caller filing those variants as ABSENT, which asserts
    # a fact about gnomAD nobody checked.
    UNKNOWN = 'unknown'


# The support states that carry no frequency at all, so a non-zero value under one is contradictory.
_NO_FREQUENCY = frozenset({FafSupport.ABSENT, FafSupport.UNKNOWN})


@dataclasses.dataclass(frozen=True)
class Faf:
    """A variant's filtering allele frequency and the QC support behind it.

    The support is not decoration: 0.00131 computed over `AS_VQSR`-failing calls and 0.00131 over
    passing ones are different evidence, and a bare number cannot tell a caller which it holds.

    Attributes:
        value: The joint Grpmax FAF; 0 both for an absent variant and where gnomAD reports no Grpmax
            FAF (no genetic-ancestry group's 95% lower bound clears zero).
        support: Which calls the value rests on.
        flags: gnomAD's flags across the callsets carrying the variant — caveats on how to read the
            number (`lcr`, `segdup`, `non_par`, `monoallelic`), not a gate. Scoring ignores them and
            the analyst does not: the SVCv4 reference lists low-allele-number distortion under
            `lcr` as a POP_FRQ caveat, and a flag dropped here is one the report cannot mention.
        releases: The gnomAD releases the figure rests on, where `faf_from_gnomad` read it off a
            response; empty for a `Faf` the caller built itself.
    """

    value: decimal.Decimal
    support: FafSupport
    flags: tuple[str, ...]
    releases: tuple[provenance.Release, ...] = ()

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError(f'FAF must be non-negative, got {self.value}')
        if self.support in _NO_FREQUENCY and self.value != 0:
            raise ValueError(f'a {self.support.value} FAF carries no frequency, got {self.value}')

    @property
    def scoreable(self) -> bool:
        """Whether this FAF may be compared to a DAFT: a filtered or unestablished one may not.

        The two are not the same absence. A filtered call is an observation — gnomAD called the
        allele and its own QC rejected the call, which counts against rarity but never toward
        benignity (GN085) — where an unknown one is no lookup at all. Neither is a frequency the
        comparison can take, so neither yields POP_FRQ points.
        """
        return self.support in (FafSupport.PASSING, FafSupport.ABSENT)


def absent_faf() -> Faf:
    """The FAF of a variant gnomAD holds no record of — the `Gnomad` rpc's NOT_FOUND.

    Distinct from `joint_faf` so that an absent variant is stated rather than being what a caller
    gets for passing no frequency block at all, and from `unknown_faf` so that "gnomAD does not hold
    it" is never what a failed or skipped lookup reads as.
    """
    return Faf(value=decimal.Decimal(0), support=FafSupport.ABSENT, flags=())


def unknown_faf() -> Faf:
    """The FAF of a variant whose frequency was not established — not looked up, or the lookup failed.

    It enters no comparison and counts toward no precondition: `daft_from_pathogenic_variants` needs
    ten variants it could read, and `pop_frq` refuses to score one.
    """
    return Faf(value=decimal.Decimal(0), support=FafSupport.UNKNOWN, flags=())


def joint_faf(faf95_popmax: decimal.Decimal | None, *, exome: Callset | None, genome: Callset | None) -> Faf:
    """Read a variant's POP_FRQ FAF off its gnomAD v4 frequency block.

    Args:
        faf95_popmax: `joint.faf95.popmax`, or None where gnomAD reports no joint Grpmax FAF — a FAF
            of 0 for this comparison (a 95% lower bound that does not clear zero is no benign
            frequency evidence), not a missing input.
        exome: The `exome` block's allele count and filters, or None where the variant is not in
            gnomAD's exome callset at all (the block comes back null).
        genome: The same, for the `genome` block.

    Returns:
        The `Faf`: the joint value, and whether the calls behind it passed variant QC. A variant
        gnomAD holds a site but no called allele for (`AC0` in every callset it appears in) is
        `FafSupport.ABSENT`.

    Raises:
        ValueError: On a negative FAF; on both blocks absent (a variant gnomAD holds no record of
            never reaches here — the rpc answers NOT_FOUND — so it is a caller passing no payload;
            use `absent_faf`); or on a positive FAF over no called allele, which gnomAD cannot
            produce, so the arguments describe two different variants.
    """
    if faf95_popmax is not None and faf95_popmax < 0:
        raise ValueError(f'FAF must be non-negative, got {faf95_popmax}')
    if exome is None and genome is None:
        raise ValueError("joint_faf needs gnomAD's exome and/or genome block; for an absent variant use absent_faf()")
    value = faf95_popmax if faf95_popmax is not None else decimal.Decimal(0)
    carrying = [callset for callset in (exome, genome) if callset is not None and callset.allele_count > 0]
    if not carrying:
        if value > 0:
            raise ValueError(f'a joint FAF of {value} over no called allele in either callset')
        return absent_faf()
    flags = tuple(sorted({flag for callset in carrying for flag in callset.flags}))
    support = FafSupport.FILTER_FAILING if any(callset.filters for callset in carrying) else FafSupport.PASSING
    return Faf(value=value, support=support, flags=flags)


_JOINT_FAF95_POPMAX = 'variant.joint.faf95.popmax'
_CALLSETS = ('exome', 'genome')


def _callset(raw: dict[str, object], name: str) -> Callset | None:
    if payload.block(raw, f'variant.{name}') is None:
        return None
    return Callset(
        allele_count=payload.count(raw, f'variant.{name}.ac'),
        filters=payload.strings(raw, f'variant.{name}.filters'),
        flags=payload.strings(raw, f'variant.{name}.flags'),
    )


def faf_from_gnomad(response: gnomad_pb2.DescribeVariantResponse) -> Faf:
    """Read a variant's POP_FRQ FAF off a `Gnomad.DescribeVariant` response.

    Reads `variant.joint.faf95.popmax` for the figure, and `ac`, `filters` and `flags` under each of
    `variant.exome` and `variant.genome` for the QC verdict behind it. Never
    `variant.joint.filters`, which is an alias for the joint flags and carries no variant-QC filter
    at all (module docstring).

    Args:
        response: The rpc's answer. A variant gnomAD holds no record of does not arrive here — the
            rpc answers NOT_FOUND, which is `absent_faf`.

    Returns:
        The `Faf`, stamped with the gnomAD releases the response names.

    Raises:
        ValueError: If the payload does not carry one of the documented paths, or states the whole
            joint block as null — the upstream's shape has moved under a contract that still names
            it — or if the response states no provenance.
    """
    raw = payload.fields(response.raw)
    if payload.block(raw, 'variant.joint') is None:
        raise ValueError(
            'the response states no joint block, and POP_FRQ is scored on the joint Grpmax FAF; reading '
            'that as no Grpmax FAF would score the variant as rare on a block nobody published'
        )
    faf = joint_faf(
        payload.number(raw, _JOINT_FAF95_POPMAX),
        exome=_callset(raw, 'exome'),
        genome=_callset(raw, 'genome'),
    )
    return dataclasses.replace(faf, releases=provenance.releases_of(response.provenance))


@dataclasses.dataclass(frozen=True)
class ClassifiedVariant:
    """One ClinVar-classified variant of the gene, as the pathogenic-variants DAFT method reads it.

    Attributes:
        classification: The aggregate germline classification verbatim (`ClinVarRecord.classification`).
            Only an unqualified P/LP one makes the variant a known P/LP variant for this method.
        review_stars: The record's ClinVar gold-star review count, 0-4.
        faf: The variant's joint Grpmax FAF, from `joint_faf`.
    """

    classification: str
    review_stars: int
    faf: Faf

    def __post_init__(self) -> None:
        if not 0 <= self.review_stars <= MAX_REVIEW_STARS:
            raise ValueError(f'review stars must be 0-{MAX_REVIEW_STARS}, got {self.review_stars}')


class DaftMethod(enum.Enum):
    """Which SM3 method produced a DAFT, in SM3's own order of preference."""

    CURATED = 'curated'  # a published VCEP threshold
    CALCULATOR = 'calculator'  # SM3's first computed choice
    BINNING = 'binning'  # SM3 Tables 1-6
    PATHOGENIC_VARIANTS = 'pathogenic_variants'  # SM3's third choice


@dataclasses.dataclass(frozen=True)
class Daft:
    """A disease allele frequency threshold, with the method and inputs behind it.

    Two thresholds of the same value are not the same claim — one rests on a published VCEP
    consensus, another on ten gnomAD frequencies — and SM3 ranks the methods, so a caller and a
    report must be able to read which one was applied off the result itself.

    Attributes:
        value: The threshold; positive.
        method: Which SM3 method produced it.
        lower_bound: Whether the method could only bound the threshold from below — a
            pathogenic-variants maximum taken over a truncated pool. It is not a caveat to read past:
            a threshold that is too low raises the FAF/DAFT ratio and awards benign points the
            evidence does not support, so a caller must branch on it rather than parse `basis`.
        basis: The inputs it rested on, as the report quotes them (the calculator's parameters, the
            pathogenic-variants pool and its floor, the curator's citation).
        releases: The releases behind the retrievals the threshold rests on — the ClinVar pool and
            the gnomAD frequencies of the pathogenic-variants method. Empty for a method computed
            from curator-supplied epidemiology, which rests on no retrieval.
    """

    value: decimal.Decimal
    method: DaftMethod
    lower_bound: bool
    basis: str
    releases: tuple[provenance.Release, ...] = ()

    def __post_init__(self) -> None:
        if self.value <= 0:
            raise ValueError(f'DAFT must be positive, got {self.value}')
        if not self.basis.strip():
            raise ValueError(f'a {self.method.value} DAFT must state what it was derived from')


def curated_daft(value: decimal.Decimal, *, source: str) -> Daft:
    """A DAFT published by a VCEP, which supersedes every computed method (SM3).

    Args:
        value: The published threshold.
        source: The specification it is published in, e.g. "ClinGen Hearing Loss VCEP v3 (GN005)".
            Required: an expert-consensus threshold with no citation cannot be checked, and the
            report has to name where it came from.

    Returns:
        The `Daft`.

    Raises:
        ValueError: If `value` is not positive, or `source` is empty.
    """
    if not source.strip():
        raise ValueError('a curated VCEP DAFT must name its source')
    return Daft(
        value=value,
        method=DaftMethod.CURATED,
        lower_bound=False,
        basis=f'curated VCEP DAFT: {source.strip()}',
    )


class BinningTable(enum.Enum):
    """Which of SM3's six DAFT lookup tables a binned threshold is read off.

    The value is the title printed inside the image: SM3's own name for the table, and how a
    curation block names the one it read. Which table applies is the caller's judgement and not the
    library's — the MDE's inheritance, and for an X-linked one whether the frequency being scored is
    a male, female or combined-sex frequency; each table's `applies_to` in the reference states its
    case. SM3 prints no X-linked-recessive combined-sex table, so that observation has no binned
    route at all.
    """

    AUTOSOMAL_DOMINANT = 'AUTOSOMAL DOMINANT'
    AUTOSOMAL_RECESSIVE = 'AUTOSOMAL RECESSIVE'
    X_LINKED_MALE = 'X-LINKED DOMINANT OR RECESSIVE - MALE (sex-specific prevalence)'
    X_LINKED_DOMINANT_FEMALE = 'X-LINKED DOMINANT - FEMALE (sex-specific prevalence)'
    X_LINKED_RECESSIVE_FEMALE = 'X-LINKED RECESSIVE - FEMALE (sex-specific prevalence)'
    X_LINKED_DOMINANT_COMBINED = 'X-LINKED DOMINANT - COMBINED (combined male and female prevalence)'


def binned_daft(
    ref: reference.Reference,
    table: BinningTable,
    *,
    prevalence_denominator: int,
    penetrance: decimal.Decimal,
) -> Daft:
    """Read a DAFT off one of SM3's binning tables (Tables 1-6).

    SM3's route wherever the calculator does not apply — X-linked inheritance above all, which
    `daft_calculator` refuses outright. The method fixes locus and allelic heterogeneity at 1, so a
    table is keyed on prevalence and penetrance alone.

    An estimate is moved onto the axes by SM3's own rounding, which runs in opposite directions:
    prevalence rounds **up**, to the next more frequent bin and so a smaller denominator (an
    estimated 1/2,000 takes the 1/1,000 row); penetrance rounds **down**, to the next less penetrant
    column (0.65 takes the 50% column). Both err toward a higher DAFT, SM3's conservative direction
    for a benign call. An estimate past the coarse end of either axis — a disease more frequent than
    the first row, a penetrance below the last column — rounds onto no cell and is refused rather
    than clamped; a disease rarer than the last row rounds onto it like any other.

    Args:
        ref: The loaded reference, which carries the transcribed tables.
        table: Which of the six applies, the caller's judgement (`BinningTable`).
        prevalence_denominator: X in the prevalence "1 in X" of the phenotype for the whole MDE,
            lumping every gene associated with it. Which population it is measured over is the
            table's business: a sex-specific table takes the sex-specific prevalence.
        penetrance: Expected penetrance, a fraction in (0, 1].

    Returns:
        The `Daft`, its `basis` naming the table, the cell reached, and the estimates that reached
        it.

    Raises:
        ValueError: On a non-positive input, or one that rounds onto no row or column.
        reference.ReferenceDataError: If the reference does not carry `table`.
    """
    grid = ref.binning_grid(table.value)
    if prevalence_denominator <= 0:
        raise ValueError(f'prevalence denominator must be positive, got {prevalence_denominator}')
    if not decimal.Decimal(0) < penetrance <= decimal.Decimal(1):
        raise ValueError(f'penetrance must be in (0, 1], got {penetrance}')
    rows = [bin_ for bin_ in grid.prevalence_denominators if bin_ <= prevalence_denominator]
    if not rows:
        raise ValueError(
            f'a prevalence of 1/{prevalence_denominator:,} is more frequent than the coarsest bin of SM3 Table '
            f'{grid.number}, 1/{grid.prevalence_denominators[0]:,}, so it rounds up onto no row'
        )
    columns = [column for column in grid.penetrances if column <= penetrance]
    if not columns:
        raise ValueError(
            f'a penetrance of {penetrance} is below the least penetrant column of SM3 Table {grid.number}, '
            f'{grid.penetrances[-1]:.0%}, so it rounds down onto no column'
        )
    cell = (max(rows), max(columns))
    marker = ' — SM3 prints this cell with a "*" it defines nowhere' if cell in grid.marked else ''
    return Daft(
        value=grid.cells[cell],
        method=DaftMethod.BINNING,
        lower_bound=False,
        basis=(
            f'SM3 Table {grid.number} ({grid.title}): prevalence 1/{prevalence_denominator:,} -> the 1/{cell[0]:,} '
            f'bin, penetrance {penetrance} -> the {cell[1]:.0%} column{marker}'
        ),
    )


class Inheritance(enum.Enum):
    """Inheritance / affected-genotype state, for the DAFT calculator and POP_HMZ."""

    MONOALLELIC = 'monoallelic'  # autosomal dominant (also used for the semidominant monoallelic phenotype)
    BIALLELIC = 'biallelic'  # autosomal recessive
    XLINKED = 'xlinked'


class HmzInheritance(enum.Enum):
    """MDE inheritance for POP_HMZ point weighting (SM3 Table 7)."""

    AD = 'AD'
    SEMIDOMINANT = 'semidominant'
    AR = 'AR'
    XLINKED = 'xlinked'


_DAFT_INHERITANCE: dict[evidence_pb2.Inheritance, Inheritance] = {
    evidence_pb2.INHERITANCE_AUTOSOMAL_DOMINANT: Inheritance.MONOALLELIC,
    evidence_pb2.INHERITANCE_AUTOSOMAL_RECESSIVE: Inheritance.BIALLELIC,
    evidence_pb2.INHERITANCE_X_LINKED: Inheritance.XLINKED,
}

_HMZ_INHERITANCE: dict[evidence_pb2.Inheritance, HmzInheritance] = {
    evidence_pb2.INHERITANCE_AUTOSOMAL_DOMINANT: HmzInheritance.AD,
    evidence_pb2.INHERITANCE_AUTOSOMAL_RECESSIVE: HmzInheritance.AR,
    evidence_pb2.INHERITANCE_SEMIDOMINANT: HmzInheritance.SEMIDOMINANT,
    evidence_pb2.INHERITANCE_X_LINKED: HmzInheritance.XLINKED,
}

_BINNING_TABLES: dict[evidence_pb2.Inheritance, tuple[BinningTable, ...]] = {
    evidence_pb2.INHERITANCE_AUTOSOMAL_DOMINANT: (BinningTable.AUTOSOMAL_DOMINANT,),
    evidence_pb2.INHERITANCE_AUTOSOMAL_RECESSIVE: (BinningTable.AUTOSOMAL_RECESSIVE,),
    evidence_pb2.INHERITANCE_SEMIDOMINANT: (BinningTable.AUTOSOMAL_DOMINANT, BinningTable.AUTOSOMAL_RECESSIVE),
    evidence_pb2.INHERITANCE_X_LINKED: (
        BinningTable.X_LINKED_MALE,
        BinningTable.X_LINKED_DOMINANT_FEMALE,
        BinningTable.X_LINKED_RECESSIVE_FEMALE,
        BinningTable.X_LINKED_DOMINANT_COMBINED,
    ),
}


def _mode_name(mode: evidence_pb2.Inheritance) -> str:
    """Name an inheritance mode for an error message, however it was composed."""
    if type(mode) is int and mode in evidence_pb2.Inheritance.values():
        return evidence_pb2.Inheritance.Name(mode)
    return repr(mode)


def _curated(mode: evidence_pb2.Inheritance) -> evidence_pb2.Inheritance:
    """The mode, held to the contract's enum.

    Checked at runtime because the model composes this untyped in code mode, where a bool, a float
    and a `Decimal` all hash equal to a member and would resolve to whichever one they equal.
    """
    if type(mode) is not int:
        raise ValueError(
            f'the inheritance mode must be an evidence_pb2.Inheritance value, got a {type(mode).__name__}; '
            'it is what `GeneDisease.DescribeGene` states per curated entity'
        )
    return mode


def daft_inheritance(mode: evidence_pb2.Inheritance) -> Inheritance:
    """The DAFT calculator's inheritance for a curated entity's mode of inheritance.

    Args:
        mode: The entity's mode, as `GeneDisease.DescribeGene` states it.

    Returns:
        The `Inheritance` the calculator's two formulas are keyed on.

    Raises:
        ValueError: For a mode the calculator has no formula for. A semidominant MDE has a
            monoallelic and a biallelic phenotype at different frequencies, so which one is being
            scored is a judgement rather than a mapping; Y-linked, mitochondrial and undetermined
            modes reach no DAFT method at all. X-linked resolves here and `daft_calculator` then
            refuses it, which is SM3's own routing to the binning tables — `binning_tables_for`.
    """
    resolved = _DAFT_INHERITANCE.get(_curated(mode))
    if resolved is None:
        raise ValueError(
            f'the DAFT calculator has no formula for inheritance {_mode_name(mode)}; SM3 states one for a '
            'monoallelic and one for a biallelic MDE, and a semidominant MDE is scored on whichever of its '
            'two phenotypes the frequency is being compared for'
        )
    return resolved


def hmz_inheritance(mode: evidence_pb2.Inheritance) -> HmzInheritance:
    """The POP_HMZ weighting row for a curated entity's mode of inheritance (SM3 Table 7).

    Args:
        mode: The entity's mode, as `GeneDisease.DescribeGene` states it.

    Returns:
        The `HmzInheritance` whose per-observation weight Table 7 states.

    Raises:
        ValueError: For a mode Table 7 states no row for — Y-linked, mitochondrial, and the
            undetermined mode, which is the curator declining to state one.
    """
    resolved = _HMZ_INHERITANCE.get(_curated(mode))
    if resolved is None:
        raise ValueError(f'SM3 Table 7 states no per-observation weight for inheritance {_mode_name(mode)}')
    return resolved


def binning_tables_for(mode: evidence_pb2.Inheritance) -> tuple[BinningTable, ...]:
    """The SM3 binning tables a curated entity's mode of inheritance admits.

    A set rather than one table: SM3 separates X-linked dominant from recessive and male from female
    where the contract curates one X-linked mode, and prints no semidominant table at all. So the
    remaining choice — which sex stratum the frequency is measured over, and which of a semidominant
    MDE's two phenotypes is being scored — stays the caller's, over a set nothing outside it can be
    read off.

    Args:
        mode: The entity's mode, as `GeneDisease.DescribeGene` states it.

    Returns:
        The tables the mode admits, in SM3's own order.

    Raises:
        ValueError: For a mode SM3 prints no table for.
    """
    resolved = _BINNING_TABLES.get(_curated(mode))
    if resolved is None:
        raise ValueError(f'SM3 prints no DAFT binning table for inheritance {_mode_name(mode)}')
    return resolved


def daft_calculator(
    inheritance: Inheritance,
    *,
    prevalence_denominator: int,
    genetic_heterogeneity: decimal.Decimal,
    allelic_heterogeneity: decimal.Decimal,
    penetrance: decimal.Decimal,
) -> Daft:
    """Compute a DAFT by the calculator method (Whiffin maximum-credible-AF, PMID 28518168).

    SM3's first computed choice, so this is the method to reach for whenever the four parameters can
    be sourced; `daft_from_pathogenic_variants` is the fallback for a gene where they cannot.

    Monoallelic: `prevalence x genetic_het x allelic_het / (2 x penetrance)` (heterozygote frequency
    ~= 2 x allele frequency for a rare dominant allele). Biallelic: `allelic_het x sqrt(prevalence x
    genetic_het / penetrance)` (affected ~= q^2 x penetrance under Hardy-Weinberg). X-linked routes
    to the binning method per SM3 and is rejected here.

    Args:
        inheritance: The MDE's inheritance (monoallelic or biallelic).
        prevalence_denominator: X in a prevalence of "1 in X"; the smallest reasonable X gives the
            highest (most conservative) DAFT.
        genetic_heterogeneity: Max proportion of the phenotype attributable to this gene (locus
            heterogeneity); 1 assumes locus homogeneity.
        allelic_heterogeneity: Max proportion of the gene's disease attributable to a single allele.
        penetrance: Expected penetrance; the lowest reasonable value gives the highest DAFT.

    Returns:
        The `Daft`: a maximum credible allele frequency, with the parameters it was computed from.

    Raises:
        ValueError: On an out-of-range input or X-linked inheritance (use the binning method).
    """
    if prevalence_denominator <= 0:
        raise ValueError(f'prevalence denominator must be positive, got {prevalence_denominator}')
    for name, value in (
        ('penetrance', penetrance),
        ('genetic_heterogeneity', genetic_heterogeneity),
        ('allelic_heterogeneity', allelic_heterogeneity),
    ):
        if not decimal.Decimal(0) < value <= decimal.Decimal(1):
            raise ValueError(f'{name} must be in (0, 1], got {value}')
    prevalence = decimal.Decimal(1) / decimal.Decimal(prevalence_denominator)
    if inheritance is Inheritance.MONOALLELIC:
        value = prevalence * genetic_heterogeneity * allelic_heterogeneity / (decimal.Decimal(2) * penetrance)
    elif inheritance is Inheritance.BIALLELIC:
        value = allelic_heterogeneity * (prevalence * genetic_heterogeneity / penetrance).sqrt()
    else:
        raise ValueError('X-linked DAFT uses the binning method (SM3 Tables 3-6), not the calculator')
    return Daft(
        value=value,
        method=DaftMethod.CALCULATOR,
        lower_bound=False,
        basis=(
            f'calculator: {inheritance.value}, prevalence 1/{prevalence_denominator}, genetic heterogeneity '
            f'{genetic_heterogeneity}, allelic heterogeneity {allelic_heterogeneity}, penetrance {penetrance}'
        ),
    )


def known_pathogenic(variants: Iterable[ClassifiedVariant], *, review_status_floor: int) -> list[ClassifiedVariant]:
    """The supplied records that are known P/LP variants of the gene, in the order given.

    Two gates, both of which change the answer:

    - **The classification is an unqualified pathogenic one**
      (`clinvar_classification.is_unqualified_pathogenic`) — stricter than the gate the pool arrives
      filtered on, and stricter in the direction only a frequency claim needs. Anything else anchors
      the threshold on a variant whose frequency says nothing about a fully-penetrant pathogenic
      allele's: SERPINA1 PI*Z ("Pathogenic/Likely pathogenic; risk factor") sits near 1% in
      Europeans and would set that gene's threshold two orders of magnitude above a Mendelian one.
      Applied here rather than upstream because the pool's other readers need the wider set.
    - **The review status clears `review_status_floor`**, the caller's policy (module docstring).

    Args:
        variants: The gene's ClinVar-classified variants (clinvar's `DescribeVariantResponse.classified_in_gene`).
        review_status_floor: Minimum ClinVar gold stars a record needs to count, 0-4.

    Returns:
        The qualifying records.

    Raises:
        ValueError: If `review_status_floor` is outside ClinVar's 0-4 star range, or a record's
            classification carries a term outside ClinVar's germline vocabulary.
    """
    if not 0 <= review_status_floor <= MAX_REVIEW_STARS:
        raise ValueError(f'review status floor must be 0-{MAX_REVIEW_STARS} stars, got {review_status_floor}')
    return [
        variant
        for variant in variants
        if clinvar_classification.is_unqualified_pathogenic(variant.classification)
        and variant.review_stars >= review_status_floor
    ]


def daft_from_pathogenic_variants(
    variants: Sequence[ClassifiedVariant], *, review_status_floor: int, pool_truncated: bool
) -> Daft:
    """Compute a DAFT by the pathogenic-variants method (SM3's third choice).

    Reach for `daft_calculator` first and fall back here when its parameters cannot be sourced: SM3
    ranks this method below the calculator and the binning tables, and being the only one that
    automates does not promote it (module docstring).

    The DAFT is the highest Grpmax FAF among the gene's >=10 known P/LP variants, after excluding a
    founder/outlier variant whose FAF is more than 5x the next-highest. The empirical maximum can
    under-estimate the max-credible AF for an under-studied gene, where SM3 asks for a high,
    conservative DAFT.

    Three sets, and which one each rule reads is what makes the method behave:

    - The **known P/LP variants** are `known_pathogenic`'s two gates over the supplied records.
    - The **>=10** is counted over those whose FAF is readable — a variant absent from gnomAD counts
      (its frequency is known, and SM3 expects most of a gene's P/LP variants to be absent), one
      whose FAF rests on filter-failing calls does not. A pool that reaches ten only by counting
      conflicting records, unreviewed ones, or unreadable frequencies has not met the precondition,
      and the calculator method is where that lands.
    - The **peel and the maximum** run over the *observed* FAFs alone. SM3's founder is "significantly
      higher in frequency than other P/LP variants", which the absent ones are not: leaving their
      zeros in the comparison makes any single observed variant more than 5x its next-highest
      neighbour, so the peel eats the whole set and the method reports no threshold on exactly the
      ordinary gene — a handful of observed variants among mostly-absent ones.

    A P/LP variant whose FAF rests on filter-failing calls is dropped rather than entered as 0. Its
    frequency is not high, low or absent — it is unreadable, and a threshold is a claim about the
    frequencies the gene's pathogenic variants actually reach. Dropping it does lower the maximum,
    which is the direction that risks a false benign call; the >=10 floor over the readable set is
    what stops that from being decided by a handful of variants.

    At most one variant is peeled, where SM3 names "founder/outlier variants" without saying how far
    to walk. Repeating the comparison down the sorted list looks like the same rule but is a
    different one: P/LP frequencies in a gene routinely span orders of magnitude, so consecutive >5x
    gaps are the ordinary spectrum rather than a stack of founder effects, and a repeating peel walks
    a gene like {4e-3, 6e-4, 8e-5, 1e-5, 1e-6} down to its smallest value — a threshold three orders
    of magnitude below the gene's real maximum, in the direction that produces false benign calls.
    Peeling once removes the artefact SM3 describes and errs high, which is the direction SM3 asks
    for. A Grpmax FAF has also already had the bottlenecked groups (Amish, Ashkenazi Jewish, European
    Finnish, and the remaining individuals) excluded upstream, so the founder spikes the rule was
    written against are largely gone before this sees them.

    Args:
        variants: The gene's ClinVar-classified variants, each with its joint Grpmax FAF
            (`joint_faf`); a variant absent from gnomAD carries a `FafSupport.ABSENT` 0.
        review_status_floor: Minimum ClinVar gold stars a record needs to count, 0-4. Required: the
            library has no defensible default (module docstring). It must be the floor the pool was
            *fetched* at or stricter — a pool already filtered at 2 stars re-filtered here at 0 makes
            the stamped basis say "at >= 0 stars" of a set that never contained one, and nothing here
            can detect that.
        pool_truncated: Whether `variants` is a prefix of the gene's classified set rather than all
            of it (clinvar's `DescribeVariantResponse.pool_truncated`). A maximum over a prefix is a lower
            bound, and the prefix is arbitrary with respect to frequency, so the threshold is returned as
            `Daft.lower_bound` — SM3's method is defined over the gene's whole P/LP set.

    Returns:
        The `Daft`: the highest non-outlier FAF, and a basis naming the pool it was taken over.

    Raises:
        ValueError: If `review_status_floor` is outside 0-4, if fewer than 10 supplied records are
            known P/LP variants with a readable FAF (use the calculator method), or if none of them
            was observed in gnomAD (no frequency to anchor a threshold).
    """
    qualifying = known_pathogenic(variants, review_status_floor=review_status_floor)
    readable = [variant.faf.value for variant in qualifying if variant.faf.scoreable]
    if len(readable) < MIN_PATHOGENIC_VARIANTS:
        raise ValueError(
            f'pathogenic-variants method needs >= {MIN_PATHOGENIC_VARIANTS} known P/LP variants at '
            f'>= {review_status_floor} stars with a readable FAF; got {len(readable)} of '
            f'{len(qualifying)} known P/LP variants, out of {len(variants)} supplied records — '
            'use the calculator method'
        )
    observed = sorted((value for value in readable if value > 0), reverse=True)
    if not observed:
        raise ValueError(
            'no known P/LP variant of the gene carries a Grpmax FAF above zero; no threshold '
            'derivable — use the calculator method'
        )
    remaining = observed
    if len(observed) >= 2 and observed[0] > _FOUNDER_OUTLIER_MULTIPLE * observed[1]:
        remaining = observed[1:]
    pool = 'a truncated pool, so a lower bound' if pool_truncated else "the gene's whole classified set"
    return Daft(
        value=remaining[0],
        method=DaftMethod.PATHOGENIC_VARIANTS,
        lower_bound=pool_truncated,
        releases=provenance.union(*(v.faf.releases for v in qualifying if v.faf.scoreable)),
        basis=(
            f'pathogenic-variants over {pool}: {len(readable)} known P/LP variants at '
            f'>= {review_status_floor} stars with a readable FAF (of {len(variants)} records), '
            f'{len(observed)} of them with a Grpmax FAF above zero, '
            f'{len(observed) - len(remaining)} peeled as founder outliers'
        ),
    )


def daft_from_clinvar_pool(
    request: clinvar_pb2.DescribeVariantRequest,
    response: clinvar_pb2.DescribeVariantResponse,
    fafs: Mapping[str, Faf],
) -> Daft:
    """Compute the pathogenic-variants DAFT over a `ClinVar.DescribeVariant` gene pool.

    The two facts a caller most easily gets wrong are taken from the exchange rather than passed
    again. The review-status floor comes from the **request**, because the floor rides in the search
    term: re-stating a stricter one here would stamp a basis naming a floor the pool was never
    fetched at. The lower bound comes from `pool_truncated`, because a maximum over a prefix of the
    gene's pathogenic set is a lower bound and the prefix is arbitrary with respect to frequency.

    Args:
        request: The request the pool was fetched with; its `review_status_floor` is the pool's.
        response: The rpc's answer; `classified_in_gene` is the pool and `pool_truncated` the bound.
        fafs: Each pooled record's joint Grpmax FAF (`faf_from_gnomad`), keyed by the record's
            `clinvar_id`. A record with no entry is one whose frequency was not established, which
            `daft_from_pathogenic_variants` counts toward neither the pool nor the maximum.

    Returns:
        The `Daft`, stamped with the ClinVar releases the response names and the gnomAD releases
        behind the frequencies it was taken over.

    Raises:
        ValueError: If `fafs` names a record the pool does not carry — the mapping is keyed on the
            record accession, and a mapping keyed on anything else would otherwise read as a pool
            whose frequencies were never looked up. Also on everything
            `daft_from_pathogenic_variants` refuses: a pool under ten readable known P/LP variants,
            or none observed in gnomAD.
    """
    pooled = {record.clinvar_id for record in response.classified_in_gene}
    unmatched = sorted(set(fafs) - pooled)
    if unmatched:
        raise ValueError(
            f'{unmatched} name no record of the pool; the frequencies are keyed on ClinVarRecord.clinvar_id, '
            'and a mapping keyed on anything else leaves every record without a readable FAF'
        )
    variants = [
        ClassifiedVariant(
            classification=record.classification,
            review_stars=record.review_stars,
            faf=fafs.get(record.clinvar_id, unknown_faf()),
        )
        for record in response.classified_in_gene
    ]
    daft = daft_from_pathogenic_variants(
        variants, review_status_floor=request.review_status_floor, pool_truncated=response.pool_truncated
    )
    return dataclasses.replace(
        daft, releases=provenance.union(daft.releases, provenance.releases_of(response.provenance))
    )


@dataclasses.dataclass(frozen=True)
class PopFrq:
    """The POP_FRQ finding: the points where the code was determined, and what they rest on.

    A `classify.ScoredCode`. Points of 0.0 are a determination — the variant is rarer than its
    threshold, or absent from gnomAD — and a FAF the framework cannot score is not that: it carries
    no points at all, so a tally line reading `POP_FRQ 0.0` never stands for a frequency nobody could
    read. The points separate the two findings; `faf.support` separates the two zeros, and names why
    an unscoreable FAF could not be scored.

    Attributes:
        points: The POP_FRQ points, for the classifier's independent codes; None where the FAF was
            not scoreable, which is the framework determining no POP_FRQ at all.
        faf: The FAF that was binned, with its QC support.
        daft: The threshold it was binned against, with the method and inputs behind it.
        multiple: `faf / daft`, the quantity the bins are defined over; None under a FAF that was not
            scoreable, since no multiple of it means anything.
    """

    points: decimal.Decimal | None
    faf: Faf
    daft: Daft
    multiple: decimal.Decimal | None

    def __post_init__(self) -> None:
        if self.faf.support is FafSupport.UNKNOWN:
            raise ValueError(
                'POP_FRQ needs a frequency: this variant has none established — neither a score of 0 nor the '
                'no-determination an unscoreable FAF earns, both of which rest on a lookup somebody made'
            )
        if (self.points is None) is self.faf.scoreable:
            raise ValueError(
                f'a POP_FRQ over a {self.faf.support.value} FAF carries points iff that FAF is scoreable, '
                f'got {self.points}'
            )
        if (self.multiple is None) is not (self.points is None):
            raise ValueError(
                f'a POP_FRQ carries the multiple that selected its bin iff it carries points, got '
                f'{self.multiple} and {self.points}'
            )
        if self.points is not None:
            # Before the comparisons: a NaN would make `> 0` raise InvalidOperation instead.
            if not self.points.is_finite():
                raise ValueError(f'POP_FRQ points must be finite, got {self.points}')
            if self.points > 0:
                raise ValueError(f'POP_FRQ counts toward benignity only, got {self.points}')
            if self.points.is_signed() and self.points.is_zero():
                raise ValueError('POP_FRQ points of negative zero read as a benign score that rounded away')

    @property
    def code(self) -> str:
        """The evidence code these points are filed under."""
        return 'POP_FRQ'

    @property
    def derivation(self) -> str:
        """The FAF, its QC support, the threshold, and the multiple that selected the bin — or that none did."""
        outcome = 'unscoreable, so no POP_FRQ determined' if self.multiple is None else f'{_shown(self.multiple)}x DAFT'
        return f'FAF {self.faf.value} ({self.faf.support.value}) against {self.daft.basis} -> {outcome}'

    @property
    def releases(self) -> tuple[provenance.Release, ...]:
        """The releases behind both sides of the comparison."""
        return provenance.union(self.faf.releases, self.daft.releases)


def _shown(multiple: decimal.Decimal) -> str:
    """A FAF/DAFT multiple at three significant digits, for the trail.

    Truncated rather than rounded: the bins are stated as multiples, so a rounded 1.4999 shown as
    1.50 reads as the bin above the one that was scored. The exact value stays on `PopFrq.multiple`.
    """
    truncated = multiple.quantize(
        decimal.Decimal(1).scaleb(multiple.adjusted() - _SHOWN_DIGITS + 1), rounding=decimal.ROUND_DOWN
    )
    return f'{truncated:f}'


def pop_frq(ref: reference.Reference, faf: Faf, daft: Daft) -> PopFrq:
    """Bin the FAF against the DAFT to POP_FRQ points (0.0 / -1.0 / -3.0 / -6.0; SM3 Figure 1).

    The FAF/DAFT multiple selects the bin: `< 1.5x` -> 0.0, `>= 1.5x` -> -1.0, `>= 5x` -> -3.0,
    `>= 15x` -> -6.0 (the reference's open boundaries closed to the lower edge). An absent variant
    (FAF 0) bins to 0.0 — absence is no benignity evidence.

    A FAF resting on filter-failing calls determines no POP_FRQ whatever its value: the gnomAD calls
    behind it did not pass variant QC, so it may count against the variant's rarity but never toward
    its benignity (GN085). 0.0 is not the neutral answer that leaves — it is what a variant rarer
    than its threshold earns, and the rarity SM4 conditions the clinical codes on — so the finding
    carries no points and `faf.support` says why. A FAF that was never established is a third thing
    again, and it raises: even a not-determined finding asserts the framework weighed a frequency.

    Args:
        ref: The loaded reference (supplies the bins).
        faf: The variant's joint Grpmax FAF and its QC support (`joint_faf`).
        daft: The disease allele frequency threshold and the method that produced it. Read
            `daft.lower_bound` before the points: a threshold bounded only from below awards benign
            points the evidence may not support.

    Returns:
        The `PopFrq`: the points where the FAF was scoreable, plus both inputs, so a report can state
        which threshold method the score rests on without the caller having to track it.

    Raises:
        ValueError: If the variant's frequency was never established (`FafSupport.UNKNOWN`).
    """
    if not faf.scoreable:
        return PopFrq(points=None, faf=faf, daft=daft, multiple=None)
    multiple = faf.value / daft.value
    points = ref.frequency_bins[0].points
    for frequency_bin in ref.frequency_bins:
        if multiple >= frequency_bin.min_multiple:
            points = frequency_bin.points
    return PopFrq(points=points, faf=faf, daft=daft, multiple=multiple)


@dataclasses.dataclass(frozen=True)
class CallsetObservations:
    """One gnomAD callset's homozygous and hemizygous counts, with that callset's QC verdict.

    Attributes:
        callset: `exome` or `genome` — which block the counts were read from.
        homozygotes: The callset's `homozygote_count`.
        hemizygotes: Its `hemizygote_count`. Summed with the homozygotes rather than kept apart:
            SM3 Table 7 weighs a homozygous and a hemizygous occurrence identically, and on an
            X-linked MDE a variant has both.
        filters: The callset's own `filters`; empty is a pass, exactly as on `Callset`.
    """

    callset: str
    homozygotes: int
    hemizygotes: int
    filters: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.homozygotes < 0 or self.hemizygotes < 0:
            raise ValueError(f'{self.callset} counts must be non-negative, got {self.homozygotes}/{self.hemizygotes}')

    @property
    def passed_qc(self) -> bool:
        """Whether the callset's calls of this variant passed gnomAD's variant QC."""
        return not self.filters

    @property
    def observations(self) -> int:
        """The callset's homozygous and hemizygous occurrences together."""
        return self.homozygotes + self.hemizygotes


@dataclasses.dataclass(frozen=True)
class HomozygoteObservations:
    """gnomAD's homozygous/hemizygous occurrences of a variant, split by each callset's QC verdict.

    The callsets are separate sample sets, so their counts sum. The QC split is the same asymmetry
    the FAF carries: an occurrence in a callset that failed variant QC is not a homozygous
    observation, and counting it would award benign points off a call gnomAD did not stand behind.

    What this is not is an eligibility verdict. QC is the part a filter can settle; coverage at the
    position and whether the genotype is credible are the analyst's, which is why `pop_hmz` still
    takes a bare count as well.

    Attributes:
        callsets: One entry per callset gnomAD holds the variant in; at least one.
        releases: The gnomAD releases the counts rest on.
    """

    callsets: tuple[CallsetObservations, ...]
    releases: tuple[provenance.Release, ...] = ()

    def __post_init__(self) -> None:
        if not self.callsets:
            raise ValueError('gnomAD holds the variant in neither callset, so there is no count to read')

    @property
    def eligible(self) -> int:
        """The occurrences in QC-passing callsets — the count SM3 Table 7 is entered with."""
        return sum(callset.observations for callset in self.callsets if callset.passed_qc)

    @property
    def excluded_for_qc(self) -> int:
        """The occurrences dropped because their callset failed variant QC."""
        return sum(callset.observations for callset in self.callsets if not callset.passed_qc)

    @property
    def derivation(self) -> str:
        """The per-callset account, so a reader can see what was counted and what was dropped."""
        counted = ', '.join(
            f'{callset.callset} {callset.homozygotes} homozygous / {callset.hemizygotes} hemizygous'
            f'{"" if callset.passed_qc else f" (excluded: filters {list(callset.filters)})"}'
            for callset in self.callsets
        )
        return f'gnomAD {counted}'


def homozygotes_from_gnomad(response: gnomad_pb2.DescribeVariantResponse) -> HomozygoteObservations:
    """Read a variant's homozygous/hemizygous occurrences off a `Gnomad.DescribeVariant` response.

    Reads `homozygote_count`, `hemizygote_count` and `filters` under each of `variant.exome` and
    `variant.genome`. Per callset, because the QC verdict is: the joint block states counts over
    calls that failed variant QC, exactly as it states a FAF over them.

    Args:
        response: The rpc's answer.

    Returns:
        The `HomozygoteObservations`, stamped with the gnomAD releases the response names.

    Raises:
        ValueError: If the payload does not carry one of the documented paths, if gnomAD holds the
            variant in neither callset, or if the response states no provenance.
    """
    raw = payload.fields(response.raw)
    counted = []
    for name in _CALLSETS:
        if payload.block(raw, f'variant.{name}') is None:
            continue
        counted.append(
            CallsetObservations(
                callset=name,
                homozygotes=payload.count(raw, f'variant.{name}.homozygote_count'),
                hemizygotes=payload.count(raw, f'variant.{name}.hemizygote_count'),
                filters=payload.strings(raw, f'variant.{name}.filters'),
            )
        )
    return HomozygoteObservations(callsets=tuple(counted), releases=provenance.releases_of(response.provenance))


class HmzSupport(enum.Enum):
    """What a POP_HMZ finding rests on — the gate homozygote evidence has to pass."""

    SCORED = 'scored'  # the eligible count reached SM3's floor; the points are a determination
    BELOW_FLOOR = 'below_floor'  # a count was established and fell under the floor; SM3 determines no score
    PRECONDITION_UNMET = 'precondition_unmet'  # SM3 Table 7's penetrance/severity gate failed; the count is moot
    UNKNOWN = 'unknown'  # no count was established — not looked up, or the lookup failed


@dataclasses.dataclass(frozen=True)
class PopHmz:
    """The POP_HMZ finding: the points where the code was determined, and what it rests on.

    A count below SM3's floor of two eligible observations is not a score of zero, and the two must
    not arrive as the same `Decimal`: a tally line reading `POP_HMZ 0.0` asserts the code was
    assessed and contributed nothing, where the framework determined nothing at all.

    Attributes:
        points: The POP_HMZ points; None wherever the code was not determined, so a not-determined
            finding cannot be summed into a tally or filed as an independent code by accident.
        support: Whether the count reached SM3's floor, fell below it, or was never established.
        observations: The eligible homozygous/hemizygous observations the finding was read from;
            None under `HmzSupport.UNKNOWN`, where there is no count.
        counts: The per-callset counts the eligible number was read off, where `pop_hmz` was handed
            gnomAD's rather than a bare number; None where the caller supplied the count itself.
    """

    points: decimal.Decimal | None
    support: HmzSupport
    observations: int | None
    counts: HomozygoteObservations | None = None

    @property
    def code(self) -> str:
        """The evidence code these points are filed under."""
        return 'POP_HMZ'

    @property
    def releases(self) -> tuple[provenance.Release, ...]:
        """The releases behind the counts, where a response supplied them."""
        return () if self.counts is None else self.counts.releases

    @property
    def derivation(self) -> str:
        """The count and what the framework made of it, including where it made nothing."""
        counted = '' if self.counts is None else f'; {self.counts.derivation}'
        if self.support is HmzSupport.UNKNOWN:
            return 'no homozygous/hemizygous count established'
        if self.support is HmzSupport.PRECONDITION_UNMET:
            return (
                f'{self.observations} eligible homozygous/hemizygous observations, unscored: SM3 Table 7 '
                "conditions every row on near-100% penetrance and affected individuals' absence from "
                f'population databases{counted}'
            )
        if self.support is HmzSupport.BELOW_FLOOR:
            return (
                f"{self.observations} eligible homozygous/hemizygous observations, below SM3 Table 7's floor "
                f'of {_HMZ_OBSERVATION_FLOOR}{counted}'
            )
        return (
            f'{self.observations} eligible homozygous/hemizygous observations, all but the first counted '
            f'(SM3 Table 7){counted}'
        )

    def __post_init__(self) -> None:
        if (self.points is None) is (self.support is HmzSupport.SCORED):
            raise ValueError(f'a {self.support.value} POP_HMZ carries points iff it is scored, got {self.points}')
        if (self.observations is None) is not (self.support is HmzSupport.UNKNOWN):
            raise ValueError(f'a {self.support.value} POP_HMZ carries a count iff it is not unknown')
        if self.observations is not None:
            if self.observations < 0:
                raise ValueError(f'observations must be non-negative, got {self.observations}')
            # Only the two states the count decides between are held to it; under an unmet
            # precondition SM3 determines nothing whatever the count reached.
            below = self.observations < _HMZ_OBSERVATION_FLOOR
            if (self.support is HmzSupport.SCORED and below) or (self.support is HmzSupport.BELOW_FLOOR and not below):
                raise ValueError(
                    f'a {self.support.value} POP_HMZ over {self.observations} observations, against a floor '
                    f'of {_HMZ_OBSERVATION_FLOOR}'
                )
        if self.points is not None:
            # Before the comparisons: a NaN weight would make `> 0` raise InvalidOperation instead.
            if not self.points.is_finite():
                raise ValueError(f'POP_HMZ points must be finite, got {self.points}')
            if self.points > 0:
                raise ValueError(f'POP_HMZ counts toward benignity only, got {self.points}')
            if self.points.is_signed() and self.points.is_zero():
                raise ValueError('POP_HMZ points of negative zero read as a benign score that rounded away')


def unknown_hmz() -> PopHmz:
    """The POP_HMZ of a variant whose homozygote count was never established.

    Distinct from a below-floor finding so that "fewer than two eligible observations" is never what a
    failed or skipped lookup reads as — the same distinction `unknown_faf` draws on the frequency side.
    """
    return PopHmz(points=None, support=HmzSupport.UNKNOWN, observations=None)


def pop_hmz(
    ref: reference.Reference,
    inheritance: HmzInheritance,
    observations: int | HomozygoteObservations,
    *,
    penetrance_near_100pct: bool,
    affected_not_expected_in_databases: bool,
) -> PopHmz:
    """Score homozygous/hemizygous population occurrences (POP_HMZ; SM3 Table 7).

    Points accrue only from the 2nd eligible observation onward (the first is not counted), and only
    when there are at least two. AD homozygous scores -1.0 per counted observation; semidominant,
    AR, and X-linked score -0.5. Below two the code is not determined, which is what the returned
    `PopHmz` says rather than scoring a zero.

    The reference (and SM3 Table 7) assign AD homozygous -1.0; SM3's prose (para "an analyst can
    assign -0.5 pts per homozygous occurrence") disagrees for AD. The reference/table value (-1.0)
    is authoritative here.

    Every row of Table 7 is conditioned on the penetrance/severity gate, which is why both limbs are
    required rather than defaulted: a gate assumed met awards benign points SM3 withholds, and a
    phenotype mild enough to appear in a population database fails the second limb at any count —
    SM3's own worked case turns on that, with dozens of hemizygotes earning no benignity at all.

    Eligibility carries the same QC question the FAF does — a homozygote call from a callset that
    failed variant QC is not a homozygous observation — and `homozygotes_from_gnomad` settles that
    half off the response. The rest of it stays the caller's, which is why a bare count is admissible
    too: coverage at the position and whether the genotype is credible are not filter tests. A caller
    that never established a count passes none: use `unknown_hmz`, not a 0.

    Args:
        ref: The loaded reference (supplies the per-observation weights).
        inheritance: The MDE inheritance.
        observations: gnomAD's counts (`homozygotes_from_gnomad`), whose QC-passing occurrences are
            taken, or a count of eligible occurrences the caller established itself.
        penetrance_near_100pct: Whether the MDE's age-matched penetrance is near 100%.
        affected_not_expected_in_databases: Whether affected individuals would be kept out of
            population databases by the MDE's severity. False for a phenotype mild enough to appear
            in one. Individuals whose clinical details are given are CLN_UAF, not this code.

    Returns:
        The `PopHmz`: the points and the count they were read from; a `HmzSupport.BELOW_FLOOR`
        finding carrying no points where the count is under two; or a
        `HmzSupport.PRECONDITION_UNMET` one where either gate limb is False.

    Raises:
        ValueError: If `observations` is negative, or the reference weights a counted observation
            positively or at negative zero. `PopHmz` holds all three: they are properties of a
            finding, not of this call.
    """
    counts = None if isinstance(observations, int) else observations
    eligible = observations if isinstance(observations, int) else observations.eligible
    if not (penetrance_near_100pct and affected_not_expected_in_databases):
        return PopHmz(points=None, support=HmzSupport.PRECONDITION_UNMET, observations=eligible, counts=counts)
    if eligible < _HMZ_OBSERVATION_FLOOR:
        return PopHmz(points=None, support=HmzSupport.BELOW_FLOOR, observations=eligible, counts=counts)
    weights = ref.per_observation.homozygous
    per_observation = (weights.dominant if inheritance is HmzInheritance.AD else weights.other).points
    return PopHmz(
        points=per_observation * (eligible - 1),
        support=HmzSupport.SCORED,
        observations=eligible,
        counts=counts,
    )
