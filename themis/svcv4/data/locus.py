"""SM5's locus evidence: the diagnostic-yield bins, and what a cosegregation is worth.

The two locus codes share one ceiling — SM5 caps LOC_PHE + LOC_SEG jointly at +4.0, which no
per-code range can state, so the joint cap is the LOC concept cap in `codes`, and each code's own
range is `codes.CODES` too: the range a total is clamped against is stated once, where the clamp
reads it. Both codes are priced per observation: a yield bin per proband, a cosegregation per
informative individual.
"""

from __future__ import annotations

import dataclasses
import decimal

from themis.svcv4 import reference

COMBINED_CAP = 'LOC total capped at +4.0 (below the +6.0 LP threshold by design)'


@dataclasses.dataclass(frozen=True)
class PhenotypeEvidence:
    """LOC_PHE: how specifically the phenotype points at this locus.

    Attributes:
        diagnostic_yield_bins: What one proband in each yield bin is worth.
        ultra_rare_disorders: The award a semantic-similarity algorithm can reach where the MDE is
            too rare for a yield estimate.
        proband_rule: Which proband the award is read from, and which variants it applies to.
    """

    diagnostic_yield_bins: tuple[reference.ObservationRow, ...]
    ultra_rare_disorders: str
    proband_rule: str


LOC_PHE = PhenotypeEvidence(
    diagnostic_yield_bins=(
        reference.ObservationRow(cell='0_33', description='< 33%', points=decimal.Decimal('0.0')),
        reference.ObservationRow(cell='33_51', description='33-50%', points=decimal.Decimal('1.0')),
        reference.ObservationRow(cell='51_68', description='~51-67%', points=decimal.Decimal('2.0')),
        reference.ObservationRow(cell='68_82', description='68-81%', points=decimal.Decimal('3.0')),
        reference.ObservationRow(cell='ge_82', description='> 82%', points=decimal.Decimal('4.0')),
    ),
    ultra_rare_disorders=('prevalence < 1/1,000,000: up to +2.0 from phenotype semantic-similarity algorithms'),
    proband_rule=(
        'use single most specifically-phenotyped proband; biallelic compound-het applies to both variants; '
        'homozygote applies to the single variant'
    ),
)

_COSEGREGATION_PROVENANCE = (
    'SM5 Figure 2, which exists in the supplement only as an image; read from the transcription '
    'svcv4-docs/code-specific-workflow-guidance/tables/SM5-segregation-points.md, its affected rows (:59-:71), at '
    'the revision meta.CITED_DOCUMENTS pins. Its unaffected rows are per_unaffected, not here.'
)


@dataclasses.dataclass(frozen=True)
class Cosegregation:
    """SM5 Figure 2's affected rows: what one cosegregating affected individual is worth.

    Attributes:
        provenance: The image the rows were read from, and which of its rows they are.
        rows: One row per inheritance pattern and zygosity the figure prices.
    """

    provenance: str
    rows: tuple[reference.ObservationRow, ...]


_NON_SEGREGATION_PROVENANCE = (
    "SM5 §33's recommendation, which is the reading codes.py's LOC_SEG notes already state against the calculator's "
    'floor — scoring the observations off the same reading opens no second departure. Its autosomal-recessive row is '
    "SM5's closing note, which withholds benignity from autosomal recessive outright, against the co-segregation "
    'section granting it with homozygosity: both readings are in the text, so both are carried and a caller records '
    'which it took (docs/design/curation-surface.md).'
)


@dataclasses.dataclass(frozen=True)
class NonSegregations:
    """SM5's non-segregation rows: what one non-segregating observation is worth.

    Attributes:
        provenance: The passages the rows were read from, and the two readings the recessive row
            carries.
        rows: One row per inheritance pattern the recommendation reaches.
    """

    provenance: str
    rows: tuple[reference.ObservationRow, ...]


