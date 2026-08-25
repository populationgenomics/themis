"""Load and validate the SVCv4 machine-readable reference into typed structures.

The reference (`data/svcv4_scoring_reference.json`) encodes the whole framework as data:
classification bands and VUS sub-bands, the Tavtigian odds-to-points calibration, the
gene-disease-validity gate, the molecular-mechanism x exon-relevance matrix, the POP_FRQ bins, the
per-code point ranges, SM4's POP_FRQ precondition on the clinical tables, and SM7's ceiling on the
critical-residue award. This module parses it into validated dataclasses and fails loud on a missing
or renamed key so an edit that drops or renames a field is caught at load, not at score time.

Every value in the file cites the supplement line it is read from, and `meta.cited_documents` pins
the revision of the document set those citations address. This module requires the pin and exposes
it as `Reference.cited_documents`: an SM<n> §<m> citation is a line number, so without a fixed
revision no citation in the file resolves, and its absence is a load error rather than a missing
convenience. `meta.provenance`, the file's own statement of what it is, is required the same way.

What that costs is a stated expectation for everything the file is read *by name* for, since a file
being validated cannot also be what says which names are right: the gate's levels are resolved
against `themis.rpc.gene_disease_pb2.GateLevel`, its permitted classes are checked against the
classification bands, each matrix axis against the framework's levels for it, and the zero-scored
matrix cell against both axes. Anything narrower fails at score time instead — a dropped key reads
as an absence the code fills in, and a renamed one as a `KeyError` from a lookup made on a variant.

Point values are loaded as `decimal.Decimal` (via json's `parse_float` hook) so the tally is exact
and auditable: every SVCv4 point value and matrix multiplier is a terminating decimal, so decimal
arithmetic never introduces the binary-float rounding that could shift a total across a band
boundary.

Framework conflicts resolved here:

  1. The `mechanism_exon_matrix` Suspected x Most cell. SM18's narrative elects not to create
     fractions this small (12.5%); its Figure 1 states 0%. The figure is used, and `omitted_cell`
     says which reading that is at the cell: a description naming the uncreated axis product hands
     a reader a multiplier to apply instead.
  2. `CLN_DNV`'s upper bound. SM4 sums de novo occurrences across probands and states no cap on the
     sum; its +7.0 is the highest weight for one proband, and no CLN code there carries a
     cross-proband cap. The upper side is the `"sum"` sentinel, as `CLN_AFF`, `CLN_ALT` and
     `CLN_UAF` carry. The ClinGen pilot calculator states [0, 12]; the oracle pins the divergence.
"""

from __future__ import annotations

import dataclasses
import decimal
import itertools
import json
import pathlib
import re
import typing

from themis.rpc import gene_disease_pb2

_DEFAULT_DATA = pathlib.Path(__file__).parent / 'data' / 'svcv4_scoring_reference.json'

# How the reference spells a side the framework states no bound on, in a per-code range or a cap.
_UNBOUNDED = 'sum'

# A band's point range in the reference is one or two comparator clauses joined by " to ",
# e.g. "<= -4.0", "> -1.0 to < +6.0", ">= +10.0". This matches one clause.
_CLAUSE = re.compile(r'(?P<op><=|>=|<|>)\s*(?P<value>[+-]?\d+(?:\.\d+)?)')

# A POP_FRQ bin's lower DAFT-multiple boundary, e.g. the "1.5" in "> 1.5x to < 5x DAFT". The first
# bin ("< 1.5x DAFT") states no ">" clause, so has no lower boundary.
_RATIO_LOWER = re.compile(r'>\s*=?\s*(?P<value>\d+(?:\.\d+)?)\s*x')

# The cell `mechanism_exon_matrix.omitted_cell` names, whose prose leads with "<mechanism> x <exon>".
_OMITTED_CELL = re.compile(r'(?P<mechanism>\w+)\s*x\s*(?P<exon>\w+)')

# A full git commit id. A branch moves and a short id can grow ambiguous, so neither fixes the line
# an SM citation names.
_COMMIT_REVISION = re.compile(r'^[0-9a-f]{40}$')

# A binning-table axis label as SM3 prints it: a prevalence row "1/10,000", a penetrance column "50%".
_PREVALENCE_BIN = re.compile(r'1/(?P<denominator>[\d,]+)')
_PENETRANCE_COLUMN = re.compile(r'(?P<percent>\d+)%')

# SM18's two matrix axes, declared rather than read off the file this module validates. Restated
# here and not imported because `scoring` — where the same vocabulary is the enum a caller selects a
# multiplier with — imports this module; a test holds the two together.
MECHANISM_LEVELS = frozenset({'Established', 'Likely', 'Suspected', 'Unlikely', 'Unknown', 'Uncertain'})
EXON_LEVELS = frozenset({'All', 'Most', 'Few'})


