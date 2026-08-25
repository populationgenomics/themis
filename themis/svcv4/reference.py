"""The typed shape of the SVCv4 framework, and the checks a transcription of it has to satisfy.

The framework is data: classification bands and VUS sub-bands, the Tavtigian odds-to-points
calibration, the gene-disease-validity gate, the molecular-mechanism x exon-relevance matrix, the
POP_FRQ bins, the per-code point ranges, the tables that price one observed individual, SM4's
POP_FRQ precondition on the clinical tables, and SM7's ceiling on the critical-residue award. This
module holds the types those values take and the checks that hold them together. The values live in
`data`, one module per framework area, and `data.load_reference` assembles them into the one
`Reference` every compute module reads.

That split is what makes the shape checkable. A value module states a typed literal, so a dropped
or renamed field is a type error where it is written rather than a `KeyError` raised mid-score on a
variant, and what is left here are the framework-consistency questions no type can answer: that the
bands tile the point line and the VUS sub-bands partition the VUS band, that every gate row names a
curated `themis.rpc.gene_disease_pb2.GateLevel` and permits only classes the bands define, that
each matrix axis states exactly the levels a caller selects a multiplier by, that a conditioned code
exists and its admissible points fall inside the range of the code they are stated over. The two
anchors those checks rest on are outside the transcription: the contract enum, and the axis-level
vocabulary `scoring` states separately for a caller to name. Everything else is internal agreement,
and it runs at import of `data` — a transcription that fails a check is not loaded at all.

Every value cites the supplement line it is read from, and `Reference.cited_documents` pins the
revision of the document set those citations address: an SM<n> §<m> citation is a line number, so
without a fixed revision no citation resolves. `Reference.provenance`, the transcription's own
statement of what it is, is held the same way.

Point values are `decimal.Decimal` so the tally is exact and auditable: every SVCv4 point value and
matrix multiplier is a terminating decimal, so decimal arithmetic never introduces the binary-float
rounding that could shift a total across a band boundary.
"""

from __future__ import annotations

import dataclasses
import decimal
import itertools
import re
from collections.abc import Mapping, Sequence

from themis.rpc import gene_disease_pb2

# A side the framework states no bound on, in a per-code range or a combining cap: the code's
# weights are per observation and accumulate, so a clamp on that side would truncate a total.
UNBOUNDED_LOW = decimal.Decimal('-Infinity')
UNBOUNDED_HIGH = decimal.Decimal('Infinity')

# A full git commit id. A branch moves and a short id can grow ambiguous, so neither fixes the line
# an SM citation names.
_COMMIT_REVISION = re.compile(r'^[0-9a-f]{40}$')

# SM18's two matrix axes, declared rather than read off the transcription these checks validate.
# Restated here and not imported because `scoring` — where the same vocabulary is the enum a caller
# selects a multiplier with — imports this module; a test holds the two together.
MECHANISM_LEVELS = frozenset({'Established', 'Likely', 'Suspected', 'Unlikely', 'Unknown', 'Uncertain'})
EXON_LEVELS = frozenset({'All', 'Most', 'Few'})

# The two directions SM20 prints a control-count grid for, declared here for the same reason:
# `functional.fxn_from_controls` selects on them, so a renamed one is a KeyError raised mid-score.
CONTROL_RANGES = ('pathogenic', 'benign')

# SM3 states POP_FRQ over four DAFT-multiple bins.
_FREQUENCY_BIN_COUNT = 4


class ReferenceDataError(Exception):
    """The reference data is inconsistent with the framework it transcribes."""


def printed_decimals(*values: str) -> tuple[decimal.Decimal, ...]:
    """Decimals stated exactly as the framework prints them.

    Args:
        values: The values, as the decimal strings the supplement prints. The trailing zeros are the
            precision it prints them at, which is what a re-read of the same page checks against.

    Returns:
        The values as decimals, in the order given.
    """
    return tuple(decimal.Decimal(value) for value in values)


def _validate_strictly_ordered[Axis: (int, decimal.Decimal)](
    values: Sequence[Axis], context: str, *, ascending: bool
) -> None:
    """Fail loud unless the values run strictly one way, the way the framework prints the axis."""
    for left, right in itertools.pairwise(values):
        if (left >= right) if ascending else (left <= right):
            direction = 'ascending' if ascending else 'descending'
            raise ReferenceDataError(f'{context} must be strictly {direction}, got {list(values)}')


@dataclasses.dataclass(frozen=True)
class Band:
    """A half-open point interval mapping to a classification code or VUS sub-band.

    A `None` bound is unbounded (-inf for `lower`, +inf for `upper`).
    """

    code: str
    lower: decimal.Decimal | None
    lower_inclusive: bool
    upper: decimal.Decimal | None
    upper_inclusive: bool

    def contains(self, points: decimal.Decimal) -> bool:
        """Return whether `points` falls in this band."""
        above_lower = self.lower is None or points > self.lower or (points == self.lower and self.lower_inclusive)
        below_upper = self.upper is None or points < self.upper or (points == self.upper and self.upper_inclusive)
        return above_lower and below_upper


