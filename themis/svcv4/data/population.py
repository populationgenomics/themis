"""SM3's population-frequency evidence: the POP_FRQ bins, the DAFT routes, and POP_HMZ.

POP_FRQ scores a filtering allele frequency against a disease-specific threshold (the DAFT), so the
bins here are ratios of the two, not frequencies. SM3 offers three routes to a DAFT and states an
order over them; the binned route's six lookup tables exist in SM3 as images only, which is why each
table records the image it was read from and at what resolution.
"""

from __future__ import annotations

import dataclasses
import decimal

from themis.svcv4 import reference


@dataclasses.dataclass(frozen=True)
class PopulationFrequency:
    """What POP_FRQ is scored on.

    Attributes:
        input_frequency: The frequency the code is read from, and how it is bounded.
        caveats: Where that frequency cannot be taken at face value.
    """

    input_frequency: str
    caveats: tuple[str, ...]


POP_FRQ = PopulationFrequency(
    input_frequency='FAF (grpmax filtering allele frequency, lower bound 95% CI)',
    caveats=(
        'FAF not computed when AC=1',
        'beware low allele-number distortions (e.g. lcr flag)',
    ),
)

FREQUENCY_BINS = (
    reference.FrequencyBin(
        cell='lt_1_5x',
        ratio='< 1.5x DAFT',
        min_multiple=decimal.Decimal('0'),
        points=decimal.Decimal('0.0'),
    ),
    reference.FrequencyBin(
        cell='1_5x_to_5x',
        ratio='> 1.5x to < 5x DAFT',
        min_multiple=decimal.Decimal('1.5'),
        points=decimal.Decimal('-1.0'),
    ),
    reference.FrequencyBin(
        cell='5x_to_15x',
        ratio='> 5x to < 15x DAFT',
        min_multiple=decimal.Decimal('5'),
        points=decimal.Decimal('-3.0'),
    ),
    reference.FrequencyBin(
        cell='ge_15x',
        ratio='> 15x DAFT',
        min_multiple=decimal.Decimal('15'),
        points=decimal.Decimal('-6.0'),
    ),
)

reference.validate_frequency_bins(FREQUENCY_BINS)


@dataclasses.dataclass(frozen=True)
class DaftCalculator:
    """SM3's first DAFT route: the Whiffin/Ware maximum-credible-AF calculator.

    Attributes:
        preferred_when: The conditions that make this the route to take.
        tool: Where the calculator runs.
        inputs: What it is entered with.
        penetrance_defaults: The penetrances SM3 offers where no estimate exists.
        guidance: Which way to err where the inputs are uncertain.
    """

    preferred_when: str
    tool: str
    inputs: tuple[str, ...]
    penetrance_defaults: tuple[decimal.Decimal, ...]
    guidance: str


CALCULATOR = DaftCalculator(
    preferred_when=(
        'no curated VCEP or community threshold exists for the MDE, and the analyst can estimate prevalence, '
        'penetrance, and locus and allelic heterogeneity'
    ),
    tool='cardiodb.org/allelefrequencyapp',
    inputs=('inheritance', 'prevalence (1/X)', 'genetic_heterogeneity', 'allelic_heterogeneity', 'penetrance'),
    penetrance_defaults=reference.printed_decimals('0.2', '0.5', '0.8'),
    guidance='err toward high DAFT (conservative for benign calls)',
)


@dataclasses.dataclass(frozen=True)
class PrevalenceBin:
    """One row of the binned route's tables: the prevalence as the image prints it.

    Attributes:
        label: The row heading, `1/10,000` as the image prints it, kept so a re-read of the image has
            something to check the denominator against.
        denominator: The 10,000 in that heading, so a larger denominator is a rarer disease.
    """

    label: str
    denominator: int

    def __post_init__(self) -> None:
        printed = f'1/{self.denominator:,}'
        if printed != self.label:
            raise reference.ReferenceDataError(
                f'a prevalence of 1 in {self.denominator} prints as {printed!r}, and the image heads the row '
                f'{self.label!r}'
            )