class ReferenceDataError(Exception):
    """The reference file is missing, malformed, or has a renamed/removed key."""


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
    """An evidence code's family and its fixed point range `[low, high]` (its per-code cap).

    The family is the code's prefix as the reference states it (POP, CLN, LOC, MIS, CDS, NUL, SPL);
    which of them a variant-type path sums is `Reference.independent_families`.
    """

    code: str
    family: str
    low: decimal.Decimal
    high: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class CapRange:
    """A framework combining cap `[low, high]`; an unbounded side is -Infinity / +Infinity."""

    low: decimal.Decimal
    high: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class FrequencyBin:
    """One POP_FRQ bin: an inclusive-lower DAFT-multiple threshold mapping to points.

    `min_multiple` is the smallest FAF/DAFT ratio (inclusive) that earns `points`; the reference
    states the bins with open boundaries at 1.5x/5x/15x, closed here to the lower edge so the bins
    partition the ratio line with no gaps (see SM3 Figure 1's worked FBN1 thresholds).
    """

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
        marked: The cells SM3 prints with a `*` it defines nowhere (`binning.marker_note`).
    """

    number: int
    caption: str
    title: str
    applies_to: str
    prevalence_denominators: tuple[int, ...]
    penetrances: tuple[decimal.Decimal, ...]
    cells: dict[tuple[int, decimal.Decimal], decimal.Decimal]
    marked: frozenset[tuple[int, decimal.Decimal]]


@dataclasses.dataclass(frozen=True)
class PopFrqPrecondition:
    """The POP_FRQ assignments SM4 conditions a clinical code's scoring tables on.

    A conditioned code scored beside any other POP_FRQ value, or beside none at all, is one the
    framework withdraws rather than one worth fewer points.
    """

    conditioned_codes: frozenset[str]
    admissible_points: frozenset[decimal.Decimal]


@dataclasses.dataclass(frozen=True)
class ControlCountGrid:
    """One of SM20's two control-count lookup tables: a benign x pathogenic grid of FXN points.

    Attributes:
        number: SM20's table number, 1 (pathogenicity) or 2 (benignity).
        direction: Which control range the test variant's result falls in for this table to apply.
        caption: The caption SM20 prints above the image, verbatim.
        applies_to: The case the table serves.
        cells: (benign controls, pathogenic controls) -> the points that cell states.
    """

    number: int
    direction: str
    caption: str
    applies_to: str
    cells: dict[tuple[int, int], decimal.Decimal]


@dataclasses.dataclass(frozen=True)
class OddsPathLevel:
    """One Tavtigian calibration step: an OddsPath ratio and its point value."""

    strength: str
    odds: decimal.Decimal | None  # pathogenic direction > 1, benign < 1; None for Indeterminate
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


@dataclasses.dataclass(frozen=True)
class TranscriptionProvenance:
    """The file's own statement of what it is and how it is verified.

    Required at load, so no copy of the reference circulates without it.
    """

    what: str
    verified_against: str
    citation_form: str


@dataclasses.dataclass(frozen=True)
class Reference:
    """The validated SVCv4 reference.

    Holds the framework as typed structures plus the parsed source dict (`raw`) for the descriptive
    tables (routing, MIS_INF sub-rule text, multiple-disorder policy) that the compute modules read
    but do not need re-typed.
    """

    cited_documents: CitedDocuments
    provenance: TranscriptionProvenance
    class_order: tuple[str, ...]  # benign-to-pathogenic, e.g. ('B','LB','VUS','LP','P')
    bands: tuple[Band, ...]
    vus_subbands: tuple[Band, ...]
    gate: dict[gene_disease_pb2.GateLevel, GateRow]
    mechanism_factors: dict[str, decimal.Decimal]
    exon_factors: dict[str, decimal.Decimal]
    matrix_omitted_cell: tuple[str, str]  # (mechanism, exon) scored 0, not as the axis product
    codes: dict[str, CodeSpec]
    frequency_bins: tuple[FrequencyBin, ...]
    binning_grids: dict[str, BinningGrid]  # SM3 Tables 1-6, keyed by the title printed in the image
    clinical_pop_frq_precondition: PopFrqPrecondition  # SM4's POP_FRQ gate on the clinical tables
    critical_residue_max: decimal.Decimal  # SM7's ceiling on the critical-amino-acid award
    oddspath: tuple[OddsPathLevel, ...]
    control_counts: dict[str, ControlCountGrid]  # SM20 Tables 1-2, keyed by the control range they serve
    concept_caps: dict[str, CapRange]  # concept-level combining caps (e.g. MIS [-8,6], LOC [0,4])
    category_caps: dict[str, CapRange]  # category (PFD) parent-total caps (e.g. NUL_PFD [-8,10])
    concept_to_codes: dict[str, tuple[str, ...]]  # which codes each concept sums
    independent_families: frozenset[str]  # the families no variant-type path sums (`_independent_families`)
    raw: dict[str, object]

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


def _as_dict(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReferenceDataError(f'expected an object at {context}, got {type(value).__name__}')
    return value


def _as_list(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ReferenceDataError(f'expected a list at {context}, got {type(value).__name__}')
    return value


def _require(mapping: dict[str, object], key: str, context: str) -> object:
    if key not in mapping:
        raise ReferenceDataError(f'reference missing {context}.{key}')
    return mapping[key]


def _as_decimal(value: object, context: str) -> decimal.Decimal:
    if isinstance(value, decimal.Decimal):
        return value
    if isinstance(value, int):
        return decimal.Decimal(value)
    raise ReferenceDataError(f'expected a number at {context}, got {value!r}')


def _as_bound(value: object, *, is_low: bool, context: str) -> decimal.Decimal:
    """One side of a range or cap, `"sum"` meaning the framework states no bound on that side.

    The file's own convention, used by the codes whose weights are per observation and accumulate
    (`CLN_AFF` upward across probands, `CLN_ALT` and `CLN_UAF` downward across individuals) and by
    the concepts above them. Any other string is a typo that would otherwise read as an unbounded
    side, which no clamp would ever bite on.
    """
    if isinstance(value, str):
        if value != _UNBOUNDED:
            raise ReferenceDataError(f'{context} is {value!r}; an unbounded side is spelled {_UNBOUNDED!r}')
        return decimal.Decimal('-Infinity') if is_low else decimal.Decimal('Infinity')
    return _as_decimal(value, context)


def _build_cited_documents(payload: dict[str, object]) -> CitedDocuments:
    meta = _as_dict(_require(payload, 'meta', 'reference'), 'meta')
    block = _as_dict(_require(meta, 'cited_documents', 'meta'), 'meta.cited_documents')
    fields = {}
    for key in ('repository', 'revision', 'note'):
        value = _require(block, key, 'meta.cited_documents')
        if not isinstance(value, str) or not value.strip():
            raise ReferenceDataError(f'meta.cited_documents.{key} must be a non-empty string, got {value!r}')
        fields[key] = value
    if not _COMMIT_REVISION.match(fields['revision']):
        raise ReferenceDataError(
            f'meta.cited_documents.revision must be a full 40-character commit id, got {fields["revision"]!r}'
        )
    return CitedDocuments(**fields)


def _build_transcription_provenance(payload: dict[str, object]) -> TranscriptionProvenance:
    meta = _as_dict(_require(payload, 'meta', 'reference'), 'meta')
    block = _as_dict(_require(meta, 'provenance', 'meta'), 'meta.provenance')
    fields = {}
    for key in ('what', 'verified_against', 'citation_form'):
        value = _require(block, key, 'meta.provenance')
        if not isinstance(value, str) or not value.strip():
            raise ReferenceDataError(f'meta.provenance.{key} must be a non-empty string, got {value!r}')
        fields[key] = value
    return TranscriptionProvenance(**fields)


def _parse_range(text: str) -> tuple[decimal.Decimal | None, bool, decimal.Decimal | None, bool]:
    """Parse a band range string into (lower, lower_inclusive, upper, upper_inclusive)."""
    lower: decimal.Decimal | None = None
    upper: decimal.Decimal | None = None
    lower_inclusive = False
    upper_inclusive = False
    for part in text.split(' to '):
        clause = _CLAUSE.fullmatch(part.strip())
        if clause is None:
            raise ReferenceDataError(f'unparseable band range {text!r}')
        op = clause.group('op')
        value = decimal.Decimal(clause.group('value'))
        if op in ('>', '>='):
            lower, lower_inclusive = value, op == '>='
        else:
            upper, upper_inclusive = value, op == '<='
    return lower, lower_inclusive, upper, upper_inclusive


def _build_bands(entries: list[object], context: str, *, full_line: bool) -> tuple[Band, ...]:
    bands = []
    for raw_entry in entries:
        entry = _as_dict(raw_entry, context)
        code = str(_require(entry, 'code', context))
        points = str(_require(entry, 'points', f'{context}[{code}]'))
        lower, low_inc, upper, up_inc = _parse_range(points)
        bands.append(Band(code=code, lower=lower, lower_inclusive=low_inc, upper=upper, upper_inclusive=up_inc))
    _validate_partition(bands, context, full_line=full_line)
    return tuple(bands)


def _validate_partition(bands: list[Band], context: str, *, full_line: bool) -> None:
    """Fail loud unless the bands tile their span contiguously with complementary boundaries.

    The classification bands must cover the whole point line (`full_line`); the VUS sub-bands only
    tile the bounded VUS interval, so their outer bounds are finite.
    """
    ordered = sorted(bands, key=lambda b: (b.lower is not None, b.lower if b.lower is not None else 0))
    if full_line and (ordered[0].lower is not None or ordered[-1].upper is not None):
        raise ReferenceDataError(f'{context} bands do not cover the full point line')
    for left, right in itertools.pairwise(ordered):
        if left.upper != right.lower or left.upper_inclusive == right.lower_inclusive:
            raise ReferenceDataError(f'{context} bands are not contiguous at {left.code}/{right.code}')


def _band(bands: tuple[Band, ...], code: str) -> Band:
    band = next((b for b in bands if b.code == code), None)
    if band is None:
        raise ReferenceDataError(f'classification bands have no {code} band')
    return band


def _validate_subpartition(subbands: tuple[Band, ...], parent: Band, context: str) -> None:
    """Fail loud unless the (already contiguous) sub-bands' union exactly equals `parent`.

    Contiguity is checked when the sub-bands are built; this adds the outer-bound match so a gap or
    overlap against the VUS band cannot leave `band_for_total` silently returning (VUS, None).
    """
    ordered = sorted(subbands, key=lambda b: (b.lower is not None, b.lower if b.lower is not None else 0))
    lowest, highest = ordered[0], ordered[-1]
    if (lowest.lower, lowest.lower_inclusive) != (parent.lower, parent.lower_inclusive) or (
        highest.upper,
        highest.upper_inclusive,
    ) != (parent.upper, parent.upper_inclusive):
        raise ReferenceDataError(f'{context} sub-bands do not partition the {parent.code} band')


def _gate_level(name: str, context: str) -> gene_disease_pb2.GateLevel:
    """Resolve a row's `level` against the contract enum.

    `GATE_LEVEL_UNSPECIFIED` is the absence of a curated level, so it gates nothing and no row may
    claim it — which is also why it is left out of the names a failure offers as the repair.
    """
    unspecified = gene_disease_pb2.GateLevel.Name(gene_disease_pb2.GATE_LEVEL_UNSPECIFIED)
    if name == unspecified:
        raise ReferenceDataError(f'{context} names {name}, which is the absence of a level and gates nothing')
    try:
        value = gene_disease_pb2.GateLevel.Value(name)
    except ValueError as e:
        members = gene_disease_pb2.GateLevel.keys()
        curated = [member for member in members if member != unspecified]
        raise ReferenceDataError(f'{context} names {name!r}, which is no GateLevel member; they are {curated}') from e
    return typing.cast('gene_disease_pb2.GateLevel', value)


def _build_gate(payload: dict[str, object], class_order: tuple[str, ...]) -> dict[gene_disease_pb2.GateLevel, GateRow]:
    """The gate table, keyed by the contract's `GateLevel`, every permitted class checked against the bands.

    A permitted class the bands do not define caps nothing — `apply_gate` ranks the allow-set against
    `class_order` — so it would silently widen or narrow the gate at score time instead of here. A
    level named twice would likewise resolve to whichever row was read last.
    """
    gate: dict[gene_disease_pb2.GateLevel, GateRow] = {}
    for raw_entry in _as_list(
        _require(payload, 'levels', 'gene_disease_validity_gate'), 'gene_disease_validity_gate.levels'
    ):
        entry = _as_dict(raw_entry, 'gene_disease_validity_gate.levels')
        name = str(_require(entry, 'level', 'gene_disease_validity_gate.levels'))
        context = f'gene_disease_validity_gate.levels[{name}]'
        level = _gate_level(name, context)
        if level in gate:
            raise ReferenceDataError(f'gene_disease_validity_gate.levels names {name} twice')
        allows = frozenset(str(a) for a in _as_list(_require(entry, 'allows', context), f'{context}.allows'))
        unknown = sorted(allows - set(class_order))
        if unknown:
            raise ReferenceDataError(f'{context}.allows names {unknown}, which the classification bands do not define')
        result = entry.get('result')
        gate[level] = GateRow(level=level, allows=allows, result=None if result is None else str(result))
    return gate


def _build_factors(matrix: dict[str, object], key: str, levels: frozenset[str]) -> dict[str, decimal.Decimal]:
    """One matrix axis, its keys held to the framework's levels for that axis.

    The levels are what a caller names to select a multiplier, so a renamed or dropped key is not a
    missing factor — it is a `KeyError` raised mid-score, on a variant, from a lookup the caller had
    no way to know would fail.
    """
    context = f'mechanism_exon_matrix.{key}'
    axis = _as_dict(_require(matrix, key, 'mechanism_exon_matrix'), context)
    factors = {str(k): _as_decimal(v, f'{context}.{k}') for k, v in axis.items()}
    if factors.keys() != levels:
        raise ReferenceDataError(f'{context} states levels {sorted(factors)}, expected exactly {sorted(levels)}')
    return factors


def _omitted_cell(matrix: dict[str, object], mechanisms: frozenset[str], exons: frozenset[str]) -> tuple[str, str]:
    """The (mechanism, exon) cell the framework scores 0 rather than as the axis product.

    Cross-checked against both axes: a cell naming a level neither axis has is a cell that can never
    match, so the axis product would be applied silently where the framework scores 0.
    """
    stated = str(_require(matrix, 'omitted_cell', 'mechanism_exon_matrix'))
    named = _OMITTED_CELL.match(stated)
    if named is None:
        raise ReferenceDataError(f'mechanism_exon_matrix.omitted_cell does not name a cell: {stated!r}')
    mechanism, exon = named.group('mechanism'), named.group('exon')
    if mechanism not in mechanisms or exon not in exons:
        raise ReferenceDataError(
            f'mechanism_exon_matrix.omitted_cell names {mechanism!r} x {exon!r}, which the axes do not both hold'
        )
    return mechanism, exon


def _build_codes(payload: dict[str, object]) -> dict[str, CodeSpec]:
    codes = {}
    for name, raw_spec in payload.items():
        spec = _as_dict(raw_spec, f'evidence_codes.{name}')
        family = _require(spec, 'family', f'evidence_codes.{name}')
        if not isinstance(family, str) or not family.strip():
            raise ReferenceDataError(f'evidence_codes.{name}.family must be a non-empty string, got {family!r}')
        rng = _as_list(_require(spec, 'range', f'evidence_codes.{name}'), f'evidence_codes.{name}.range')
        low = _as_bound(rng[0], is_low=True, context=f'evidence_codes.{name}.range[0]')
        high = _as_bound(rng[1], is_low=False, context=f'evidence_codes.{name}.range[1]')
        codes[name] = CodeSpec(code=name, family=family, low=low, high=high)
    return codes


def _build_frequency_bins(payload: dict[str, object]) -> tuple[FrequencyBin, ...]:
    pop_frq = _as_dict(_require(payload, 'POP_FRQ', 'population_frequency'), 'population_frequency.POP_FRQ')
    bins = _as_list(_require(pop_frq, 'bins', 'population_frequency.POP_FRQ'), 'population_frequency.POP_FRQ.bins')
    # The reference states each bin's ratio as an open interval ("< 1.5x", "> 1.5x to < 5x", ...);
    # the lower multiple named in each becomes its inclusive lower edge so the bins partition the
    # ratio line. Each JSON bin's stated lower boundary is cross-checked against these positional
    # multiples, so a reordered reference fails loud instead of silently mis-mapping points.
    multiples = [decimal.Decimal('0'), decimal.Decimal('1.5'), decimal.Decimal('5'), decimal.Decimal('15')]
    if len(bins) != len(multiples):
        raise ReferenceDataError(f'expected {len(multiples)} POP_FRQ bins, got {len(bins)}')
    parsed = []
    for multiple, raw_entry in zip(multiples, bins, strict=True):
        entry = _as_dict(raw_entry, 'population_frequency.POP_FRQ.bins')
        ratio = str(_require(entry, 'ratio', 'population_frequency.POP_FRQ.bins'))
        stated = _ratio_lower_multiple(ratio)
        if stated != multiple:
            raise ReferenceDataError(
                f'POP_FRQ bin {ratio!r} states lower multiple {stated}, expected {multiple} at this position'
            )
        points = _as_decimal(_require(entry, 'points', 'population_frequency.POP_FRQ.bins'), 'points')
        parsed.append(FrequencyBin(min_multiple=multiple, points=points))
    return tuple(parsed)


def _ratio_lower_multiple(ratio: str) -> decimal.Decimal:
    """The lower DAFT-multiple boundary named in a POP_FRQ bin's ratio string (0 if none stated)."""
    match = _RATIO_LOWER.search(ratio)
    return decimal.Decimal(match.group('value')) if match else decimal.Decimal('0')