def gate_level_name(level: gene_disease_pb2.GateLevel) -> str:
    """Name a gate level for an error message.

    Args:
        level: A gate level; an untyped code-mode caller may pass a value the enum does not name.

    Returns:
        The `GateLevel` member name, or `repr(level)` where the enum names no member for it.
    """
    if type(level) is int and level in gene_disease_pb2.GateLevel.values():
        return gene_disease_pb2.GateLevel.Name(level)
    return repr(level)


@dataclasses.dataclass(frozen=True)
class GateRow:
    """One gene-disease-validity level's gate: the classes it permits, or a terminal result string.

    Exactly one of the two, which is the invariant `apply_gate` branches on. A level with neither
    permits nothing and names nothing, so it would silently cap every band to the lowest class; a
    level with both leaves which one wins to whichever field the reader looked at first.
    """

    level: gene_disease_pb2.GateLevel
    allows: frozenset[str]
    result: str | None

    def __post_init__(self) -> None:
        if bool(self.allows) is (self.result is not None):
            raise ReferenceDataError(
                f'gate level {gate_level_name(self.level)} must either permit classes or name a terminal '
                f'result, not both and not neither; got allows={sorted(self.allows)} result={self.result!r}'
            )


@dataclasses.dataclass(frozen=True)
class CodeSpec:
    """An evidence code as the framework states it, with its fixed point range `[low, high]`.

    Attributes:
        code: The code name.
        family: The code's prefix (POP, CLN, LOC, MIS, CDS, NUL, SPL); which of them a variant-type
            path sums is `Reference.independent_families`.
        concept: The evidence concept the code carries inside its family (FRQ, PRD, FXN, INF, ...).
        direction: The single direction the framework scores the code in, where it states one; None
            where the code carries evidence both ways.
        low: The floor of the per-code cap, `UNBOUNDED_LOW` where no supplement states one.
        high: The ceiling of the per-code cap, `UNBOUNDED_HIGH` where no supplement states one.
        supplement: The supplements the code is stated in.
        notes: What the framework states about the code, and where our reading of its range departs
            from the ClinGen pilot calculator's.
    """

    code: str
    family: str
    concept: str
    direction: str | None
    low: decimal.Decimal
    high: decimal.Decimal
    supplement: tuple[int, ...]
    notes: str

    def __post_init__(self) -> None:
        """A code names itself, its family and its concept; a blank one addresses no cap.

        The family carries the most: `independent_families` reads the path split off it, so a blank
        one puts the code on neither side of the split rather than on the wrong one.
        """
        for name in ('code', 'family', 'concept'):
            if not getattr(self, name).strip():
                raise ReferenceDataError(f'evidence code {self.code!r} states a blank {name}')


@dataclasses.dataclass(frozen=True)
class CapRange:
    """A framework combining cap `[low, high]`; an unbounded side is -Infinity / +Infinity."""

    low: decimal.Decimal
    high: decimal.Decimal

    def __post_init__(self) -> None:
        if self.low > self.high:
            raise ReferenceDataError(f'cap [{self.low}, {self.high}] has low above high, so it admits nothing')


@dataclasses.dataclass(frozen=True)
class FrequencyBin:
    """One POP_FRQ bin: an inclusive-lower DAFT-multiple threshold mapping to points.

    Attributes:
        cell: The bin's cell-id fragment, which is how `observations` addresses the row.
        ratio: The bin as SM3 prints it, kept so the interval this reading was taken from stays
            checkable against the supplement.
        min_multiple: The smallest FAF/DAFT ratio (inclusive) that earns `points`. SM3 prints the
            bins with open boundaries at 1.5x/5x/15x, closed here to the lower edge so the bins
            partition the ratio line with no gaps (see SM3 Figure 1's worked FBN1 thresholds).
        points: What one observation in the bin scores.
    """

    cell: str
    ratio: str
    min_multiple: decimal.Decimal
    points: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class BinningGrid:
    """One of SM3's six DAFT lookup tables: a prevalence x penetrance grid of thresholds.

    Both axes are ordered as the image prints them, and SM3's rounding rules move an estimate onto
    them in opposite directions: prevalence up, to a more frequent bin and so a *smaller*
    denominator; penetrance down, to a less penetrant column. `frequency.binned_daft` applies them.

    Attributes:
        number: SM3's table number, 1-6.
        caption: The caption SM3 prints above the image, verbatim.
        title: The title printed inside the image — SM3's own name for the table, and how a curation
            block names the one it read.
        applies_to: The MDE and frequency stratum the table serves, and which prevalence to enter.
        prevalence_denominators: The row axis, ascending: X in a prevalence of "1 in X", so a larger
            denominator is a rarer disease.
        penetrances: The column axis, descending, as the image orders it.
        cells: (prevalence denominator, penetrance) -> the threshold that cell states.
        marked: The cells SM3 prints with a `*` it defines nowhere (`population.BINNING.marker_note`).
        media_file: The image inside SM3's .docx the table was read from.
        media_pixels: That image's pixel dimensions.
        legibility: Whether every cell was in frame and readable at the resolution it was read at.
    """

    number: int
    caption: str
    title: str
    applies_to: str
    prevalence_denominators: tuple[int, ...]
    penetrances: tuple[decimal.Decimal, ...]
    cells: Mapping[tuple[int, decimal.Decimal], decimal.Decimal]
    marked: frozenset[tuple[int, decimal.Decimal]]
    media_file: str
    media_pixels: str
    legibility: str

    def __post_init__(self) -> None:
        """Hold the grid to its own axes: full coverage, the printed order, and positive thresholds.

        The axes carry SM3's rounding rules, so their order is not presentation: `binned_daft` walks
        the rows down to the estimate and the columns along it. A missing cell would be a lookup that
        raises on a prevalence the table does prints a row for.
        """
        _validate_strictly_ordered(
            self.prevalence_denominators, f'{self.title} prevalence denominators', ascending=True
        )
        _validate_strictly_ordered(self.penetrances, f'{self.title} penetrances', ascending=False)
        expected = set(itertools.product(self.prevalence_denominators, self.penetrances))
        missing = sorted(str(cell) for cell in expected - self.cells.keys())
        extra = sorted(str(cell) for cell in self.cells.keys() - expected)
        if missing or extra:
            raise ReferenceDataError(
                f'{self.title} states cells the axes do not: missing {missing}, off the axes {extra}'
            )
        for cell, threshold in self.cells.items():
            if threshold <= 0:
                raise ReferenceDataError(f'{self.title} cell {cell}: a DAFT must be positive, got {threshold}')
        off_axis = sorted(str(cell) for cell in self.marked - self.cells.keys())
        if off_axis:
            raise ReferenceDataError(f'{self.title} marks {off_axis}, which is off the axes')


