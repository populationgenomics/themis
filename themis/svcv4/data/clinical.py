"""SM4's clinical observations: the case-control trigger, and the tables priced per individual.

The tables here price one observed individual, and a code's contribution is that value times how many
individuals fall in the row — which is what `observations` addresses by cell id and sums. CLN_CCS is
the exception: a case-control study is one determination about a whole cohort, so its rows are read
once. Each row keeps the name SM4's transcription gives it beside the fragment the cell id is built
from, so a row can be matched to the supplement without going through the fragment.

The precondition SM4 states over these tables is scored, not documentation: `CLN_AFF` is withdrawn
rather than reduced where POP_FRQ took any other value.
"""

from __future__ import annotations

import dataclasses
import decimal

from themis.svcv4 import reference

PRECONDITION = reference.PopFrqPrecondition(
    conditioned_codes=frozenset({'CLN_AFF'}),
    admissible_points=frozenset({decimal.Decimal('0.0'), decimal.Decimal('-1.0')}),
    source='SM4 §25, which admits Tables 1 and 2 only at those two POP_FRQ point thresholds',
)


@dataclasses.dataclass(frozen=True)
class CaseControlDeterminations:
    """SM4 Figure 1's three case-control determinations: what each of them is worth.

    Attributes:
        provenance: The figure the rows were read from, and what its unvalued row rests on.
        rows: One row per determination the figure routes an odds ratio to.
    """

    provenance: str
    rows: tuple[reference.ObservationRow, ...]


_DETERMINATION_PROVENANCE = (
    'SM4 Figure 1, which exists in the supplement only as an image. It routes an odds ratio near or below 1.0 to a '
    'statistician rather than to a number, so that row is the framework declining to value it rather than a cell '
    'nobody mapped; the reading is argued in docs/design/curation-surface.md.'
)


@dataclasses.dataclass(frozen=True)
class CaseControlStudy:
    """CLN_CCS: a case-control odds ratio, and what it does to the other clinical codes.

    Attributes:
        trigger: The odds ratio that earns the award, and what it earns.
        requires: The study size and quality the award needs.
        effect_when_applied: Which other codes the award withdraws.
        per_determination: What each determination the figure states is worth.
    """

    trigger: str
    requires: tuple[str, ...]
    effect_when_applied: str
    per_determination: CaseControlDeterminations


CLN_CCS = CaseControlStudy(
    trigger='OR > 5.0 -> +4.0',
    requires=('>= 5 case observations', '>= 100 unrelated cases', 'matched controls', 'CI excludes 1.0'),
    effect_when_applied='all other CLN codes = NA except CLN_DNV',
    per_determination=CaseControlDeterminations(
        provenance=_DETERMINATION_PROVENANCE,
        rows=(
            reference.ObservationRow(
                cell='or_above_5', description='calculated OR above 5.0', points=decimal.Decimal('4.0')
            ),
            reference.ObservationRow(
                cell='ci_includes_1',
                description='confidence interval includes 1.0',
                points=decimal.Decimal('0.0'),
            ),
            reference.ObservationRow(cell='or_near_or_below_1', description='OR near to or below 1.0', points=None),
        ),
    ),
)

_MONOALLELIC = reference.ObservationGrid(
    columns=(
        reference.ObservationColumn(
            cell='full', heading='all_genes_tested_and_nongenetic_unlikely_and_no_other_variant'
        ),
        reference.ObservationColumn(cell='partial', heading='with_caveats'),
        reference.ObservationColumn(
            cell='other_variant', heading='PLP_in_trans_same_gene_or_explanatory_PLP_other_gene'
        ),
    ),
    rows=(
        reference.ObservationGridRow(
            cell='specific',
            description='SPECIFIC_phenotype',
            points=reference.printed_decimals('1.0', '0.5', '0.0'),
        ),
        reference.ObservationGridRow(
            cell='consistent',
            description='CONSISTENT_phenotype',
            points=reference.printed_decimals('0.5', '0.25', '0.0'),
        ),
    ),
    collapsed_rows=(
        reference.ObservationRow(cell='not_consistent', description='NOT_CONSISTENT', points=decimal.Decimal('0.0')),
    ),
)

_BIALLELIC = reference.ObservationGrid(
    columns=(
        reference.ObservationColumn(cell='trans_plp_confirmed', heading='confirmed_in_trans_PLP'),
        reference.ObservationColumn(cell='trans_plp_assumed', heading='assumed_in_trans_PLP'),
        reference.ObservationColumn(cell='trans_vus_confirmed', heading='confirmed_in_trans_VUS'),
        reference.ObservationColumn(cell='homozygous', heading='homozygous'),
        reference.ObservationColumn(cell='no_second_variant', heading='no_or_cis_or_unknown_second_variant'),
    ),
    rows=(
        reference.ObservationGridRow(
            cell='consistent_full_lt_0_0001',
            description='consistent_all_tested_cooccurrence_lt_0.0001',
            points=reference.printed_decimals('3.0', '1.5', '1.5', '1.0', '0.0'),
        ),
        reference.ObservationGridRow(
            cell='consistent_full_0_0001_0_01',
            description='consistent_all_tested_cooccurrence_0.0001_to_0.01',
            points=reference.printed_decimals('2.0', '1.0', '1.0', '1.0', '0.0'),
        ),
        reference.ObservationGridRow(
            cell='consistent_partial',
            description='with_caveats',
            points=reference.printed_decimals('1.0', '0.75', '0.5', '0.5', '0.0'),
        ),
        reference.ObservationGridRow(
            cell='consistent_other_variant',
            description='explanatory_PLP_other_gene',
            points=reference.printed_decimals('0.0', '0.0', '0.0', '0.0', '0.0'),
        ),
        reference.ObservationGridRow(
            cell='not_consistent',
            description='NOT_CONSISTENT',
            points=reference.printed_decimals('0.0', '0.0', '0.0', '0.0', '0.0'),
        ),
    ),
    collapsed_rows=(),
)