def _binning_section(payload: dict[str, object]) -> tuple[dict[str, object], str]:
    pop_frq = _as_dict(_require(payload, 'POP_FRQ', 'population_frequency'), 'population_frequency.POP_FRQ')
    methods = _as_dict(
        _require(pop_frq, 'DAFT_methods', 'population_frequency.POP_FRQ'), 'population_frequency.POP_FRQ.DAFT_methods'
    )
    context = 'population_frequency.POP_FRQ.DAFT_methods.binning'
    return _as_dict(_require(methods, 'binning', 'population_frequency.POP_FRQ.DAFT_methods'), context), context


def _prevalence_denominator(label: str, context: str) -> int:
    match = _PREVALENCE_BIN.fullmatch(label)
    if not match:
        raise ReferenceDataError(f'{context}: prevalence bin {label!r} is not of the form "1/X"')
    return int(match.group('denominator').replace(',', ''))


def _penetrance_column(label: str, context: str) -> decimal.Decimal:
    match = _PENETRANCE_COLUMN.fullmatch(label)
    if not match:
        raise ReferenceDataError(f'{context}: penetrance column {label!r} is not a whole percentage')
    return decimal.Decimal(match.group('percent')) / 100


def _build_binning_grids(payload: dict[str, object]) -> dict[str, BinningGrid]:
    """Parse SM3 Tables 1-6 into grids keyed by the title printed in each image.

    The axes are stated once for all six tables and every table is held to them, so a row or column
    reaching one table and not the others fails at load rather than at the cell that is missing.
    """
    binning, context = _binning_section(payload)
    row_context, column_context = f'{context}.prevalence_bins', f'{context}.penetrance_columns'
    rows = [str(label) for label in _as_list(_require(binning, 'prevalence_bins', context), row_context)]
    columns = [str(label) for label in _as_list(_require(binning, 'penetrance_columns', context), column_context)]
    denominators = tuple(_prevalence_denominator(label, context) for label in rows)
    penetrances = tuple(_penetrance_column(label, context) for label in columns)
    if sorted(denominators) != list(denominators) or len(set(denominators)) != len(denominators):
        raise ReferenceDataError(f'{context}.prevalence_bins must be strictly rarest-last, got {rows}')
    if sorted(penetrances, reverse=True) != list(penetrances) or len(set(penetrances)) != len(penetrances):
        raise ReferenceDataError(f'{context}.penetrance_columns must be strictly most-penetrant-first, got {columns}')

    grids: dict[str, BinningGrid] = {}
    for position, entry in enumerate(_as_list(_require(binning, 'tables', context), f'{context}.tables'), start=1):
        grid = _build_binning_grid(
            _as_dict(entry, f'{context}.tables'), position, rows, columns, denominators, penetrances, context
        )
        if grid.title in grids:
            raise ReferenceDataError(f'{context}.tables: two tables titled {grid.title!r}')
        grids[grid.title] = grid
    return grids