@dataclasses.dataclass(frozen=True)
class PenetranceColumn:
    """One column of the binned route's tables.

    Attributes:
        label: The column heading as the image prints it, kept for the same reason as the row's.
        penetrance: The fraction that heading stands for.
    """

    label: str
    penetrance: decimal.Decimal

    def __post_init__(self) -> None:
        printed = f'{self.penetrance:.0%}'
        if printed != self.label:
            raise reference.ReferenceDataError(
                f'a penetrance of {self.penetrance} prints as {printed!r}, and the image heads the column '
                f'{self.label!r}'
            )


PREVALENCE_BINS = (
    PrevalenceBin(label='1/500', denominator=500),
    PrevalenceBin(label='1/1,000', denominator=1000),
    PrevalenceBin(label='1/5,000', denominator=5000),
    PrevalenceBin(label='1/10,000', denominator=10000),
    PrevalenceBin(label='1/50,000', denominator=50000),
    PrevalenceBin(label='1/100,000', denominator=100000),
    PrevalenceBin(label='1/500,000', denominator=500000),
    PrevalenceBin(label='1/1,000,000', denominator=1000000),
)

PENETRANCE_COLUMNS = (
    PenetranceColumn(label='80%', penetrance=decimal.Decimal('0.8')),
    PenetranceColumn(label='50%', penetrance=decimal.Decimal('0.5')),
    PenetranceColumn(label='20%', penetrance=decimal.Decimal('0.2')),
)

_DENOMINATORS = tuple(row.denominator for row in PREVALENCE_BINS)
_PENETRANCES = tuple(column.penetrance for column in PENETRANCE_COLUMNS)


def _cells(rows: tuple[tuple[decimal.Decimal, ...], ...]) -> dict[tuple[int, decimal.Decimal], decimal.Decimal]:
    """One table's thresholds, addressed by the axes every table in the set shares."""
    return {
        (denominator, penetrance): threshold
        for denominator, row in zip(_DENOMINATORS, rows, strict=True)
        for penetrance, threshold in zip(_PENETRANCES, row, strict=True)
    }


_TABLE_1_CELLS = (
    reference.printed_decimals('0.001250000', '0.002000000', '0.005000000'),  # 1/500
    reference.printed_decimals('0.000625000', '0.001000000', '0.002500000'),  # 1/1,000
    reference.printed_decimals('0.000125000', '0.000200000', '0.000500000'),  # 1/5,000
    reference.printed_decimals('0.000062500', '0.000100000', '0.000250000'),  # 1/10,000
    reference.printed_decimals('0.000012500', '0.000020000', '0.000050000'),  # 1/50,000
    reference.printed_decimals('0.000006250', '0.000010000', '0.000025000'),  # 1/100,000
    reference.printed_decimals('0.000001250', '0.000002000', '0.000005000'),  # 1/500,000
    reference.printed_decimals('0.000000625', '0.000001000', '0.000002500'),  # 1/1,000,000
)

_TABLE_2_CELLS = (
    reference.printed_decimals('0.05000', '0.05000', '0.05000'),  # 1/500
    reference.printed_decimals('0.03540', '0.04470', '0.05000'),  # 1/1,000
    reference.printed_decimals('0.01580', '0.02000', '0.03160'),  # 1/5,000
    reference.printed_decimals('0.01120', '0.01410', '0.02240'),  # 1/10,000
    reference.printed_decimals('0.00500', '0.00632', '0.01000'),  # 1/50,000
    reference.printed_decimals('0.00354', '0.00447', '0.00707'),  # 1/100,000
    reference.printed_decimals('0.00158', '0.00200', '0.00316'),  # 1/500,000
    reference.printed_decimals('0.00112', '0.00141', '0.00224'),  # 1/1,000,000
)

_TABLE_3_CELLS = (
    reference.printed_decimals('0.00250000', '0.00400000', '0.01000000'),  # 1/500
    reference.printed_decimals('0.00125000', '0.00200000', '0.00500000'),  # 1/1,000
    reference.printed_decimals('0.00025000', '0.00040000', '0.00100000'),  # 1/5,000
    reference.printed_decimals('0.00012500', '0.00020000', '0.00050000'),  # 1/10,000
    reference.printed_decimals('0.00002500', '0.00004000', '0.00010000'),  # 1/50,000
    reference.printed_decimals('0.00001250', '0.00002000', '0.00005000'),  # 1/100,000
    reference.printed_decimals('0.00000250', '0.00000400', '0.00001000'),  # 1/500,000
    reference.printed_decimals('0.00000125', '0.00000200', '0.00000500'),  # 1/1,000,000
)

