"""Address and price the framework's per-observation rows.

`scoring` computes a decision-tree path from tiers. The clinical and locus codes are not that shape:
SM3 Table 7, SM4 Tables 1-3, SM5's yield bins and segregation tables state a value *per observed
individual*, and a code's contribution is that value times how many individuals fall in the row. The
library has never modelled those rows, so everything that needs them re-derives them -- the agent by
hand in prose, and any projection of a curator's worksheet separately.

This module gives each row a stable **cell id** and reads its value out of the same reference
`scoring` reads, so one revision of the framework data moves every consumer at once. The
ids are the vocabulary a curation worksheet stores and a run states, which is what lets the two be
compared row by row rather than only as totals.

Only the independent codes are here. The variant-type path codes (`MIS_`, `NUL_`, `CDS_`, `SPL_`)
are priced by the path a `builders` call selects, from a tier plus the mechanism and exon axes; a
cell id is the wrong key for them and `builders` is where they belong.
"""

from __future__ import annotations

import decimal
from collections.abc import Mapping

from themis.svcv4 import reference


class UnknownCellError(Exception):
    """A cell id no per-observation table defines."""


# The zygosity/phase columns of SM4 Table 2, in the order the reference's own `columns` list names.
_BIALLELIC_COLUMNS = (
    'trans_plp_confirmed',
    'trans_plp_assumed',
    'trans_vus_confirmed',
    'homozygous',
    'no_second_variant',
)
# SM4 Table 2's rows, as the reference keys them.
_BIALLELIC_ROWS = (
    ('consistent_full_lt_0_0001', 'consistent_all_tested_cooccurrence_lt_0.0001'),
    ('consistent_full_0_0001_0_01', 'consistent_all_tested_cooccurrence_0.0001_to_0.01'),
    ('consistent_partial', 'with_caveats'),
    ('consistent_other_variant', 'explanatory_PLP_other_gene'),
    ('not_consistent', 'NOT_CONSISTENT'),
)
# CLN_UAF's penetrance bands, and the two columns the reference collapses the zygosities into.
_PENETRANCE_BANDS = (
    ('near_100', 'near_100pct_penetrance'),
    ('80_100', 'penetrance_80_100pct'),
)
_UAF_AS_P = 'dominant_or_semidominant_or_homo_hemizygous_or_in_trans_P'
# LOC_PHE's yield bins are listed benign-to-pathogenic in the reference.
_YIELD_CELLS = ('0_33', '33_51', '51_68', '68_82', 'ge_82')
# POP_FRQ's ratio bins, likewise.
_FREQUENCY_CELLS = ('lt_1_5x', '1_5x_to_5x', '5x_to_15x', 'ge_15x')


def _decimal(value: object, context: str) -> decimal.Decimal:
    if isinstance(value, decimal.Decimal):
        return value
    if isinstance(value, int):
        return decimal.Decimal(value)
    raise reference.ReferenceDataError(f'{context}: expected a number, got {value!r}')


def _table(raw: Mapping[str, object], *path: str) -> Mapping[str, object]:
    node: object = raw
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            raise reference.ReferenceDataError(f'the reference has no {".".join(path)}')
        node = node[key]
    if not isinstance(node, Mapping):
        raise reference.ReferenceDataError(f'{".".join(path)} is not a table')
    return node


def _sequence(raw: Mapping[str, object], *path: str) -> list[object]:
    node: object = raw
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            raise reference.ReferenceDataError(f'the reference has no {".".join(path)}')
        node = node[key]
    if not isinstance(node, list):
        raise reference.ReferenceDataError(f'{".".join(path)} is not a list')
    return node