def _build_binning_grid(
    entry: dict[str, object],
    position: int,
    rows: list[str],
    columns: list[str],
    denominators: tuple[int, ...],
    penetrances: tuple[decimal.Decimal, ...],
    context: str,
) -> BinningGrid:
    where = f'{context}.tables[{position - 1}]'
    number = _require(entry, 'number', where)
    if number != position:
        raise ReferenceDataError(f'{where}: table numbered {number!r} sits at position {position}')
    raw_cells = _as_dict(_require(entry, 'cells', where), f'{where}.cells')
    if list(raw_cells) != rows:
        raise ReferenceDataError(f'{where}.cells: rows {list(raw_cells)} are not the prevalence bins {rows}')
    cells: dict[tuple[int, decimal.Decimal], decimal.Decimal] = {}
    for denominator, row in zip(denominators, rows, strict=True):
        values = _as_list(raw_cells[row], f'{where}.cells[{row!r}]')
        if len(values) != len(columns):
            raise ReferenceDataError(
                f'{where}.cells[{row!r}] states {len(values)} values for {len(columns)} penetrance columns'
            )
        for penetrance, value in zip(penetrances, values, strict=True):
            threshold = _as_decimal(value, f'{where}.cells[{row!r}]')
            if threshold <= 0:
                raise ReferenceDataError(f'{where}.cells[{row!r}]: a DAFT must be positive, got {threshold}')
            cells[(denominator, penetrance)] = threshold
    marked = frozenset(
        _binning_cell_key(pair, rows, columns, denominators, penetrances, f'{where}.marked_cells')
        for pair in _as_list(_require(entry, 'marked_cells', where), f'{where}.marked_cells')
    )
    return BinningGrid(
        number=position,
        caption=str(_require(entry, 'caption', where)),
        title=str(_require(entry, 'title', where)),
        applies_to=str(_require(entry, 'applies_to', where)),
        prevalence_denominators=denominators,
        penetrances=penetrances,
        cells=cells,
        marked=marked,
    )