_TABLE_4_CELLS = (
    reference.printed_decimals('0.001250000', '0.002000', '0.0050000'),  # 1/500
    reference.printed_decimals('0.000625000', '0.001000', '0.0025000'),  # 1/1,000
    reference.printed_decimals('0.000125000', '0.000200', '0.0005000'),  # 1/5,000
    reference.printed_decimals('0.000062500', '0.000100', '0.0002500'),  # 1/10,000
    reference.printed_decimals('0.000012500', '0.000020', '0.0000500'),  # 1/50,000
    reference.printed_decimals('0.000006250', '0.000010', '0.0000250'),  # 1/100,000
    reference.printed_decimals('0.000001250', '0.000002', '0.0000050'),  # 1/500,000
    reference.printed_decimals('0.000000625', '0.000001', '0.0000025'),  # 1/1,000,000
)

_TABLE_5_CELLS = (
    reference.printed_decimals('0.05000', '0.05000', '0.05000'),  # 1/500
    reference.printed_decimals('0.03540', '0.04470', '0.05000'),  # 1/1,000
    reference.printed_decimals('0.01580', '0.02000', '0.03160'),  # 1/5,000
    reference.printed_decimals('0.01120', '0.01410', '0.02240'),  # 1/10,000
    reference.printed_decimals('0.00500', '0.00632', '0.01000'),  # 1/50,000
    reference.printed_decimals('0.00354', '0.00447', '0.00707'),  # 1/100,000
    reference.printed_decimals('0.00158', '0.00200', '0.00316'),  # 1/500,000
    reference.printed_decimals('0.00112', '0.00141', '0.00224'),  # 1/1,000,000
)

_TABLE_6_CELLS = (
    reference.printed_decimals('0.001670000', '0.002670000', '0.006670000'),  # 1/500
    reference.printed_decimals('0.000833000', '0.001330000', '0.003330000'),  # 1/1,000
    reference.printed_decimals('0.000167000', '0.000267000', '0.000667000'),  # 1/5,000
    reference.printed_decimals('0.000083300', '0.000133000', '0.000333000'),  # 1/10,000
    reference.printed_decimals('0.000016700', '0.000026700', '0.000066700'),  # 1/50,000
    reference.printed_decimals('0.000008330', '0.000013300', '0.000033300'),  # 1/100,000
    reference.printed_decimals('0.000001670', '0.000002670', '0.000006670'),  # 1/500,000
    reference.printed_decimals('0.000000833', '0.000001330', '0.000003330'),  # 1/1,000,000
)

# The three cells SM3 prints a '*' on, in Tables 2 and 5: 1/500 x 50%, 1/500 x 20%, 1/1,000 x 20%.
_MARKED_RECESSIVE = frozenset(
    {
        (500, decimal.Decimal('0.5')),
        (500, decimal.Decimal('0.2')),
        (1000, decimal.Decimal('0.2')),
    }
)