@dataclasses.dataclass(frozen=True)
class PopFrqPrecondition:
    """The POP_FRQ assignments SM4 conditions a clinical code's scoring tables on.

    A conditioned code scored beside any other POP_FRQ value, or beside none at all, is one the
    framework withdraws rather than one worth fewer points.

    Attributes:
        conditioned_codes: The codes whose tables the precondition governs.
        admissible_points: The POP_FRQ point values that admit them.
        source: The passage the precondition is read from.
    """

    conditioned_codes: frozenset[str]
    admissible_points: frozenset[decimal.Decimal]
    source: str

    def __post_init__(self) -> None:
        if not self.conditioned_codes or not self.admissible_points:
            raise ReferenceDataError(
                'the POP_FRQ precondition must name at least one conditioned code and one admissible POP_FRQ value; '
                'a gate over neither fires on every classification or on none'
            )


@dataclasses.dataclass(frozen=True)
class ControlCountGrid:
    """One of SM20's two control-count lookup tables: a benign x pathogenic grid of FXN points.

    Attributes:
        number: SM20's table number, 1 (pathogenicity) or 2 (benignity).
        direction: Which control range the test variant's result falls in for this table to apply.
        caption: The caption SM20 prints above the image, verbatim.
        applies_to: The case the table serves.
        cells: (benign controls, pathogenic controls) -> the points that cell states.
        media_file: The image inside SM20's .docx the table was read from.
        media_pixels: That image's pixel dimensions.
        legibility: Whether every cell was in frame and readable at the resolution it was read at.
    """

    number: int
    direction: str
    caption: str
    applies_to: str
    cells: Mapping[tuple[int, int], decimal.Decimal]
    media_file: str
    media_pixels: str
    legibility: str

    def __post_init__(self) -> None:
        """Hold the grid to its axes and to its own direction's sign.

        Both axes are control counts from 0 up, so the grid is square and its extent is its own
        statement of how many controls the table reaches; a cell outside that square is one nothing
        can look up. Table 1 awards pathogenicity and Table 2 benignity, and SM20's prose cites the
        two by the wrong numbers, so a transposed pair is the live error.
        """
        if self.direction not in CONTROL_RANGES:
            raise ReferenceDataError(f'control range {self.direction!r} is not one of {list(CONTROL_RANGES)}')
        benign_counts = sorted({benign for benign, _ in self.cells})
        pathogenic_counts = sorted({pathogenic for _, pathogenic in self.cells})
        for axis, counts in (('rows', benign_counts), ('columns', pathogenic_counts)):
            if counts != list(range(len(counts))):
                raise ReferenceDataError(f'{self.direction} table {axis} {counts} are not the control counts from 0 up')
        if len(benign_counts) != len(pathogenic_counts) or len(self.cells) != len(benign_counts) * len(
            pathogenic_counts
        ):
            raise ReferenceDataError(
                f'{self.direction} table states {len(self.cells)} cells over {len(benign_counts)} benign and '
                f'{len(pathogenic_counts)} pathogenic control counts; the axes are both control counts, so the '
                'grid is square'
            )
        pathogenic_direction = self.direction == 'pathogenic'
        for cell, points in self.cells.items():
            if (points < 0) if pathogenic_direction else (points > 0):
                raise ReferenceDataError(
                    f'the {self.direction} table states {points} at {cell}, which awards evidence in one direction only'
                )