def _binning_cell_key(
    pair: object,
    rows: list[str],
    columns: list[str],
    denominators: tuple[int, ...],
    penetrances: tuple[decimal.Decimal, ...],
    context: str,
) -> tuple[int, decimal.Decimal]:
    """The (denominator, penetrance) a `[prevalence, penetrance]` label pair names, held to the axes."""
    labels = [str(label) for label in _as_list(pair, context)]
    if len(labels) != 2:
        raise ReferenceDataError(f'{context}: expected a [prevalence, penetrance] pair, got {labels}')
    row, column = labels
    if row not in rows or column not in columns:
        raise ReferenceDataError(f'{context}: cell {labels} is off the axes {rows} x {columns}')
    return denominators[rows.index(row)], penetrances[columns.index(column)]


def _build_pop_frq_precondition(payload: dict[str, object], codes: dict[str, CodeSpec]) -> PopFrqPrecondition:
    """SM4's POP_FRQ gate on the clinical tables, held to the codes and range it is stated over.

    Both cross-checks guard a gate that would otherwise fire on every classification or on none: a
    conditioned code `evidence_codes` does not define is never in a tally to withdraw, and an
    admissible value outside the POP_FRQ range is one the code can never be assigned.
    """
    context = 'clinical_observations.pop_frq_precondition'
    section = _as_dict(_require(payload, 'clinical_observations', 'reference'), 'clinical_observations')
    entry = _as_dict(_require(section, 'pop_frq_precondition', 'clinical_observations'), context)
    conditioned = frozenset(
        str(code) for code in _as_list(_require(entry, 'conditioned_codes', context), f'{context}.conditioned_codes')
    )
    admissible = frozenset(
        _as_decimal(points, f'{context}.admissible_pop_frq_points')
        for points in _as_list(
            _require(entry, 'admissible_pop_frq_points', context), f'{context}.admissible_pop_frq_points'
        )
    )
    if not conditioned or not admissible:
        raise ReferenceDataError(f'{context} must name at least one conditioned code and one admissible POP_FRQ value')
    unknown = sorted(conditioned - codes.keys())
    if unknown:
        raise ReferenceDataError(f'{context}.conditioned_codes names {unknown}, which evidence_codes does not define')
    if 'POP_FRQ' not in codes:
        raise ReferenceDataError(f'{context} is stated over POP_FRQ, which evidence_codes does not define')
    pop_frq = codes['POP_FRQ']
    outside = sorted(str(points) for points in admissible if not pop_frq.low <= points <= pop_frq.high)
    if outside:
        raise ReferenceDataError(
            f'{context}.admissible_pop_frq_points names {outside}, outside the POP_FRQ range '
            f'[{pop_frq.low}, {pop_frq.high}]'
        )
    return PopFrqPrecondition(conditioned_codes=conditioned, admissible_points=admissible)