GRIDS = reference.assemble_binning_grids(
    (
        reference.BinningGrid(
            number=1,
            caption=('Table 1: DAFT Lookup Table for Monogenic Disease Entities with Autosomal Dominant Inheritance'),
            title='AUTOSOMAL DOMINANT',
            applies_to='an autosomal-dominant MDE, on the overall prevalence',
            prevalence_denominators=_DENOMINATORS,
            penetrances=_PENETRANCES,
            cells=_cells(_TABLE_1_CELLS),
            marked=frozenset(),
            media_file='image4.png',
            media_pixels='1136x1038',
            legibility='clear',
        ),
        reference.BinningGrid(
            number=2,
            caption=('Table 2: DAFT Lookup Table for Monogenic Disease Entities with Autosomal Recessive Inheritance'),
            title='AUTOSOMAL RECESSIVE',
            applies_to='an autosomal-recessive MDE, on the overall prevalence',
            prevalence_denominators=_DENOMINATORS,
            penetrances=_PENETRANCES,
            cells=_cells(_TABLE_2_CELLS),
            marked=_MARKED_RECESSIVE,
            media_file='image3.png',
            media_pixels='1140x988',
            legibility='clear',
        ),
        reference.BinningGrid(
            number=3,
            caption=(
                'Table 3: DAFT Lookup Table for Monogenic Disease Entities with X-Linked Inheritance - Male Frequencies'
            ),
            title='X-LINKED DOMINANT OR RECESSIVE - MALE (sex-specific prevalence)',
            applies_to=(
                'an X-linked MDE, dominant or recessive, scoring a male frequency, on the male-specific prevalence'
            ),
            prevalence_denominators=_DENOMINATORS,
            penetrances=_PENETRANCES,
            cells=_cells(_TABLE_3_CELLS),
            marked=frozenset(),
            media_file='image8.png',
            media_pixels='1140x1034',
            legibility='clear',
        ),
        reference.BinningGrid(
            number=4,
            caption=(
                'Table 4: DAFT Lookup Table for Monogenic Disease Entities with X-Linked Dominant Inheritance - '
                'Female Frequencies'
            ),
            title='X-LINKED DOMINANT - FEMALE (sex-specific prevalence)',
            applies_to='an X-linked-dominant MDE scoring a female frequency, on the female-specific prevalence',
            prevalence_denominators=_DENOMINATORS,
            penetrances=_PENETRANCES,
            cells=_cells(_TABLE_4_CELLS),
            marked=frozenset(),
            media_file='image5.png',
            media_pixels='1140x992',
            legibility='clear',
        ),
        reference.BinningGrid(
            number=5,
            caption=(
                'Table 5: DAFT Lookup Table for Monogenic Disease Entities with X-Linked Recessive Inheritance - '
                'Female Frequencies'
            ),
            title='X-LINKED RECESSIVE - FEMALE (sex-specific prevalence)',
            applies_to='an X-linked-recessive MDE scoring a female frequency, on the female-specific prevalence',
            prevalence_denominators=_DENOMINATORS,
            penetrances=_PENETRANCES,
            cells=_cells(_TABLE_5_CELLS),
            marked=_MARKED_RECESSIVE,
            media_file='image7.png',
            media_pixels='1140x992',
            legibility='clear',
        ),
        reference.BinningGrid(
            number=6,
            caption=(
                'Table 6: DAFT Lookup Table for Monogenic Disease Entities with X-Linked Dominant Inheritance - '
                'Combined Male and Female Frequencies'
            ),
            title='X-LINKED DOMINANT - COMBINED (combined male and female prevalence)',
            applies_to='an X-linked-dominant MDE scoring a combined-sex frequency, on the combined prevalence',
            prevalence_denominators=_DENOMINATORS,
            penetrances=_PENETRANCES,
            cells=_cells(_TABLE_6_CELLS),
            marked=frozenset(),
            media_file='image6.png',
            media_pixels='1140x1032',
            legibility='clear',
        ),
    )
)


@dataclasses.dataclass(frozen=True)
class GridProvenance:
    """How the six binning tables were read out of SM3's images.

    Attributes:
        read_from: Where the images live inside the supplement.
        table_to_image: How each caption was matched to its image.
        read_at: The resolution each table was read at, and which were re-read.
        read_on: When they were read.
        completeness: Whether every cell was in frame and legible.
    """

    read_from: str
    table_to_image: str
    read_at: str
    read_on: str
    completeness: str


@dataclasses.dataclass(frozen=True)
class BinningMethod:
    """SM3's binned DAFT route: the six lookup tables, and the rules for entering them.

    Attributes:
        use_when: When the binned route is taken rather than the calculator.
        locus_and_allelic_heterogeneity: The heterogeneity the tables are computed under.
        lookup: Which tables the route reads.
        prevalence_bins: The row axis.
        penetrance_columns: The column axis.
        penetrance_bins: SM3's prose bins for penetrance, which the column headings override.
        prevalence_rule: Which row an estimate between two rows takes.
        penetrance_rule: Which column an estimate between two columns takes.
        penetrance_column_note: Why the column headings govern where the prose bins disagree.
        marker_note: What is known about the '*' SM3 prints on three cells.
        no_table_for: The observation SM3 prints no table for.
        provenance: How the tables were read.
        grids: The tables, keyed by the title printed inside each image.
    """

    use_when: str
    locus_and_allelic_heterogeneity: int
    lookup: str
    prevalence_bins: tuple[PrevalenceBin, ...]
    penetrance_columns: tuple[PenetranceColumn, ...]
    penetrance_bins: tuple[str, ...]
    prevalence_rule: str
    penetrance_rule: str
    penetrance_column_note: str
    marker_note: str
    no_table_for: str
    provenance: GridProvenance
    grids: dict[str, reference.BinningGrid]