@dataclasses.dataclass(frozen=True)
class OddsPathLevel:
    """One Tavtigian calibration step: an OddsPath ratio and its point value.

    Attributes:
        strength: The strength label the ratio calibrates.
        odds_path: The ratio as the framework prints it, "18.7:1" pathogenic or "1:2.08" benign.
        odds: That ratio as a scalar, above 1 pathogenic and below 1 benign; None for Indeterminate,
            which the framework prints no ratio for.
        points: The points the level is worth.
    """

    strength: str
    odds_path: str
    odds: decimal.Decimal | None
    points: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class CitedDocuments:
    """The document set this reference's citations resolve against.

    The revision is a full git commit id: an SM<n> §<m> citation names a line in a text extraction,
    and only a fixed tree settles which line that is.
    """

    repository: str
    revision: str
    note: str

    def __post_init__(self) -> None:
        for name in ('repository', 'revision', 'note'):
            value = getattr(self, name)
            if not value.strip():
                raise ReferenceDataError(f'cited_documents.{name} must be a non-empty string, got {value!r}')
        if not _COMMIT_REVISION.match(self.revision):
            raise ReferenceDataError(
                f'cited_documents.revision must be a full 40-character commit id, got {self.revision!r}'
            )


@dataclasses.dataclass(frozen=True)
class TranscriptionProvenance:
    """The transcription's own statement of what it is and how it is verified.

    Held at load, so no copy of the reference circulates without it.
    """

    what: str
    verified_against: str
    citation_form: str

    def __post_init__(self) -> None:
        for name in ('what', 'verified_against', 'citation_form'):
            value = getattr(self, name)
            if not value.strip():
                raise ReferenceDataError(f'provenance.{name} must be a non-empty string, got {value!r}')


@dataclasses.dataclass(frozen=True)
class ObservationColumn:
    """One column of a per-observation table.

    Attributes:
        cell: The column's cell-id fragment.
        heading: What the column states, as the framework names it.
    """

    cell: str
    heading: str


@dataclasses.dataclass(frozen=True)
class ObservationRow:
    """One per-observation row the framework prices at a single value.

    Attributes:
        cell: The row's cell-id fragment, which is how `observations` addresses it.
        description: What one observation in the row is, as the framework states it.
        points: What one such observation scores.
    """

    cell: str
    description: str
    points: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class ObservationGridRow:
    """One row of an `ObservationGrid`, priced per column.

    Attributes:
        cell: The row's cell-id fragment.
        description: What the row is, as the framework states it.
        points: What one observation scores, aligned to the grid's columns.
    """

    cell: str
    description: str
    points: tuple[decimal.Decimal, ...]


@dataclasses.dataclass(frozen=True)
class ObservationGrid:
    """A per-observation table priced by row and column, the shape of SM4's Tables 1-3 and 5.

    The column vocabulary is stated once for the whole table, so a row cannot state its own columns
    and no row's values can be read against a heading it was not written under.

    Attributes:
        columns: The columns, in the order every row's values follow.
        rows: The rows priced per column.
        collapsed_rows: The rows a cell id addresses without a column fragment, because the table
            prices the row as a whole (SM4 Table 1's NOT_CONSISTENT row, Table 5's
            under-80%-penetrance band).
    """

    columns: tuple[ObservationColumn, ...]
    rows: tuple[ObservationGridRow, ...]
    collapsed_rows: tuple[ObservationRow, ...]

    def __post_init__(self) -> None:
        headings = [column.cell for column in self.columns]
        if not headings:
            raise ReferenceDataError('a per-observation table states no columns, so no row of it can be priced')
        if len(set(headings)) != len(headings):
            raise ReferenceDataError(f'the per-observation columns {headings} address the same cell twice')
        rows = [row.cell for row in self.rows] + [row.cell for row in self.collapsed_rows]
        if not rows or len(set(rows)) != len(rows):
            raise ReferenceDataError(f'the per-observation rows {sorted(rows)} address the same cell twice')
        for row in self.rows:
            if len(row.points) != len(self.columns):
                raise ReferenceDataError(
                    f'row {row.cell!r} states {len(row.points)} values for the {len(self.columns)} columns {headings}'
                )


@dataclasses.dataclass(frozen=True)
class HomozygousWeights:
    """POP_HMZ's per-observation tariffs (SM3 Table 7), by the MDE's inheritance.

    Attributes:
        dominant: One homozygous observation in an autosomal-dominant MDE.
        other: One homozygous or hemizygous observation in a semidominant, recessive or X-linked one.
    """

    dominant: ObservationRow
    other: ObservationRow


@dataclasses.dataclass(frozen=True)
class AlternateCauseRows:
    """CLN_ALT's three rows (SM4 Table 4): what an alternate cause of the phenotype is worth.

    Attributes:
        more_severe: A proband whose phenotype is more severe than the alternate cause explains.
        not_more_severe: A proband whose phenotype the alternate cause explains.
        not_consistent_recessive: A recessive MDE the observation is not consistent with, which the
            framework states for the variant axis only.
    """

    more_severe: ObservationRow
    not_more_severe: ObservationRow
    not_consistent_recessive: ObservationRow


