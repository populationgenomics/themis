"""The evidence codes, and the combining caps above them.

A code's range is its own cap. Above it sit the concept caps, which bound what a variant-type path
sums under one parent code, and the category (PFD) caps, which bound that parent's total.
`CONCEPT_TO_CODES` states which codes each concept sums, and it is the only statement of the split:
`reference.independent_families` reads the other side off it, so the POP, CLN and LOC codes — which
no path sums — reach the tally on their own, under their per-code range alone.

One range is a reading rather than a transcription. `CLN_DNV`'s upper bound: SM4 sums de novo
occurrences across probands and states no cap on the sum; its +7.0 is the highest weight for one
proband, and no CLN code there carries a cross-proband cap, so the upper side is unbounded, as
`CLN_AFF`, `CLN_ALT` and `CLN_UAF` are. The ClinGen pilot calculator states [0, 12], and
`tools/svcv4-oracle` pins that divergence with the passage behind it, as it does for every other
range whose `notes` names one.
"""

from __future__ import annotations

import dataclasses
import decimal

from themis.svcv4 import reference

_SPECS = (
    reference.CodeSpec(
        code='POP_FRQ',
        family='POP',
        concept='FRQ',
        direction='benign',
        low=decimal.Decimal('-6.0'),
        high=decimal.Decimal('0.0'),
        supplement=(3,),
        notes='see population.FREQUENCY_BINS',
    ),
    reference.CodeSpec(
        code='POP_HMZ',
        family='POP',
        concept='HMZ',
        direction='benign',
        low=reference.UNBOUNDED_LOW,
        high=decimal.Decimal('0.0'),
        supplement=(3,),
        notes=(
            'per-observation -1.0 (AD hom) or -0.5, summed; from 2nd occurrence; requires age-matched penetrance '
            'near 100% and affected individuals not expected in population databases (SM3 Table 7)'
        ),
    ),
    reference.CodeSpec(
        code='CLN_CCS',
        family='CLN',
        concept='CCS',
        direction=None,
        low=decimal.Decimal('-8.0'),
        high=decimal.Decimal('8.0'),
        supplement=(4,),
        notes=(
            'PS4 case-control -> benign AND pathogenic; OR>5 -> +4.0 (SM4 §23); disables other CLN except CLN_DNV. '
            "SM4 §13 states a benign direction with no magnitude, so the range is the ClinGen pilot calculator's "
            'EVIDENCE_CODE_CAP, the only executable statement of one; tools/svcv4-oracle holds both sides to it.'
        ),
    ),
    reference.CodeSpec(
        code='CLN_AFF',
        family='CLN',
        concept='AFF',
        direction=None,
        low=decimal.Decimal('0.0'),
        high=reference.UNBOUNDED_HIGH,
        supplement=(4,),
        notes='mono Table1 (+0..+1 per proband); biallelic Table2 (up to +3.0); summed',
    ),
    reference.CodeSpec(
        code='CLN_DNV',
        family='CLN',
        concept='DNV',
        direction=None,
        low=decimal.Decimal('0.0'),
        high=reference.UNBOUNDED_HIGH,
        supplement=(4,),
        notes=(
            'specific+confirmed=+7.0 per proband, summed across probands; SM4 states no cross-proband cap, so the '
            'upper side is unbounded; additive with CLN_AFF; only the SPECIFIC row is monoallelic-only'
        ),
    ),
    reference.CodeSpec(
        code='CLN_ALT',
        family='CLN',
        concept='ALT',
        direction='benign',
        low=reference.UNBOUNDED_LOW,
        high=decimal.Decimal('0.0'),
        supplement=(4,),
        notes='not for AR; -0.5/-1.0 per proband (SM4 Table 4), summed',
    ),
    reference.CodeSpec(
        code='CLN_UAF',
        family='CLN',
        concept='UAF',
        direction='benign',
        low=reference.UNBOUNDED_LOW,
        high=decimal.Decimal('0.0'),
        supplement=(4,),
        notes='requires age-matched penetrance; points per unaffected individual (SM4 Table 5), summed',
    ),
    reference.CodeSpec(
        code='LOC_PHE',
        family='LOC',
        concept='PHE',
        direction=None,
        low=decimal.Decimal('0.0'),
        high=decimal.Decimal('4.0'),
        supplement=(5,),
        notes=(
            'diagnostic-yield bins; LOC_PHE + LOC_SEG capped JOINTLY at +4.0 (SM5 §5), which no per-code range can '
            'state'
        ),
    ),
    reference.CodeSpec(
        code='LOC_SEG',
        family='LOC',
        concept='SEG',
        direction=None,
        low=decimal.Decimal('-4.0'),
        high=decimal.Decimal('4.0'),
        supplement=(5,),
        notes=(
            '+per affected/unaffected; non-seg -> -4.0 (AD/ARhom/XL); LOC_PHE + LOC_SEG capped JOINTLY at +4.0 '
            '(SM5 §5), which no per-code range can state. Range [-4,4] kept intentionally: the ClinGen pilot '
            "calculator clamps the benign floor to 0 ([0,4]), which drops SM5's non-segregation -4.0; ours is more "
            'correct (flag for spec confirmation).'
        ),
    ),
    reference.CodeSpec(
        code='MIS_PRD',
        family='MIS',
        concept='PRD',
        direction=None,
        low=decimal.Decimal('-4.0'),
        high=decimal.Decimal('4.0'),
        supplement=(6,),
        notes=(
            'ONE calibrated predictor chosen in advance '
            '(BayesDel/MutPred2/REVEL/VEST4/AlphaMissense/ESM1b/VARITY_R); positive PRD scaled by transcript '
            'relevance All=x1/Most=x0.5/Few=x0'
        ),
    ),
    reference.CodeSpec(
        code='MIS_FXN',
        family='MIS',
        concept='FXN',
        direction=None,
        low=decimal.Decimal('-8.0'),
        high=decimal.Decimal('8.0'),
        supplement=(6,),
        notes='MIS_PRD + MIS_FXN combined capped [-8.0, 6.0]',
    ),
    reference.CodeSpec(
        code='MIS_INF',
        family='MIS',
        concept='INF',
        direction=None,
        low=decimal.Decimal('-8.0'),
        high=decimal.Decimal('8.0'),
        supplement=(6,),
        notes='four summable sub-rules; see policies.MIS_INF_SUBRULES',
    ),
    reference.CodeSpec(
        code='NUL_PRD',
        family='NUL',
        concept='PRD',
        direction=None,
        low=decimal.Decimal('0.0'),
        high=decimal.Decimal('6.0'),
        supplement=(8, 9, 13, 14, 15, 16),
        notes=(
            'NMD/loss-of-transcript path; initial +6.0 (+4.0 NSD), scaled by the mechanism x exon matrix. Code cap '
            '[0,6], the union of the subgenic ceilings SM8/SM9/SM15 state; the whole-gene-deletion +10.0 tier is '
            'admitted by the NUL_PFD category cap [-8,10] in CATEGORY_CAPS, not by this code.'
        ),
    ),
    reference.CodeSpec(
        code='NUL_FXN',
        family='NUL',
        concept='FXN',
        direction=None,
        low=decimal.Decimal('-8.0'),
        high=decimal.Decimal('8.0'),
        supplement=(8, 9, 13, 14, 15, 16),
        notes='must confirm transcript/protein loss',
    ),
    reference.CodeSpec(
        code='NUL_INF',
        family='NUL',
        concept='INF',
        direction=None,
        low=decimal.Decimal('-8.0'),
        high=decimal.Decimal('8.0'),
        supplement=(8, 9, 13, 14, 15, 16),
        notes='same-exon NMD PTC variants +2.0 first P/+1.0 LP/+1.0 additional',
    ),
    reference.CodeSpec(
        code='CDS_PRD',
        family='CDS',
        concept='PRD',
        direction=None,
        low=decimal.Decimal('-1.0'),
        high=decimal.Decimal('6.0'),
        supplement=(8, 9, 10, 13, 14, 15, 16),
        notes='coding/rescue path; by protein-fraction/critical-domain; -1.0 if functional alt-start',
    ),
    reference.CodeSpec(
        code='CDS_FXN',
        family='CDS',
        concept='FXN',
        direction=None,
        low=decimal.Decimal('-8.0'),
        high=decimal.Decimal('8.0'),
        supplement=(8, 9, 10, 13, 14, 15, 16),
        notes='PRD+FXN cap typically [-8.0, 9.0]',
    ),
    reference.CodeSpec(
        code='CDS_INF',
        family='CDS',
        concept='INF',
        direction=None,
        low=decimal.Decimal('-8.0'),
        high=decimal.Decimal('8.0'),
        supplement=(8, 9, 10, 13, 14, 15, 16),
        notes='PTC downstream of VBC / between VBC and alt-start, per path',
    ),
    reference.CodeSpec(
        code='SPL_PRD',
        family='SPL',
        concept='PRD',
        direction=None,
        low=decimal.Decimal('-1.0'),
        high=decimal.Decimal('6.0'),
        supplement=(6, 11, 12),
        notes='initial by splice prediction; canonical NMD +6.0, intronic/synon NMD +3.0',
    ),
    reference.CodeSpec(
        code='SPL_SPA',
        family='SPL',
        concept='SPA',
        direction=None,
        low=decimal.Decimal('-6.0'),
        high=decimal.Decimal('3.0'),
        supplement=(6, 11, 12),
        notes=(
            'RNA splice assay; scales PRD in path, or standalone -2.0..+2.0 in uncertain path. Range is the union '
            'of the per-colour bounds: 0.0 to +3.0 (SM6 §69, SM12 §42), -2.0 to +2.0 (SM6 §98, SM11 §72), -2.0 to '
            "0.0 (SM6 §119, SM11 §94, SM12 §109), and the -6.0 floor stated only in SM11's figure boxes (Canonical "
            'Splice Workflow.decision-tree.md:112 and :143). Nothing scores against the union: builders validate '
            'against splice_tree.spa_bounds, which derives the bound of the colour in play.'
        ),
    ),
    reference.CodeSpec(
        code='SPL_FXN',
        family='SPL',
        concept='FXN',
        direction=None,
        low=decimal.Decimal('-8.0'),
        high=decimal.Decimal('8.0'),
        supplement=(11, 12),
        notes='protein function, distinct from SPA',
    ),
    reference.CodeSpec(
        code='SPL_INF',
        family='SPL',
        concept='INF',
        direction=None,
        low=decimal.Decimal('-8.0'),
        high=decimal.Decimal('8.0'),
        supplement=(6, 11, 12),
        notes='+2.0 first P/+1.0 LP/+1.0 additional',
    ),
)

