"""The framework's rules that are neither a point value nor a table: routing, and the policies.

A variant type routes to one supplement and one workflow diagram, and the rest here is what the
framework states in prose: how informative variants are counted, the missense informative-variant
sub-rules, how several disorders in one gene are handled, and the critical-residue award, which is
scored as a modifier on a predictive code rather than as a code of its own.
"""

from __future__ import annotations

import dataclasses
import decimal

from themis.svcv4 import reference


@dataclasses.dataclass(frozen=True)
class VariantTypeRoute:
    """Where one variant type is scored.

    Attributes:
        supplement: The supplement that states the type's workflow.
        diagram: The workflow diagram, or None where the supplement is unreleased.
        sites: The positions the type covers, where the framework defines them.
        status: The release state, where the framework gives one.
    """

    supplement: int
    diagram: str | None
    sites: str | None
    status: str | None


VARIANT_TYPE_ROUTING = {
    'missense': VariantTypeRoute(supplement=6, diagram='Missense Workflow', sites=None, status=None),
    'nonsense': VariantTypeRoute(supplement=8, diagram='Nonsense Workflow', sites=None, status=None),
    'frameshift': VariantTypeRoute(supplement=9, diagram='Frameshift Workflow', sites=None, status=None),
    'inframe_indel': VariantTypeRoute(supplement=10, diagram='Inframe Indel Workflow', sites=None, status=None),
    'canonical_splice': VariantTypeRoute(
        supplement=11,
        diagram='Canonical Splice Workflow',
        sites='donor GT +1/+2, acceptor AG -2/-1',
        status=None,
    ),
    'intronic_synonymous': VariantTypeRoute(
        supplement=12, diagram='Intronic and Synonymous Workflow', sites=None, status=None
    ),
    'exon_deletion': VariantTypeRoute(
        supplement=13, diagram='Single Or Multiexon Del Workflow', sites=None, status=None
    ),
    'exon_dup_insertion': VariantTypeRoute(
        supplement=14, diagram='Single Or Multiexon Dup Gain Workflow', sites=None, status=None
    ),
    'start_lost': VariantTypeRoute(supplement=15, diagram='Start Lost Workflow', sites=None, status=None),
    'stop_lost': VariantTypeRoute(supplement=16, diagram='Stop Lost Workflow', sites=None, status=None),
    'non_coding': VariantTypeRoute(supplement=17, diagram=None, sites=None, status='NOT_YET_RELEASED'),
}

ROUTING_RULES = (
    '> 1 gene affected -> CNV framework (PMID 31690835), not these trees',
    'sub-exon length-changing -> InDel (SM10) / Frameshift (SM9)',
    'single exon-intron boundary deletion -> Canonical Splice (SM11)',
    'NMD predicted when PTC >= 50 bp upstream of the last exon-exon junction (never in single-exon genes)',
)


@dataclasses.dataclass(frozen=True)
class InformativeVariants:
    """How other classified variants at or near the locus are counted.

    Attributes:
        cap: The award's range.
        applied: Where in the tally the award lands, and what does not reduce it.
        distinctness: How an informative variant has to differ from the one being classified.
        counting: What is counted, and what is not.
        default_pathogenic_scoring: The default tariff per distinct pathogenic variant.
        benign_scoring: The benign side of that tariff.
        prerequisites: What has to hold before the award is taken at all.
        mde_scope: Which variants are in scope for the MDE being classified.
    """

    cap: reference.CapRange
    applied: str
    distinctness: str
    counting: str
    default_pathogenic_scoring: str
    benign_scoring: str
    prerequisites: tuple[str, ...]
    mde_scope: str


INFORMATIVE_VARIANTS = InformativeVariants(
    cap=reference.CapRange(low=decimal.Decimal('-8.0'), high=decimal.Decimal('8.0')),
    applied='AFTER the mechanism x exon matrix; NOT reduced by it',
    distinctness='must be DISTINCT from the VBC (identical variants -> CLN_AFF)',
    counting='count distinct variants only; observation count irrelevant',
    default_pathogenic_scoring=(
        '+2.0 first distinct P; +1.0 each additional P; if only LP: +1.0 first LP and +1.0 each additional'
    ),
    benign_scoring='mirror of pathogenic',
    prerequisites=(
        (
            'informative variant must itself be classified under v4 (do not take ClinVar P/LP at face value unless '
            '3-4 star and circularity excluded; no PP5/BP6 shortcut)'
        ),
        (
            'do NOT award INF when the informative variant carries the SAME codes/weights as the VBC (e.g. both '
            'nonsense with only NUL_PRD_+6) - inflates points'
        ),
    ),
    mde_scope=(
        "limit to VBC's MDE; allelic MDEs sharing LoF mechanism qualify; paralogous-gene variants may contribute "
        'modest expert-judgment evidence'
    ),
)