@dataclasses.dataclass(frozen=True)
class PerObservationTables:
    """The framework's tables that price one observed individual rather than a variant.

    SM3 Table 7, SM4 Tables 1-5, SM5's yield bins and segregation figure state a value per observed
    individual, and a code's contribution is that value times how many individuals fall in the row.
    `observations` gives each row a cell id and reads its value here.

    Attributes:
        homozygous: POP_HMZ's per-observation tariffs.
        unaffected: CLN_UAF, by penetrance band.
        alternate_cause: CLN_ALT's three rows.
        affected_monoallelic: CLN_AFF Table 1, one monoallelic proband.
        affected_biallelic: CLN_AFF Table 2, one biallelic proband.
        de_novo: CLN_DNV Table 3, one de novo occurrence.
        diagnostic_yield: LOC_PHE's diagnostic-yield bins.
        cosegregation: LOC_SEG's per-cosegregation rows.
    """

    homozygous: HomozygousWeights
    unaffected: ObservationGrid
    alternate_cause: AlternateCauseRows
    affected_monoallelic: ObservationGrid
    affected_biallelic: ObservationGrid
    de_novo: ObservationGrid
    diagnostic_yield: tuple[ObservationRow, ...]
    cosegregation: tuple[ObservationRow, ...]


@dataclasses.dataclass(frozen=True)
class Reference:
    """The validated SVCv4 reference: the framework as the compute modules read it.

    Assembled once by `data.load_reference`, which is also where the checks across two of these
    structures run. A value travels with the words the framework states it in — a code's notes, a
    bin's printed ratio, a table's caption and the image it was read from. What stays behind in the
    `data` module that states the values is the descriptive aggregates: the prose rules, the
    crosswalk from v3's criteria, the provenance of a whole set of tables.
    """

    cited_documents: CitedDocuments
    provenance: TranscriptionProvenance
    class_order: tuple[str, ...]  # benign-to-pathogenic, e.g. ('B','LB','VUS','LP','P')
    bands: tuple[Band, ...]
    vus_subbands: tuple[Band, ...]
    gate: Mapping[gene_disease_pb2.GateLevel, GateRow]
    mechanism_factors: Mapping[str, decimal.Decimal]
    exon_factors: Mapping[str, decimal.Decimal]
    matrix_omitted_cell: tuple[str, str]  # (mechanism, exon) scored 0, not as the axis product
    codes: Mapping[str, CodeSpec]
    frequency_bins: tuple[FrequencyBin, ...]
    binning_grids: Mapping[str, BinningGrid]  # SM3 Tables 1-6, keyed by the title printed in the image
    clinical_pop_frq_precondition: PopFrqPrecondition  # SM4's POP_FRQ gate on the clinical tables
    critical_residue_max: decimal.Decimal  # SM7's ceiling on the critical-amino-acid award
    oddspath: tuple[OddsPathLevel, ...]
    control_counts: Mapping[str, ControlCountGrid]  # SM20 Tables 1-2, keyed by the control range they serve
    concept_caps: Mapping[str, CapRange]  # concept-level combining caps (e.g. MIS [-8,6], LOC [0,4])
    category_caps: Mapping[str, CapRange]  # category (PFD) parent-total caps (e.g. NUL_PFD [-8,10])
    concept_to_codes: Mapping[str, tuple[str, ...]]  # which codes each concept sums
    independent_families: frozenset[str]  # the families no variant-type path sums (`independent_families`)
    per_observation: PerObservationTables  # the tables priced per observed individual

    def code(self, name: str) -> CodeSpec:
        """Return the `CodeSpec` for an evidence code, raising if it is unknown."""
        try:
            return self.codes[name]
        except KeyError as e:
            raise ReferenceDataError(f'unknown evidence code {name!r}') from e

    def binning_grid(self, title: str) -> BinningGrid:
        """Return the SM3 binning table printed under this title, raising if it is unknown."""
        try:
            return self.binning_grids[title]
        except KeyError as e:
            raise ReferenceDataError(f'unknown SM3 binning table {title!r}') from e

    def control_count_grid(self, direction: str) -> ControlCountGrid:
        """Return the SM20 control-count table for a control range, raising if it is unknown."""
        try:
            return self.control_counts[direction]
        except KeyError as e:
            raise ReferenceDataError(f'unknown control range {direction!r}') from e

    def concept_cap(self, name: str) -> CapRange:
        """Return the concept-level combining cap, raising if it is unknown."""
        try:
            return self.concept_caps[name]
        except KeyError as e:
            raise ReferenceDataError(f'unknown evidence concept {name!r}') from e

    def category_cap(self, name: str) -> CapRange:
        """Return the category (PFD) parent-total cap, raising if it is unknown."""
        try:
            return self.category_caps[name]
        except KeyError as e:
            raise ReferenceDataError(f'unknown evidence category {name!r}') from e


def band_for(bands: Sequence[Band], code: str) -> Band:
    """Return the band a classification code occupies, raising if the bands define none."""
    band = next((b for b in bands if b.code == code), None)
    if band is None:
        raise ReferenceDataError(f'classification bands have no {code} band')
    return band