def cell_points(ref: reference.Reference) -> dict[str, decimal.Decimal]:
    """Every per-observation cell the framework prices, by id.

    Raises:
        ReferenceDataError: If a table the framework states is absent or malformed. A missing table
            is a gap in the reference data, never a reason to price its rows at zero.
    """
    raw = ref.raw
    cells: dict[str, decimal.Decimal] = {}

    frequency = _sequence(raw, 'population_frequency', 'POP_FRQ', 'bins')
    for name, bin_ in zip(_FREQUENCY_CELLS, frequency, strict=True):
        if not isinstance(bin_, Mapping):
            raise reference.ReferenceDataError(f'POP_FRQ bin {name} is not a table')
        cells[f'POP_FRQ.bin.{name}'] = _decimal(bin_['points'], f'POP_FRQ.bin.{name}')

    homozygous = _table(raw, 'population_frequency', 'POP_HMZ', 'per_observation_points')
    cells['POP_HMZ.ad.homozygous'] = _decimal(homozygous['AD_homozygous'], 'POP_HMZ.ad')
    cells['POP_HMZ.arxl.homozygous_or_hemizygous'] = _decimal(
        homozygous['semidominant_or_AR_or_Xlinked_homo_hemizygous'], 'POP_HMZ.arxl'
    )

    unaffected = _table(raw, 'clinical_observations', 'CLN_UAF')
    for band, key in _PENETRANCE_BANDS:
        table = _table(unaffected, key)
        as_p = _decimal(table[_UAF_AS_P], f'CLN_UAF.{band}')
        as_lp = _decimal(table['in_trans_LP'], f'CLN_UAF.{band}.in_trans_LP')
        cells[f'CLN_UAF.ad.{band}'] = as_p
        cells[f'CLN_UAF.arxl.{band}.hom_hemi'] = as_p
        cells[f'CLN_UAF.arxl.{band}.trans_p'] = as_p
        cells[f'CLN_UAF.arxl.{band}.trans_lp'] = as_lp
    low = _decimal(unaffected['penetrance_lt_80pct'], 'CLN_UAF.lt_80')
    cells['CLN_UAF.ad.lt_80'] = low
    for zygosity in ('hom_hemi', 'trans_p', 'trans_lp'):
        cells[f'CLN_UAF.arxl.lt_80.{zygosity}'] = low

    # CLN_ALT states an ordered value list rather than named rows, so its cells map positionally.
    alternate = _sequence(raw, 'clinical_observations', 'CLN_ALT', 'values')
    severity = [_decimal(v, 'CLN_ALT') for v in alternate]
    cells['CLN_ALT.variant.more_severe'] = severity[0]
    cells['CLN_ALT.variant.not_more_severe'] = severity[1]
    cells['CLN_ALT.variant.not_consistent_recessive'] = severity[2]
    cells['CLN_ALT.gene.more_severe'] = severity[0]
    cells['CLN_ALT.gene.not_more_severe'] = severity[1]

    monoallelic = _table(raw, 'clinical_observations', 'CLN_AFF', 'table1_monoallelic_per_proband')
    for row, key in (('specific', 'SPECIFIC_phenotype'), ('consistent', 'CONSISTENT_phenotype')):
        table = _table(monoallelic, key)
        cells[f'CLN_AFF.ad.{row}_full'] = _decimal(
            table['all_genes_tested_and_nongenetic_unlikely_and_no_other_variant'], f'CLN_AFF.{row}'
        )
        cells[f'CLN_AFF.ad.{row}_partial'] = _decimal(table['with_caveats'], f'CLN_AFF.{row}')
        cells[f'CLN_AFF.ad.{row}_other_variant'] = _decimal(
            table['PLP_in_trans_same_gene_or_explanatory_PLP_other_gene'], f'CLN_AFF.{row}'
        )
    cells['CLN_AFF.ad.not_consistent'] = _decimal(monoallelic['NOT_CONSISTENT'], 'CLN_AFF')

    biallelic = _table(raw, 'clinical_observations', 'CLN_AFF', 'table2_biallelic_per_proband')
    for row, key in _BIALLELIC_ROWS:
        values = biallelic[key]
        if not isinstance(values, list):
            raise reference.ReferenceDataError(f'CLN_AFF table 2 row {key} is not a list')
        for column, value in zip(_BIALLELIC_COLUMNS, values, strict=True):
            cells[f'CLN_AFF.arxl.{row}.{column}'] = _decimal(value, f'CLN_AFF.arxl.{row}.{column}')

    denovo = _table(raw, 'clinical_observations', 'CLN_DNV', 'table3')
    for row, key in (
        ('specific', 'SPECIFIC'),
        ('consistent', 'CONSISTENT'),
        ('not_consistent', 'NOT_CONSISTENT'),
    ):
        table = _table(denovo, key)
        cells[f'CLN_DNV.{row}.confirmed'] = _decimal(table['confirmed_parentage'], f'CLN_DNV.{row}')
        cells[f'CLN_DNV.{row}.unconfirmed'] = _decimal(table['unconfirmed_parentage'], f'CLN_DNV.{row}')

    yields = _sequence(raw, 'locus_evidence', 'LOC_PHE', 'diagnostic_yield_bins')
    for name, bin_ in zip(_YIELD_CELLS, yields, strict=True):
        if not isinstance(bin_, Mapping):
            raise reference.ReferenceDataError(f'LOC_PHE bin {name} is not a table')
        cells[f'LOC_PHE.yield.{name}'] = _decimal(bin_['points'], f'LOC_PHE.yield.{name}')
    # Step 1 gates the workflow rather than scoring it.
    cells['LOC_PHE.step1.no'] = decimal.Decimal(0)
    cells['LOC_PHE.step1.yes'] = decimal.Decimal(0)

    segregation = _table(raw, 'locus_evidence', 'LOC_SEG', 'per_cosegregation')
    dominant = _table(segregation, 'autosomal_dominant')
    recessive = _table(segregation, 'autosomal_recessive')
    semidominant = _table(segregation, 'semidominant')
    xlinked = _table(segregation, 'x_linked')
    cells['LOC_SEG.ad.het_affected'] = _decimal(dominant['heterozygous_affected'], 'LOC_SEG.ad')
    cells['LOC_SEG.ar.hom_or_chet_affected'] = _decimal(recessive['homozygous_or_compound_het_affected'], 'LOC_SEG.ar')
    cells['LOC_SEG.sd.hom_or_chet_severe'] = _decimal(
        semidominant['homozygous_or_compound_het_severely_affected'], 'LOC_SEG.sd'
    )
    cells['LOC_SEG.sd.het_affected'] = _decimal(semidominant['heterozygous_affected'], 'LOC_SEG.sd')
    cells['LOC_SEG.xl.hemi_severe_male'] = _decimal(xlinked['hemizygous_severely_affected_male'], 'LOC_SEG.xl')
    cells['LOC_SEG.xl.hom_or_chet_severe_female'] = _decimal(
        xlinked['homozygous_or_compound_het_severely_affected_female'], 'LOC_SEG.xl'
    )
    cells['LOC_SEG.xl.het_affected_female'] = _decimal(xlinked['heterozygous_affected_female'], 'LOC_SEG.xl')

    return cells