CODES = {spec.code: spec for spec in _SPECS}

CONCEPT_CAPS = {
    'POP': reference.CapRange(low=reference.UNBOUNDED_LOW, high=decimal.Decimal('0.0')),
    'CLN': reference.CapRange(low=reference.UNBOUNDED_LOW, high=reference.UNBOUNDED_HIGH),
    'LOC': reference.CapRange(low=decimal.Decimal('0.0'), high=decimal.Decimal('4.0')),
    'MIS': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('6.0')),
    'MIS_INF': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('8.0')),
    'CDS': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('9.0')),
    'CDS_INF': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('8.0')),
    'NUL': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('10.0')),
    'NUL_INF': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('8.0')),
    'SPL': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('9.0')),
    'SPL_FXN': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('9.0')),
    'SPL_INF': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('8.0')),
    'SPL_PRD_SPA': reference.CapRange(low=decimal.Decimal('-3.0'), high=decimal.Decimal('6.0')),
}

CATEGORY_CAPS = {
    'MIS_PFD': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('9.0')),
    'CDS_PFD': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('10.0')),
    'NUL_PFD': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('10.0')),
    'SPL_PFD': reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('10.0')),
}

CONCEPT_TO_CODES = {
    'MIS': ('MIS_PRD', 'MIS_FXN', 'MIS_INF'),
    'CDS': ('CDS_PRD', 'CDS_FXN', 'CDS_INF'),
    'NUL': ('NUL_PRD', 'NUL_FXN', 'NUL_INF'),
    'SPL': ('SPL_PRD', 'SPL_FXN', 'SPL_INF', 'SPL_SPA'),
}