def validate_bands(bands: Sequence[Band], context: str, *, full_line: bool) -> None:
    """Fail loud unless the bands tile their span contiguously with complementary boundaries.

    The classification bands must cover the whole point line (`full_line`); the VUS sub-bands only
    tile the bounded VUS interval, so their outer bounds are finite. A gap or an overlap is a total
    that lands in no band or in two, which `band_for_total` cannot report as either.

    The order they are handed in is the order they are declared in, and that order is itself
    load-bearing: it is `Reference.class_order`, which `apply_gate` ranks a permitted class by. So
    the bands are checked as given, benign-to-pathogenic, rather than sorted first — a declaration
    that runs the other way would otherwise load clean and invert every gate cap.

    Args:
        bands: The bands to check, in declaration order.
        context: What is being checked, for the error message.
        full_line: Whether the outermost bounds must be unbounded.

    Raises:
        ReferenceDataError: If there are none, they run the wrong way, they leave a gap or overlap,
            or (under `full_line`) they stop short of either end.
    """
    if not bands:
        raise ReferenceDataError(f'{context} states no bands, so no total maps to a class')
    for left, right in itertools.pairwise(bands):
        if left.upper is None or right.lower is None or left.upper > right.lower:
            raise ReferenceDataError(
                f'{context} bands run {left.code} before {right.code}, which is not ascending: a band is '
                'declared above the one below it, and only the outermost two are unbounded'
            )
        if left.upper != right.lower or left.upper_inclusive == right.lower_inclusive:
            raise ReferenceDataError(f'{context} bands are not contiguous at {left.code}/{right.code}')
    if full_line and (bands[0].lower is not None or bands[-1].upper is not None):
        raise ReferenceDataError(f'{context} bands do not cover the full point line')


def validate_subbands(subbands: Sequence[Band], parent: Band, context: str) -> None:
    """Fail loud unless the (already contiguous) sub-bands' union exactly equals `parent`.

    Contiguity is `validate_bands`; this adds the outer-bound match, so a gap or overlap against the
    VUS band cannot leave `band_for_total` silently returning (VUS, None).

    Args:
        subbands: The sub-bands, in the ascending order `validate_bands` holds them to.
        parent: The band they subdivide.
        context: What is being checked, for the error message.

    Raises:
        ReferenceDataError: If there are none, or their union is not exactly `parent`.
    """
    if not subbands:
        raise ReferenceDataError(f'{context} states no sub-bands to partition the {parent.code} band')
    lowest, highest = subbands[0], subbands[-1]
    if (lowest.lower, lowest.lower_inclusive) != (parent.lower, parent.lower_inclusive) or (
        highest.upper,
        highest.upper_inclusive,
    ) != (parent.upper, parent.upper_inclusive):
        raise ReferenceDataError(f'{context} sub-bands do not partition the {parent.code} band')


def assemble_gate(rows: Sequence[GateRow], class_order: Sequence[str]) -> dict[gene_disease_pb2.GateLevel, GateRow]:
    """Key the gate rows by the contract's `GateLevel`, holding each row to the classification bands.

    A permitted class the bands do not define caps nothing — `apply_gate` ranks the allow-set against
    `class_order` — so it would silently widen or narrow the gate at score time instead of here. A
    level named twice would likewise resolve to whichever row was read last, and
    `GATE_LEVEL_UNSPECIFIED` is the absence of a curated level, so it gates nothing and no row may
    claim it.

    Args:
        rows: The authored gate rows.
        class_order: The classification codes, benign to pathogenic.

    Returns:
        The gate keyed by level.

    Raises:
        ReferenceDataError: If a level is no `GateLevel` member, is claimed twice, or is
            `GATE_LEVEL_UNSPECIFIED`, or a row permits a class the bands do not define.
    """
    gate: dict[gene_disease_pb2.GateLevel, GateRow] = {}
    for row in rows:
        name = gate_level_name(row.level)
        if row.level not in gene_disease_pb2.GateLevel.values():
            names = gene_disease_pb2.GateLevel.keys()
            unspecified = gate_level_name(gene_disease_pb2.GATE_LEVEL_UNSPECIFIED)
            curated = [member for member in names if member != unspecified]
            raise ReferenceDataError(f'a gate row names {name}; the levels one may name are {curated}')
        if row.level == gene_disease_pb2.GATE_LEVEL_UNSPECIFIED:
            raise ReferenceDataError(f'a gate row names {name}, which is the absence of a level and gates nothing')
        if row.level in gate:
            raise ReferenceDataError(f'the gate names {name} twice')
        unknown = sorted(row.allows - set(class_order))
        if unknown:
            raise ReferenceDataError(
                f'gate level {name} permits {unknown}, which the classification bands do not define'
            )
        gate[row.level] = row
    return gate


def validate_factor_axis(factors: Mapping[str, decimal.Decimal], levels: frozenset[str], context: str) -> None:
    """Fail loud unless a matrix axis states exactly the framework's levels for it.

    The levels are what a caller names to select a multiplier, so a renamed or dropped one is not a
    missing factor — it is a `KeyError` raised mid-score, on a variant, from a lookup the caller had
    no way to know would fail.

    Args:
        factors: The axis, level to multiplier.
        levels: The framework's levels for that axis.
        context: Which axis, for the error message.

    Raises:
        ReferenceDataError: If the axis states any other set of levels.
    """
    if factors.keys() != levels:
        raise ReferenceDataError(f'{context} states levels {sorted(factors)}, expected exactly {sorted(levels)}')