@dataclasses.dataclass(frozen=True)
class NonSegregation:
    """What an observation that does not segregate does to the locus codes.

    Attributes:
        effect: What non-segregation does to the two locus codes' awards.
        benign_points: The benign award it earns, and where the re-analysis goes next.
        ar_note: Why a recessive MDE earns none of it.
        per_observation: What one non-segregating observation is worth, by inheritance pattern.
    """

    effect: str
    benign_points: str
    ar_note: str
    per_observation: NonSegregations


@dataclasses.dataclass(frozen=True)
class SegregationEvidence:
    """LOC_SEG: what the variant's segregation through a family is worth.

    Attributes:
        per_affected_individual: The band one affected individual falls in, by informative meioses.
        per_cosegregation: SM5 Figure 2's affected rows.
        per_unaffected: The award an unaffected carrier earns, and the preconditions on it.
        non_segregation: What non-segregation does.
    """

    per_affected_individual: str
    per_cosegregation: Cosegregation
    per_unaffected: str
    non_segregation: NonSegregation


LOC_SEG = SegregationEvidence(
    per_affected_individual='+1.0 to +2.0 (informative meioses)',
    per_cosegregation=Cosegregation(
        provenance=_COSEGREGATION_PROVENANCE,
        rows=(
            reference.ObservationRow(
                cell='ad.het_affected',
                description='autosomal_dominant.heterozygous_affected',
                points=decimal.Decimal('1.0'),
            ),
            reference.ObservationRow(
                cell='ar.hom_or_chet_affected',
                description='autosomal_recessive.homozygous_or_compound_het_affected',
                points=decimal.Decimal('2.0'),
            ),
            reference.ObservationRow(
                cell='sd.hom_or_chet_severe',
                description='semidominant.homozygous_or_compound_het_severely_affected',
                points=decimal.Decimal('2.0'),
            ),
            reference.ObservationRow(
                cell='sd.het_affected',
                description='semidominant.heterozygous_affected',
                points=decimal.Decimal('1.0'),
            ),
            reference.ObservationRow(
                cell='xl.hemi_severe_male',
                description='x_linked.hemizygous_severely_affected_male',
                points=decimal.Decimal('1.0'),
            ),
            reference.ObservationRow(
                cell='xl.hom_or_chet_severe_female',
                description='x_linked.homozygous_or_compound_het_severely_affected_female',
                points=decimal.Decimal('1.0'),
            ),
            reference.ObservationRow(
                cell='xl.het_affected_female',
                description='x_linked.heterozygous_affected_female',
                points=decimal.Decimal('1.0'),
            ),
        ),
    ),
    per_unaffected=(
        'up to +1.0 (AR: +0.4 per unaffected VBC carrier, SM5 §28, which scopes the award to carriers where SM5 '
        "Figure 2's AR row reads 'heterozygous or wild type'); only if penetrance ~100% and phase established"
    ),
    non_segregation=NonSegregation(
        effect='zeroes LOC_PHE and LOC_SEG (log-odds -inf at Theta=0)',
        benign_points=(
            '-4.0 for AD / AR-homozygous / X-linked (legacy BS4); then re-analyze other loci excluding recombinant '
            'locus'
        ),
        ar_note='AR non-segregation gives NO benignity evidence',
        per_observation=NonSegregations(
            provenance=_NON_SEGREGATION_PROVENANCE,
            rows=(
                reference.ObservationRow(
                    cell='autosomal_dominant',
                    description='autosomal_dominant',
                    points=decimal.Decimal('-4.0'),
                ),
                reference.ObservationRow(
                    cell='autosomal_recessive_homozygous',
                    description='autosomal_recessive_with_homozygosity',
                    points=decimal.Decimal('-4.0'),
                ),
                reference.ObservationRow(cell='x_linked', description='x_linked', points=decimal.Decimal('-4.0')),
                reference.ObservationRow(
                    cell='autosomal_recessive_no_benignity',
                    description='autosomal_recessive_benignity_withheld',
                    points=decimal.Decimal('0.0'),
                ),
            ),
        ),
    ),
)