def _build_critical_residue_max(payload: dict[str, object]) -> decimal.Decimal:
    """SM7's ceiling on the critical-amino-acid award.

    `standalone_code` is read too: the award is modelled as a modifier on a predictive code, bounded
    by that code's family concept cap, so a reference promoting it to a code of its own would give it
    a range this modelling never consults.
    """
    context = 'critical_amino_acids'
    section = _as_dict(_require(payload, context, 'reference'), context)
    if _require(section, 'standalone_code', context) is not False:
        raise ReferenceDataError(
            f'{context}.standalone_code is not false; the award is scored as a modifier on the predictive code'
        )
    maximum = _as_decimal(_require(section, 'max_points', context), f'{context}.max_points')
    if maximum <= 0:
        raise ReferenceDataError(f'{context}.max_points must be positive, got {maximum}')
    return maximum


def _build_oddspath(payload: dict[str, object]) -> tuple[OddsPathLevel, ...]:
    levels = []
    for raw_entry in _as_list(_require(payload, 'scale', 'odds_path_calibration'), 'odds_path_calibration.scale'):
        entry = _as_dict(raw_entry, 'odds_path_calibration.scale')
        strength = str(_require(entry, 'strength', 'odds_path_calibration.scale'))
        points = _as_decimal(_require(entry, 'points', 'odds_path_calibration.scale'), 'points')
        levels.append(
            OddsPathLevel(strength=strength, odds=_parse_odds(str(entry.get('odds_path', '-'))), points=points)
        )
    return tuple(levels)