@dataclasses.dataclass(frozen=True)
class AffectedIndividuals:
    """CLN_AFF: what one affected proband carrying the variant is worth.

    Attributes:
        aggregation: What the per-proband values are summed over.
        monoallelic: SM4 Table 1, a monoallelic proband.
        biallelic: SM4 Table 2, a biallelic proband.
        cooccurrence_formula: How Table 2's co-occurrence frequency is computed.
    """

    aggregation: str
    monoallelic: reference.ObservationGrid
    biallelic: reference.ObservationGrid
    cooccurrence_formula: str


CLN_AFF = AffectedIndividuals(
    aggregation='sum across unrelated probands',
    monoallelic=_MONOALLELIC,
    biallelic=_BIALLELIC,
    cooccurrence_formula='(in-trans + unphased counts) / 125,748 (gnomAD v2)',
)

_DE_NOVO = reference.ObservationGrid(
    columns=(
        reference.ObservationColumn(cell='confirmed', heading='confirmed_parentage'),
        reference.ObservationColumn(cell='unconfirmed', heading='unconfirmed_parentage'),
    ),
    rows=(
        reference.ObservationGridRow(
            cell='specific', description='SPECIFIC', points=reference.printed_decimals('7.0', '2.0')
        ),
        reference.ObservationGridRow(
            cell='consistent', description='CONSISTENT', points=reference.printed_decimals('4.0', '1.0')
        ),
        reference.ObservationGridRow(
            cell='not_consistent', description='NOT_CONSISTENT', points=reference.printed_decimals('0.0', '0.0')
        ),
    ),
    collapsed_rows=(),
)


@dataclasses.dataclass(frozen=True)
class DeNovoOccurrences:
    """CLN_DNV: what one de novo occurrence is worth.

    Attributes:
        additive_with: The code this one is scored on top of rather than instead of.
        row_applicability: Which rows a monoallelic and a biallelic proband may take.
        table3: SM4 Table 3, by phenotype specificity and parentage confirmation.
        note: Which occurrences count as de novo.
    """

    additive_with: str
    row_applicability: dict[str, str]
    table3: reference.ObservationGrid
    note: str


CLN_DNV = DeNovoOccurrences(
    additive_with='CLN_AFF',
    row_applicability={
        'SPECIFIC': 'monoallelic only',
        'CONSISTENT': (
            'monoallelic or biallelic; a biallelic proband takes this row, Table 2 having one consistency category '
            '(SM4 §140)'
        ),
        'NOT_CONSISTENT': 'monoallelic or biallelic',
    },
    table3=_DE_NOVO,
    note='mosaic events count (exclude CHIP / revertants)',
)

# SM4 Table 4 states the three values in this order and names no rows; the names are the reading of
# what each row is, which the cell ids carry.
CLN_ALT = reference.AlternateCauseRows(
    more_severe=reference.ObservationRow(
        cell='more_severe',
        description='phenotype more severe than the alternate cause explains',
        points=decimal.Decimal('0.0'),
    ),
    not_more_severe=reference.ObservationRow(
        cell='not_more_severe',
        description='phenotype no more severe than the alternate cause explains',
        points=decimal.Decimal('-0.5'),
    ),
    not_consistent_recessive=reference.ObservationRow(
        cell='not_consistent_recessive',
        description='observation not consistent with the recessive MDE',
        points=decimal.Decimal('-1.0'),
    ),
)

# SM4 Table 5 collapses the zygosities into two columns: the variant carried on its own account or in
# trans with a P variant, and in trans with an LP one.
CLN_UAF = reference.ObservationGrid(
    columns=(
        reference.ObservationColumn(cell='as_p', heading='dominant_or_semidominant_or_homo_hemizygous_or_in_trans_P'),
        reference.ObservationColumn(cell='as_lp', heading='in_trans_LP'),
    ),
    rows=(
        reference.ObservationGridRow(
            cell='near_100',
            description='near_100pct_penetrance',
            points=reference.printed_decimals('-4.0', '-2.0'),
        ),
        reference.ObservationGridRow(
            cell='80_100',
            description='penetrance_80_100pct',
            points=reference.printed_decimals('-2.0', '-1.0'),
        ),
    ),
    collapsed_rows=(
        reference.ObservationRow(cell='lt_80', description='penetrance_lt_80pct', points=decimal.Decimal('0.0')),
    ),
)
