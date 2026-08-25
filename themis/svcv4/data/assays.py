"""SM20's functional-assay evidence: the control-count grids, and the routes around them.

The small-experiment route is a lookup on how many controls of each kind the assay had, and SM20
prints the two grids for it as images only — one for a result in the pathogenic control range, one
for a result in the benign range. SM20 §22 cites them by the wrong numbers, so each grid states the
control range it serves rather than its number, and `functional.fxn_from_controls` selects on that.
"""

from __future__ import annotations

import dataclasses
import decimal

from themis.svcv4 import reference

METHOD = 'Brnich et al. OddsPath/likelihood-ratio (PMID 31892348; Tavtigian PMID 29300386)'
REQUIRES = 'BOTH pathogenic AND benign controls (single WT reference insufficient)'
STRENGTH_DRIVER = (
    '# benign controls drives pathogenic strength (SM20 Table 1); # pathogenic controls drives benign strength '
    '(SM20 Table 2)'
)
MECHANISM_MATCH_REQUIRED = True
NO_DATA = 'FXN_ND'
PATIENT_DERIVED_SAMPLES = 'usually routed to LOC_PHE, not FXN (narrow exceptions)'


def _row(printed: str) -> tuple[decimal.Decimal, ...]:
    """One row of a control-count grid, in the columns the image prints it across."""
    return reference.printed_decimals(*printed.split())


# Both axes run 0 to 10 controls, rows benign and columns pathogenic. Each row is laid out in the
# image's own column order, so a re-read of the image can be checked against it a column at a time.
_PATHOGENIC_CELLS = (
    _row(' 0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0'),  # 0 benign controls
    _row(' 0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0'),  # 1 benign controls
    _row(' 0.0  0.0  0.0  1.0  1.0  1.0  1.0  1.0  1.0  1.0  1.0'),  # 2 benign controls
    _row(' 0.0  0.0  1.0  1.0  1.0  1.0  1.0  1.0  1.0  1.0  1.0'),  # 3 benign controls
    _row(' 0.0  1.0  1.0  1.0  1.0  1.0  1.0  2.0  2.0  2.0  2.0'),  # 4 benign controls
    _row(' 0.0  1.0  1.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0'),  # 5 benign controls
    _row(' 0.0  1.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0'),  # 6 benign controls
    _row(' 0.0  1.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0'),  # 7 benign controls
    _row(' 0.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0'),  # 8 benign controls
    _row(' 0.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0  2.0  3.0  3.0'),  # 9 benign controls
    _row(' 0.0  2.0  2.0  2.0  2.0  3.0  3.0  3.0  3.0  3.0  3.0'),  # 10 benign controls
)

_BENIGN_CELLS = (
    _row(' 0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0  0.0'),  # 0 benign controls
    _row(' 0.0  0.0  0.0  0.0 -1.0 -1.0 -1.0 -1.0 -2.0 -2.0 -2.0'),  # 1 benign controls
    _row(' 0.0  0.0  0.0 -1.0 -1.0 -1.0 -2.0 -2.0 -2.0 -2.0 -2.0'),  # 2 benign controls
    _row(' 0.0  0.0 -1.0 -1.0 -1.0 -2.0 -2.0 -2.0 -2.0 -2.0 -2.0'),  # 3 benign controls
    _row(' 0.0  0.0 -1.0 -1.0 -1.0 -2.0 -2.0 -2.0 -2.0 -2.0 -2.0'),  # 4 benign controls
    _row(' 0.0  0.0 -1.0 -1.0 -1.0 -2.0 -2.0 -2.0 -2.0 -2.0 -3.0'),  # 5 benign controls
    _row(' 0.0  0.0 -1.0 -1.0 -1.0 -2.0 -2.0 -2.0 -2.0 -2.0 -3.0'),  # 6 benign controls
    _row(' 0.0  0.0 -1.0 -1.0 -2.0 -2.0 -2.0 -2.0 -2.0 -2.0 -3.0'),  # 7 benign controls
    _row(' 0.0  0.0 -1.0 -1.0 -2.0 -2.0 -2.0 -2.0 -2.0 -2.0 -3.0'),  # 8 benign controls
    _row(' 0.0  0.0 -1.0 -1.0 -2.0 -2.0 -2.0 -2.0 -2.0 -3.0 -3.0'),  # 9 benign controls
    _row(' 0.0  0.0 -1.0 -1.0 -2.0 -2.0 -2.0 -2.0 -2.0 -3.0 -3.0'),  # 10 benign controls
)


def _cells(rows: tuple[tuple[decimal.Decimal, ...], ...]) -> dict[tuple[int, int], decimal.Decimal]:
    """One grid's points, addressed by (benign controls, pathogenic controls)."""
    return {(benign, pathogenic): points for benign, row in enumerate(rows) for pathogenic, points in enumerate(row)}