# The two directions SM20 prints a grid for, declared rather than read off the file being validated:
# `functional.fxn_from_controls` selects on them, so a renamed one is a KeyError raised mid-score.
CONTROL_RANGES = ('pathogenic', 'benign')


def _build_control_counts(payload: dict[str, object]) -> dict[str, ControlCountGrid]:
    """Parse SM20 Tables 1-2 into grids keyed by the control range each serves.

    Both axes are control counts from 0 up, so the grid is square and its own row labels state the
    extent; a table whose rows and columns disagree would place a cell nothing can reach. Each table
    is also held to its own direction's sign — Table 1 awards pathogenicity and Table 2 benignity, and
    SM20's prose cites the two by the wrong numbers, so a transposed pair is the live error.
    """
    context = 'functional_assays.control_count_lookup'
    section = _as_dict(
        _require(
            _as_dict(_require(payload, 'functional_assays', 'reference'), 'functional_assays'),
            'control_count_lookup',
            'functional_assays',
        ),
        context,
    )
    grids: dict[str, ControlCountGrid] = {}
    for position, entry in enumerate(_as_list(_require(section, 'tables', context), f'{context}.tables'), start=1):
        grid = _build_control_count_grid(_as_dict(entry, f'{context}.tables'), position, context)
        if grid.direction in grids:
            raise ReferenceDataError(f'{context}.tables states two {grid.direction} tables')
        grids[grid.direction] = grid
    if tuple(sorted(grids)) != tuple(sorted(CONTROL_RANGES)):
        raise ReferenceDataError(
            f'{context}.tables states directions {sorted(grids)}, expected {sorted(CONTROL_RANGES)}'
        )
    return grids


def _build_control_count_grid(entry: dict[str, object], position: int, context: str) -> ControlCountGrid:
    where = f'{context}.tables[{position - 1}]'
    number = _require(entry, 'number', where)
    if number != position:
        raise ReferenceDataError(f'{where}: table numbered {number!r} sits at position {position}')
    direction = str(_require(entry, 'direction', where))
    if direction not in CONTROL_RANGES:
        raise ReferenceDataError(f'{where}.direction is {direction!r}, expected one of {list(CONTROL_RANGES)}')
    raw_cells = _as_dict(_require(entry, 'cells', where), f'{where}.cells')
    rows = list(raw_cells)
    if rows != [str(count) for count in range(len(rows))]:
        raise ReferenceDataError(f'{where}.cells: rows {rows} are not the control counts from 0 up')
    cells: dict[tuple[int, int], decimal.Decimal] = {}
    pathogenic_direction = direction == 'pathogenic'
    for benign, row in enumerate(rows):
        values = _as_list(raw_cells[row], f'{where}.cells[{row!r}]')
        if len(values) != len(rows):
            raise ReferenceDataError(
                f'{where}.cells[{row!r}] states {len(values)} columns for {len(rows)} rows; the axes are both '
                'control counts, so the grid is square'
            )
        for pathogenic, value in enumerate(values):
            points = _as_decimal(value, f'{where}.cells[{row!r}]')
            if (points < 0) if pathogenic_direction else (points > 0):
                raise ReferenceDataError(
                    f'{where}.cells[{row!r}] states {points} in the {direction} table, which awards evidence '
                    'in one direction only'
                )
            cells[(benign, pathogenic)] = points
    return ControlCountGrid(
        number=position,
        direction=direction,
        caption=str(_require(entry, 'caption', where)),
        applies_to=str(_require(entry, 'applies_to', where)),
        cells=cells,
    )


def _parse_odds(text: str) -> decimal.Decimal | None:
    """Parse an OddsPath ratio like "18.7:1" (pathogenic) or "1:2.08" (benign) to a scalar."""
    if ':' not in text:
        return None
    left, right = text.split(':', 1)
    return decimal.Decimal(left) / decimal.Decimal(right)


def _build_cap_table(hierarchy: dict[str, object], key: str) -> dict[str, CapRange]:
    table = _as_dict(_require(hierarchy, key, 'cap_hierarchy'), f'cap_hierarchy.{key}')
    result = {}
    for name, raw_pair in table.items():
        pair = _as_list(raw_pair, f'cap_hierarchy.{key}.{name}')
        if len(pair) != 2:
            raise ReferenceDataError(f'cap_hierarchy.{key}.{name} must be a [low, high] pair')
        low = _as_bound(pair[0], is_low=True, context=f'cap_hierarchy.{key}.{name}[0]')
        high = _as_bound(pair[1], is_low=False, context=f'cap_hierarchy.{key}.{name}[1]')
        if low > high:
            raise ReferenceDataError(f'cap_hierarchy.{key}.{name} has low {low} above high {high}')
        result[name] = CapRange(low=low, high=high)
    return result