@dataclasses.dataclass(frozen=True)
class SubRule:
    """One of the summable missense informative-variant sub-rules (MIS_INF).

    Attributes:
        rule: What the informative variant has to be.
        points: The tariff for the first such variant and for each additional one.
    """

    rule: str
    points: str


MIS_INF_SUBRULES = (
    SubRule(
        rule='same-codon / same-amino-acid change, P/LP',
        points='+4.0 first P; +2.0 LP or each additional',
    ),
    SubRule(
        rule='distinct amino-acid change, P/LP, with Grantham distance < VBC',
        points='+2.0 first; +1.0 each additional',
    ),
    SubRule(
        rule='distinct amino-acid change, B/LB, with Grantham distance > VBC',
        points='-2.0 first; -1.0 each additional',
    ),
    SubRule(
        rule='same-amino-acid change, B/LB',
        points='-4.0 first; -2.0 each additional',
    ),
    SubRule(
        rule='critical-residue motif (collagen Gly-X-Y glycine; C2H2 Cys/His)',
        points='+2.0 once, only when no other P/LP informative variant; voided by any benign informative variant',
    ),
)


@dataclasses.dataclass(frozen=True)
class CriticalAminoAcids:
    """SM7's critical-residue award: extra points on a predictor's, not a code of its own.

    Attributes:
        standalone_code: Whether the framework gives the award a code. It does not, which is what
            bounds it by the predictive code's family cap.
        effect: What the award is worth, and for which residues.
        max_points: The ceiling on it.
        examples: The motifs the framework names as qualifying.
        constraints: What does not qualify, and when the award may not be added at all.
    """

    standalone_code: bool
    effect: str
    max_points: decimal.Decimal
    examples: tuple[str, ...]
    constraints: tuple[str, ...]

    def __post_init__(self) -> None:
        reference.validate_critical_residue_award(standalone_code=self.standalone_code, max_points=self.max_points)


CRITICAL_AMINO_ACIDS = CriticalAminoAcids(
    standalone_code=False,
    effect='up to +2.0 on top of predictor points for well-established critical individual residues (SM7 §5)',
    max_points=decimal.Decimal('2.0'),
    examples=(
        'collagen Gly-X-Y glycine',
        'disulfide-bond cysteines (FBN1/NOTCH3)',
        'C2H2/C2H4 zinc-finger Cys/His (GLI3)',
    ),
    constraints=(
        'conserved domain membership alone earns NO points',
        'immunoglobulin-like / duplicated-redundant domains generally do not qualify',
        'only add if combined PRD_+INF_ maximum not yet reached (avoid double-counting; PP3+PM1 overlap)',
    ),
)


@dataclasses.dataclass(frozen=True)
class DisorderScenario:
    """One case of a gene with more than one disorder, and what to do with it.

    Attributes:
        scenario: The relation between the disorders.
        action: Whether the evidence aggregates, and how each MDE is classified.
    """

    scenario: str
    action: str


MULTIPLE_DISORDER_POLICY = (
    DisorderScenario(
        scenario='single MDE, semidominant by dosage',
        action='aggregate mono/biallelic evidence; classify the one MDE',
    ),
    DisorderScenario(
        scenario='two MDEs, distinct inheritance but consistent mechanism',
        action='aggregate; same final class for both',
    ),
    DisorderScenario(
        scenario='single mechanism, phenotypic spectrum',
        action='aggregate; case-count thresholds scale with phenotype specificity',
    ),
    DisorderScenario(
        scenario='mutually exclusive distinct mechanisms',
        action=(
            'do NOT aggregate; classify P only for the relevant MDE; benign/LB only if relevant to ALL MDEs; VUS '
            'toward MDE with partial evidence'
        ),
    ),
    DisorderScenario(
        scenario='non-mutually-exclusive MDEs',
        action=(
            'do NOT aggregate; classify each MDE separately; note lack of evidence for others (avoid ClinVar conflicts)'
        ),
    ),
    DisorderScenario(
        scenario='unclear mechanism/distinctness',
        action='judgment; aggregate only if phenotypes close and mechanism similar',
    ),
    DisorderScenario(
        scenario='multigene CNVs',
        action='aggregate all diseases into a list; note genes with unknown MDE associations',
    ),
)