GRIDS = reference.assemble_control_counts(
    (
        reference.ControlCountGrid(
            number=1,
            direction='pathogenic',
            caption=(
                'Lookup table for pathogenicity evidence strength for functional assays for small experiments with '
                'no false positives or false negatives'
            ),
            applies_to='a test variant whose result falls in the pathogenic control range',
            cells=_cells(_PATHOGENIC_CELLS),
            media_file='image3.png',
            media_pixels='1634x800',
            legibility='clear',
        ),
        reference.ControlCountGrid(
            number=2,
            direction='benign',
            caption=(
                'Lookup table for benignity evidence strength for functional assays for small experiments with no '
                'false positives or false negatives'
            ),
            applies_to='a test variant whose result falls in the benign control range',
            cells=_cells(_BENIGN_CELLS),
            media_file='image2.png',
            media_pixels='1634x804',
            legibility='clear',
        ),
    )
)


@dataclasses.dataclass(frozen=True)
class GridProvenance:
    """How the two control-count grids were read out of SM20's images.

    Attributes:
        read_from: Where the images live inside the supplement.
        table_to_image: How each caption was matched to its image.
        read_on: When they were read.
        completeness: Whether every cell was in frame and legible.
        cross_check: The relation between the two grids that a separate read of each confirmed.
    """

    read_from: str
    table_to_image: str
    read_on: str
    completeness: str
    cross_check: str


@dataclasses.dataclass(frozen=True)
class ControlCountLookup:
    """SM20's small-experiment route: the two grids, and what they may be entered for.

    Attributes:
        lookup: Which tables the route reads.
        scope: The experiments the route covers, and what the others take instead.
        rows: The row axis.
        columns: The column axis.
        reading: Which grid serves which result, and the citation error to avoid following.
        maximum: The strength the route tops out at.
        single_control_note: Why one control of a kind is worth nothing.
        provenance: How the grids were read.
        grids: The grids, keyed by the control range each serves.
    """

    lookup: str
    scope: str
    rows: str
    columns: str
    reading: str
    maximum: str
    single_control_note: str
    provenance: GridProvenance
    grids: dict[str, reference.ControlCountGrid]


CONTROL_COUNT_LOOKUP = ControlCountLookup(
    lookup='SM20 Tables 1-2',
    scope=(
        "small experiments with NO false positives and NO false negatives, after SM20's two prior gates "
        "(functional data exist, and the assay is faithful to the MDE's mechanism). Larger experiments, "
        'trichotomised readouts and MAVEs are calibrated mathematically instead (SM20 §22)'
    ),
    rows='# of benign control variants, 0-10',
    columns='# of pathogenic control variants, 0-10',
    reading=(
        'Table 1 scores a test variant whose result falls in the PATHOGENIC control range, Table 2 one in the '
        'BENIGN control range. SM20 §22 cites them by the wrong numbers (its "Table 2" and "Table 3" are these '
        'Tables 1 and 2), and following the citation as written selects the wrong sign of evidence'
    ),
    maximum=(
        'neither grid reaches ±4.0: the small-experiment regime tops out at ±3.0 with ten controls of both kinds, '
        'and an assay needing Strong-level weight takes the mathematical route instead'
    ),
    single_control_note=(
        'one benign control is worth nothing for pathogenicity and one pathogenic control nothing for benignity: '
        'SM20 §22 holds that a single reference version of the protein does not meet the control standard, and the '
        'grids state it only through those all-zero lines'
    ),
    provenance=GridProvenance(
        read_from=(
            'the PNGs embedded in "Supplementary Material 20. Functional Assay Evidence.docx" (word/media). Both '
            'tables exist in SM20 only as images and appear in no text extraction of it.'
        ),
        table_to_image=(
            'taken from the r:embed order in word/document.xml; the media filenames do not follow caption order'
        ),
        read_on='2026-08-05',
        completeness=(
            'all 242 cells in frame and legible; no cell empty, none unreadable; transcribed from the images '
            'alone, with no cell computed from the other table'
        ),
        cross_check=(
            'Table 2 is the exact sign-flipped transpose of Table 1 at all 121 positions, and the two were '
            'transcribed separately from separate images'
        ),
    ),
    grids=GRIDS,
)


@dataclasses.dataclass(frozen=True)
class MultipleAssays:
    """How several assays on one variant combine.

    Attributes:
        same_readout_same_direction: Two assays of one readout agreeing.
        same_readout_opposite: Two assays of one readout disagreeing.
        distinct_functions: Assays of different functions.
    """

    same_readout_same_direction: str
    same_readout_opposite: str
    distinct_functions: str


MULTIPLE_ASSAYS = MultipleAssays(
    same_readout_same_direction='count strongest only',
    same_readout_opposite='sum',
    distinct_functions='score most disease-relevant',
)


@dataclasses.dataclass(frozen=True)
class AnimalModels:
    """SM20's animal-model route.

    Attributes:
        code_range: The award's range.
        knock_in_table: The tiers SM20 states, by phenotype replication, inheritance and similarity.
    """

    code_range: reference.CapRange
    knock_in_table: str


ANIMAL_MODELS = AnimalModels(
    code_range=reference.CapRange(low=decimal.Decimal('0.0'), high=decimal.Decimal('4.0')),
    knock_in_table=(
        'high phenotype replication + same inheritance + high protein similarity = +4.0; different inheritance = '
        '+3.0; key features same/different = +2.0/+1.0; similar phenotype = +1.0; low consistency/similarity = 0.0'
    ),
)