@dataclasses.dataclass(frozen=True)
class CapProvenance:
    """Where each cap above the per-code ranges is read from.

    Attributes:
        what: What the cap hierarchy is.
        concept_caps: The passage stating each concept cap, and which are implied rather than stated.
        category_caps: The passage stating each category (PFD) cap.
        concept_to_codes: Where each summed code is introduced.
    """

    what: str
    concept_caps: str
    category_caps: str
    concept_to_codes: str


CAP_PROVENANCE = CapProvenance(
    what=(
        'the concept-level and category-level combining caps above the per-code caps in CODES, and which codes each '
        'parent code sums'
    ),
    concept_caps=(
        'stated: LOC SM5 §34 (the joint LOC_PHE + LOC_SEG cap, SM5 §5); MIS SM6 §19; MIS_INF SM6 §29; CDS SM8 §34; '
        'CDS_INF SM8 §38; NUL SM8 §13; NUL_INF SM8 §17; SPL and SPL_FXN SM11 §25; SPL_INF SM11 §27. SPL_PRD_SPA is '
        "stated once per colour, not once: +6.0 is SM12 §42's ceiling and -3.0 the widest of the four floors (SM6 "
        '§48 and §69 0.0, SM12 §42 -1.0, SM6 §98 -2.0, SM6 §119 / SM11 §94 / SM12 §109 -3.0). Implied, not stated: '
        "POP's 0.0 ceiling, since SM3 §2 and §72 make both POP codes benign-only; CLN's unbounded pair, since SM4 "
        '§77 and §143 sum per observation under no stated bound.'
    ),
    category_caps=(
        'the parent code each supplement caps the total at: MIS_PFD SM6 §31; CDS_PFD SM8 §38; NUL_PFD SM8 §23; '
        'SPL_PFD SM11 §31'
    ),
    concept_to_codes=(
        'where each summed code is introduced: MIS SM6 §15/§19/§29; CDS SM8 §32/§34/§38; NUL SM8 §11/§13/§17; SPL '
        'SM11 §17/§19/§25/§27'
    ),
)