def points_for(ref: reference.Reference, cell_id: str) -> decimal.Decimal:
    """The framework's value for one observation in this row.

    Raises:
        UnknownCellError: If no table defines the cell. Never returns zero for an unknown id: a cell
            nobody priced is a transcription this module cannot score, and scoring it as nothing
            would drop the observation from a total that still looked complete.
    """
    try:
        return cell_points(ref)[cell_id]
    except KeyError as e:
        raise UnknownCellError(f'no per-observation table prices {cell_id!r}') from e


def _code_of(cell_id: str) -> str:
    """The evidence code a cell belongs to: an id opens with its code, then addresses the row."""
    return cell_id.split('.', 1)[0]


def total(ref: reference.Reference, counts: Mapping[str, int]) -> decimal.Decimal:
    """Sum one code's observations: each cell's value times how many individuals fall in it.

    The arithmetic the clinical and locus codes reach the tally already carrying. Doing it here
    rather than in prose is what keeps a stated derivation and the number beside it from disagreeing.

    Cells of two codes are refused rather than added together, because the sum reaches the tally as
    one line under one code: the other code's observations are then bounded by a range that is not
    theirs, and the preconditions the framework states for them are checked against a code the tally
    cannot see they were filed under.

    Args:
        ref: The loaded reference.
        counts: Cell id to the number of individuals recorded in that row, every id addressing the
            same code. A negative count is refused; zero contributes nothing.

    Raises:
        UnknownCellError: If a cell id is not priced.
        ValueError: If the cells address more than one code, or a count is negative.
    """
    codes = sorted({_code_of(cell_id) for cell_id in counts})
    if len(codes) > 1:
        raise ValueError(
            f'the cells address {", ".join(codes)}; a total is the observations of one code, filed under '
            'that code — sum each code over its own cells'
        )
    priced = cell_points(ref)
    running = decimal.Decimal(0)
    for cell_id, count in counts.items():
        if count < 0:
            raise ValueError(f'{cell_id} has a negative observation count: {count}')
        try:
            running += priced[cell_id] * count
        except KeyError as e:
            raise UnknownCellError(f'no per-observation table prices {cell_id!r}') from e
    return running