BINNING = BinningMethod(
    use_when='X-linked or sparse data',
    locus_and_allelic_heterogeneity=1,
    lookup='SM3 Tables 1-6',
    prevalence_bins=PREVALENCE_BINS,
    penetrance_columns=PENETRANCE_COLUMNS,
    penetrance_bins=('20-<50%', '50-<80%', '>80%'),
    prevalence_rule=(
        'select the more common bin where the estimate falls between two, i.e. round the prevalence up: an '
        'estimated 1/2,000 takes the 1/1,000 bin. Estimate the phenotype prevalence lumping together every gene '
        'associated with the MDE (locus homogeneity assumed), and only over forms sharing its inheritance pattern.'
    ),
    penetrance_rule='use the lower bin near a boundary, i.e. round the penetrance estimate down to a column',
    penetrance_column_note=(
        "the images head their columns 80%, 50%, 20% over the instruction 'round down - i.e., disease is less "
        "penetrant', so a column is 'penetrance at least this value'. SM3's prose bins ('20% to <50%, 50% to <80% "
        "and >80%') leave 80% itself in no bin; the column labels govern."
    ),
    marker_note=(
        "the '*' printed on three cells of Tables 2 and 5 is defined nowhere in SM3: no footnote or endnote "
        'stream, no legend inside either image, no mention in the text. Every marked cell holds 0.05000, the '
        'largest value in either table.'
    ),
    no_table_for=(
        'a combined-sex frequency in an X-linked-recessive MDE: SM3 prints no such table, so that observation has '
        'no binned route'
    ),
    provenance=GridProvenance(
        read_from=(
            'the PNGs embedded in "Supplementary Material 3. Population Database Frequency.docx" (word/media). The '
            'six tables exist in SM3 only as images and appear in no text extraction of it.'
        ),
        table_to_image=(
            'taken from the r:embed order in word/document.xml, where each caption precedes its image; the media '
            'filenames do not follow caption order'
        ),
        read_at=(
            '1500 px wide renders (1.3x native); Tables 2 and 6, whose digits follow no arithmetic pattern that '
            'would catch a misread, re-read at 2x on cropped row bands'
        ),
        read_on='2026-08-07',
        completeness='all 144 cells in frame and legible; no cell empty, none unreadable',
    ),
    grids=GRIDS,
)


@dataclasses.dataclass(frozen=True)
class PathogenicVariantsMethod:
    """SM3's empirical DAFT route: the frequency of the gene's known pathogenic variants.

    Attributes:
        use_when: The size of pool the route needs.
        rule: Which frequency of that pool becomes the DAFT.
    """

    use_when: str
    rule: str


PATHOGENIC_VARIANTS = PathogenicVariantsMethod(
    use_when='>= 10 known P/LP variants',
    rule='highest continental FAF excluding founders (> 5x)',
)


@dataclasses.dataclass(frozen=True)
class HomozygousObservations:
    """POP_HMZ: what a homozygous or hemizygous population observation is worth.

    Attributes:
        source: The passages and table the code is read from.
        requires: The preconditions the code is scored under.
        excludes: The observations that belong to another code instead.
        weights: What one qualifying observation scores, by inheritance.
    """

    source: str
    requires: tuple[str, ...]
    excludes: str
    weights: reference.HomozygousWeights


POP_HMZ = HomozygousObservations(
    source='SM3 §§70-72 and Table 7',
    requires=(
        '>= 2 eligible gnomAD observations',
        'age-matched penetrance of the MDE near 100%',
        ('affected individuals not expected to be present in population databases (a mild phenotype does not qualify)'),
    ),
    excludes='unaffected individuals whose clinical details are given: those are CLN_UAF, not POP_HMZ',
    weights=reference.HomozygousWeights(
        dominant=reference.ObservationRow(
            cell='ad.homozygous',
            description='AD_homozygous',
            points=decimal.Decimal('-1.0'),
        ),
        other=reference.ObservationRow(
            cell='arxl.homozygous_or_hemizygous',
            description='semidominant_or_AR_or_Xlinked_homo_hemizygous',
            points=decimal.Decimal('-0.5'),
        ),
    ),
)