def validate_omitted_cell(cell: tuple[str, str], mechanisms: frozenset[str], exons: frozenset[str]) -> None:
    """Fail loud unless the cell the framework scores 0 names a level of both axes.

    A cell naming a level neither axis has is a cell that can never match, so the axis product would
    be applied silently where the framework scores 0.

    Args:
        cell: The (mechanism, exon) cell scored 0 rather than as the axis product.
        mechanisms: The mechanism axis's levels.
        exons: The exon axis's levels.

    Raises:
        ReferenceDataError: If either half of the cell is off its axis.
    """
    mechanism, exon = cell
    if mechanism not in mechanisms or exon not in exons:
        raise ReferenceDataError(f'the omitted cell names {mechanism!r} x {exon!r}, which the axes do not both hold')


def _printed_ratio(bin_: FrequencyBin, above: FrequencyBin | None) -> str:
    """A bin as SM3 prints it: the open interval between its own edge and the next bin's.

    The rendered boundary is SM3's open one (`>`) while `min_multiple` is the closed lower edge the
    bins are scored on — the deliberate difference `FrequencyBin` documents, not a mismatch.

    Args:
        bin_: The bin.
        above: The bin above it, or None for the last one, which SM3 leaves open.

    Returns:
        The interval as SM3 states it.
    """
    if above is None:
        return f'> {bin_.min_multiple}x DAFT'
    if bin_.min_multiple == 0:
        return f'< {above.min_multiple}x DAFT'
    return f'> {bin_.min_multiple}x to < {above.min_multiple}x DAFT'


def validate_frequency_bins(bins: Sequence[FrequencyBin]) -> None:
    """Fail loud unless the POP_FRQ bins are the framework's four, ascending from a ratio of 0.

    The bins are walked in order and the last one whose threshold the ratio clears is the one that
    scores, so a bin out of order maps a ratio onto another bin's points, and a missing bin leaves a
    ratio scored by the one below it. Each bin's printed interval is rendered back from the two edges
    it lies between and held to what the bin states, so the label a worksheet shows and the threshold
    that scores cannot drift apart.

    Args:
        bins: The authored bins, in the order they are walked.

    Raises:
        ReferenceDataError: If the count is wrong, the first bin does not start at 0, the thresholds
            are not strictly ascending, or a printed interval is not the one its edges describe.
    """
    if len(bins) != _FREQUENCY_BIN_COUNT:
        raise ReferenceDataError(f'SM3 states {_FREQUENCY_BIN_COUNT} POP_FRQ bins, got {len(bins)}')
    if bins[0].min_multiple != 0:
        raise ReferenceDataError(
            f'the first POP_FRQ bin covers every ratio below the next threshold, so it starts at 0, not '
            f'{bins[0].min_multiple}'
        )
    _validate_strictly_ordered([bin_.min_multiple for bin_ in bins], 'POP_FRQ bin thresholds', ascending=True)
    for index, bin_ in enumerate(bins):
        above = bins[index + 1] if index + 1 < len(bins) else None
        printed = _printed_ratio(bin_, above)
        if printed != bin_.ratio:
            raise ReferenceDataError(
                f'the POP_FRQ bin scoring {bin_.points} spans {printed!r}, and SM3 prints {bin_.ratio!r}'
            )


def validate_oddspath(levels: Sequence[OddsPathLevel]) -> None:
    """Hold each calibration step to the direction its ratio and its points both state.

    The two say one thing twice — a ratio above 1 is pathogenic evidence and so are positive points —
    and `functional.oddspath_points` walks the scale by the ratio to arrive at the points. A step
    whose halves disagree hands back the other direction's weight, and a step stating no ratio is the
    framework's Indeterminate, which scores nothing.

    Args:
        levels: The authored scale.

    Raises:
        ReferenceDataError: If a step with no ratio scores anything, or a ratio and its points point
            opposite ways.
    """
    for level in levels:
        if level.odds is None:
            if level.points != 0:
                raise ReferenceDataError(
                    f'{level.strength} scores {level.points} while stating no OddsPath ratio; the step with no '
                    'ratio is the indeterminate one, which scores nothing'
                )
            continue
        if (level.odds > 1) != (level.points > 0) or (level.odds < 1) != (level.points < 0):
            raise ReferenceDataError(
                f'{level.strength} states OddsPath {level.odds_path} and {level.points} points, which point '
                'opposite ways'
            )


def validate_pop_frq_precondition(precondition: PopFrqPrecondition, codes: Mapping[str, CodeSpec]) -> None:
    """Hold SM4's POP_FRQ gate to the codes and the range it is stated over.

    Both checks guard a gate that would otherwise fire on every classification or on none: a
    conditioned code the transcription does not define is never in a tally to withdraw, and an
    admissible value outside the POP_FRQ range is one the code can never be assigned.

    Args:
        precondition: The precondition.
        codes: The evidence codes.

    Raises:
        ReferenceDataError: If a conditioned code is undefined, POP_FRQ itself is, or an admissible
            value falls outside POP_FRQ's range.
    """
    unknown = sorted(precondition.conditioned_codes - codes.keys())
    if unknown:
        raise ReferenceDataError(f'the POP_FRQ precondition conditions {unknown}, which the reference has no code for')
    if 'POP_FRQ' not in codes:
        raise ReferenceDataError('the POP_FRQ precondition is stated over POP_FRQ, which the reference has no code for')
    pop_frq = codes['POP_FRQ']
    outside = sorted(
        str(points) for points in precondition.admissible_points if not pop_frq.low <= points <= pop_frq.high
    )
    if outside:
        raise ReferenceDataError(
            f'the POP_FRQ precondition admits {outside}, outside the POP_FRQ range [{pop_frq.low}, {pop_frq.high}]'
        )