def _build_concept_to_codes(hierarchy: dict[str, object], codes: dict[str, CodeSpec]) -> dict[str, tuple[str, ...]]:
    """Which codes each concept sums, every concept held to the family of the codes it groups.

    The grouping is the reference's own statement of which families a variant-type path sums, which
    `_independent_families` reads off it. A concept grouping a code of another family would move
    that family to the wrong side of the split, admitting a path's code where nothing bounds it.
    """
    mapping = _as_dict(_require(hierarchy, 'concept_to_codes', 'cap_hierarchy'), 'cap_hierarchy.concept_to_codes')
    result = {}
    for concept, raw_codes in mapping.items():
        names = tuple(str(c) for c in _as_list(raw_codes, f'cap_hierarchy.concept_to_codes.{concept}'))
        for name in names:
            if name not in codes:
                raise ReferenceDataError(f'cap_hierarchy.concept_to_codes.{concept} references unknown code {name!r}')
            if codes[name].family != concept:
                raise ReferenceDataError(
                    f'cap_hierarchy.concept_to_codes.{concept} groups {name!r}, whose family is {codes[name].family!r}'
                )
        result[concept] = names
    return result


def _independent_families(codes: dict[str, CodeSpec], concept_to_codes: dict[str, tuple[str, ...]]) -> frozenset[str]:
    """The code families no variant-type path sums, whose codes are therefore scored on their own.

    `cap_hierarchy.concept_to_codes` names the families a path sums under one parent code — the
    families the builders place on paths, where the concept and category caps bound them. A code of
    any other family (POP, CLN, LOC) reaches the tally on its own, under its per-code range alone,
    which is the distinction `classify` holds an independent code to.
    """
    return frozenset({spec.family for spec in codes.values()} - concept_to_codes.keys())


def _build_cap_hierarchy(
    payload: dict[str, object], codes: dict[str, CodeSpec]
) -> tuple[dict[str, CapRange], dict[str, CapRange], dict[str, tuple[str, ...]]]:
    hierarchy = _as_dict(_require(payload, 'cap_hierarchy', 'reference'), 'cap_hierarchy')
    concept_caps = _build_cap_table(hierarchy, 'concept_caps')
    category_caps = _build_cap_table(hierarchy, 'category_caps')
    concept_to_codes = _build_concept_to_codes(hierarchy, codes)
    return concept_caps, category_caps, concept_to_codes


def load_reference(path: pathlib.Path | None = None) -> Reference:
    """Load, parse, and validate the SVCv4 machine-readable reference.

    Args:
        path: Reference JSON to load; defaults to the packaged `data/svcv4_scoring_reference.json`.

    Returns:
        The validated `Reference`.

    Raises:
        ReferenceDataError: If the file is malformed or a required key is missing or renamed.
    """
    source = path or _DEFAULT_DATA
    try:
        with source.open() as f:
            loaded = json.load(f, parse_float=decimal.Decimal)
    except FileNotFoundError as e:
        raise ReferenceDataError(f'reference file not found: {source}') from e
    except json.JSONDecodeError as e:
        raise ReferenceDataError(f'invalid JSON in {source}') from e

    payload = _as_dict(loaded, 'reference')
    classification = _as_dict(_require(payload, 'classification', 'reference'), 'classification')
    ordered = _as_list(_require(classification, 'ordered_classes', 'classification'), 'classification.ordered_classes')
    class_order = tuple(
        str(_require(_as_dict(c, 'classification.ordered_classes'), 'code', 'classification.ordered_classes'))
        for c in ordered
    )
    matrix = _as_dict(_require(payload, 'mechanism_exon_matrix', 'reference'), 'mechanism_exon_matrix')

    bands = _build_bands(ordered, 'classification.ordered_classes', full_line=True)
    vus_subbands = _build_bands(
        _as_list(_require(classification, 'vus_subclasses', 'classification'), 'classification.vus_subclasses'),
        'classification.vus_subclasses',
        full_line=False,
    )
    _validate_subpartition(vus_subbands, _band(bands, 'VUS'), 'classification.vus_subclasses')

    codes = _build_codes(_as_dict(_require(payload, 'evidence_codes', 'reference'), 'evidence_codes'))
    concept_caps, category_caps, concept_to_codes = _build_cap_hierarchy(payload, codes)

    return Reference(
        cited_documents=_build_cited_documents(payload),
        provenance=_build_transcription_provenance(payload),
        class_order=class_order,
        bands=bands,
        vus_subbands=vus_subbands,
        gate=_build_gate(
            _as_dict(_require(payload, 'gene_disease_validity_gate', 'reference'), 'gene_disease_validity_gate'),
            class_order,
        ),
        mechanism_factors=_build_factors(matrix, 'molecular_mechanism', MECHANISM_LEVELS),
        exon_factors=_build_factors(matrix, 'exon_relevance', EXON_LEVELS),
        matrix_omitted_cell=_omitted_cell(matrix, MECHANISM_LEVELS, EXON_LEVELS),
        codes=codes,
        frequency_bins=_build_frequency_bins(
            _as_dict(_require(payload, 'population_frequency', 'reference'), 'population_frequency')
        ),
        binning_grids=_build_binning_grids(
            _as_dict(_require(payload, 'population_frequency', 'reference'), 'population_frequency')
        ),
        clinical_pop_frq_precondition=_build_pop_frq_precondition(payload, codes),
        critical_residue_max=_build_critical_residue_max(payload),
        oddspath=_build_oddspath(
            _as_dict(_require(payload, 'odds_path_calibration', 'reference'), 'odds_path_calibration')
        ),
        control_counts=_build_control_counts(payload),
        concept_caps=concept_caps,
        category_caps=category_caps,
        concept_to_codes=concept_to_codes,
        independent_families=_independent_families(codes, concept_to_codes),
        raw=payload,
    )