CODE_ABBREVIATIONS = {
    'AFF': 'affected individual',
    'ALT': 'alternate cause',
    'CCS': 'case-control study',
    'CDS': 'coding DNA sequence (i.e., not NUL / not NMD path)',
    'DNV': 'de novo',
    'FRQ': 'frequency',
    'FXN': 'function (functional assay)',
    'INF': 'informative variants',
    'LOC': 'locus',
    'NUL': 'null (NMD / loss-of-transcript path)',
    'MIS': 'missense',
    'NCG': 'non-coding gene',
    'PHE': 'phenotype',
    'POP': 'population',
    'PRD': 'prediction (in silico)',
    'REG': 'regulatory',
    'SEG': 'segregation',
    'SPA': 'splice assay (RNA)',
    'UAF': 'unaffected',
    'HMZ': 'homozygous/hemizygous population occurrence',
    'NA': 'not applicable',
    'ND': 'no data',
}

CONCEPTS = {
    'PRD': 'In-silico / predictive evidence (initial point value, may be scaled by mechanism x exon matrix)',
    'FXN': 'Functional-assay evidence (Brnich/OddsPath calibrated)',
    'INF': 'Informative-variant evidence (other classified variants at/near the locus)',
    'SPA': 'Splice-assay (RNA) evidence, specific to splice codes',
}


@dataclasses.dataclass(frozen=True)
class V3Crosswalk:
    """How v3's criteria map onto v4's codes.

    Attributes:
        superseded_by: Each v3 criterion, and the v4 codes that carry what it used to.
        dropped: Each v3 criterion v4 drops, and why.
    """

    superseded_by: dict[str, tuple[str, ...]]
    dropped: dict[str, str]


V3_TO_V4_MAPPING = V3Crosswalk(
    superseded_by={
        'PVS1': ('NUL_PRD', 'CDS_PRD'),
        'PS1': ('PRD_MIS', 'CDS_INF', 'NUL_INF', 'SPL_INF'),
        'PS2': ('CLN_DNV',),
        'PS3': ('MIS_FXN', 'NUL_FXN', 'CDS_FXN', 'SPL_FXN', 'SPL_SPA'),
        'PS4': ('CLN_CCS', 'CLN_AFF'),
        'PM3': ('CLN_AFF',),
        'PM4': ('CDS',),
        'PM5': ('MIS_INF', 'CDS_INF', 'NUL_INF', 'SPL_INF'),
        'PM6': ('CLN_DNV',),
        'PP1': ('LOC_SEG',),
        'PP3': ('MIS_PRD', 'SPL_PRD'),
        'PP4': ('LOC_PHE',),
        'PM2': ('POP_FRQ',),
        'BA1': ('POP_FRQ',),
        'BS1': ('POP_FRQ',),
        'BS2': ('POP_HMZ', 'CLN_UAF'),
        'BS3': ('MIS_FXN', 'NUL_FXN', 'CDS_FXN', 'SPL_FXN', 'SPL_SPA'),
        'BS4': ('LOC_SEG',),
        'BP2': ('CLN_ALT',),
        'BP5': ('CLN_ALT',),
        'BP3': ('CDS',),
        'BP4': ('MIS_PRD', 'SPL_PRD'),
        'BP7': ('SPL_PRD',),
    },
    dropped={
        'PM1': (
            'dropped for missense (predictors now reach +4.0); retained narrowly (e.g. collagen Gly-X-Y glycine). '
            'See SM6/SM7.'
        ),
        'PP2': 'dropped; redundant with calibrated predictors.',
        'BP1': 'dropped; redundant with calibrated predictors.',
        'PP5': 'dropped (ClinGen 2018 recommendation, PMID 29543229).',
        'BP6': 'dropped (same as PP5).',
    },
)

reference.validate_concept_to_codes(CONCEPT_TO_CODES, CODES)

INDEPENDENT_FAMILIES = reference.independent_families(CODES, CONCEPT_TO_CODES)