def validate_critical_residue_award(*, standalone_code: bool, max_points: decimal.Decimal) -> None:
    """Hold SM7's critical-amino-acid award to the way it is scored.

    The award is a modifier on a predictive code, bounded by that code's family concept cap, so a
    transcription promoting it to a code of its own would give it a range this modelling never
    consults. A non-positive ceiling withholds an award the framework states.

    Args:
        standalone_code: Whether the framework gives the award a code of its own.
        max_points: The ceiling on the award.

    Raises:
        ReferenceDataError: If the award is a standalone code, or its ceiling is not positive.
    """
    if standalone_code:
        raise ReferenceDataError(
            'the critical-residue award is stated as a standalone code; it is scored as a modifier on the '
            "predictive code, bounded by that code's family concept cap"
        )
    if max_points <= 0:
        raise ReferenceDataError(f'the critical-residue ceiling must be positive, got {max_points}')


def validate_concept_to_codes(concept_to_codes: Mapping[str, tuple[str, ...]], codes: Mapping[str, CodeSpec]) -> None:
    """Hold every concept to the family of the codes it groups.

    The grouping is the framework's own statement of which families a variant-type path sums, which
    `independent_families` reads off it. A concept grouping a code of another family would move that
    family to the wrong side of the split, admitting a path's code where nothing bounds it.

    Args:
        concept_to_codes: Which codes each concept sums.
        codes: The evidence codes.

    Raises:
        ReferenceDataError: If a grouped code is undefined or belongs to another family.
    """
    for concept, names in concept_to_codes.items():
        for name in names:
            if name not in codes:
                raise ReferenceDataError(f'concept {concept} groups unknown code {name!r}')
            if codes[name].family != concept:
                raise ReferenceDataError(f'concept {concept} groups {name!r}, whose family is {codes[name].family!r}')


def independent_families(
    codes: Mapping[str, CodeSpec], concept_to_codes: Mapping[str, tuple[str, ...]]
) -> frozenset[str]:
    """The code families no variant-type path sums, whose codes are therefore scored on their own.

    `concept_to_codes` names the families a path sums under one parent code — the families the
    builders place on paths, where the concept and category caps bound them. A code of any other
    family (POP, CLN, LOC) reaches the tally on its own, under its per-code range alone, which is the
    distinction `classify` holds an independent code to.

    Args:
        codes: The evidence codes.
        concept_to_codes: Which codes each concept sums.

    Returns:
        The families nothing groups.
    """
    return frozenset({spec.family for spec in codes.values()} - concept_to_codes.keys())


def assemble_binning_grids(grids: Sequence[BinningGrid]) -> dict[str, BinningGrid]:
    """Key SM3's binning tables by the title printed in each image.

    A curation block names the table it read by that title, so two tables under one title would let a
    later one answer for an earlier one. Each table's own number is held to its position, because the
    number is how SM3's prose points at it.

    Args:
        grids: The tables, in SM3's order.

    Returns:
        The tables keyed by title.

    Raises:
        ReferenceDataError: If a number disagrees with its position, or two tables share a title.
    """
    keyed: dict[str, BinningGrid] = {}
    for position, grid in enumerate(grids, start=1):
        if grid.number != position:
            raise ReferenceDataError(f'SM3 table numbered {grid.number} sits at position {position}')
        if grid.title in keyed:
            raise ReferenceDataError(f'two SM3 binning tables are titled {grid.title!r}')
        keyed[grid.title] = grid
    return keyed


def assemble_control_counts(grids: Sequence[ControlCountGrid]) -> dict[str, ControlCountGrid]:
    """Key SM20's control-count tables by the control range each serves.

    `functional.fxn_from_controls` selects a grid by that range, so exactly the two SM20 prints have
    to be present: a missing one is a KeyError raised mid-score, and two for one range would let the
    second answer for the first.

    Args:
        grids: The tables, in SM20's order.

    Returns:
        The tables keyed by direction.

    Raises:
        ReferenceDataError: If a number disagrees with its position, or the directions are not
            exactly `CONTROL_RANGES`.
    """
    keyed: dict[str, ControlCountGrid] = {}
    for position, grid in enumerate(grids, start=1):
        if grid.number != position:
            raise ReferenceDataError(f'SM20 table numbered {grid.number} sits at position {position}')
        if grid.direction in keyed:
            raise ReferenceDataError(f'two SM20 tables serve the {grid.direction} control range')
        keyed[grid.direction] = grid
    if tuple(sorted(keyed)) != tuple(sorted(CONTROL_RANGES)):
        raise ReferenceDataError(f'SM20 states the control ranges {sorted(keyed)}, expected {sorted(CONTROL_RANGES)}')
    return keyed
