# Design: reading SVCv4 — the interpretations the evidence layer applies

> **Draft standard and evaluation software notice:** ClinGen released SVCv4 as a pilot in July 2026. Its point values,
> thresholds, evidence code names, and data model may change before formal publication (targeted for *Genetics in
> Medicine* around January 2027), and Supplementary Material 17 (non-coding variants) remains unreleased. This
> repository is evaluation software for testing automated classification against curator worksheets; it is not a
> validated clinical implementation, is not for diagnostic use, and does not produce clinical variant classifications.
> Every interpretation recorded here must be re-verified against the final published standard and the
> [ClinGen Pilot Calculator](https://calculator.clinicalgenome.org/v4/pilot/ui/classification).

**Related:** [`evidence-interfaces.md`](evidence-interfaces.md) (the gRPC services and in-sandbox library that apply
these readings), [`curation-surface.md`](curation-surface.md) (the curator workspace and entity registration),
[`svcv4-worksheet-transcription.md`](svcv4-worksheet-transcription.md) (the curation worksheet transcription),
[`svcv4-classification-widget.md`](svcv4-classification-widget.md) (the live scoring widget),
[`run-review-loop.md`](run-review-loop.md) (the loop that evaluates classification runs against curator worksheets),
[`literature-evidence-layer.md`](literature-evidence-layer.md) (the paper-derived evidence alongside curated sources),
[`../PRODUCT.md`](../PRODUCT.md) (the product frame: §6 facts and judgement, §12 the Spike); terms in
[`../../GLOSSARY.md`](../../GLOSSARY.md).

## Overview

Running the ClinGen/ACMG/AMP/CAP Sequence Variant Classification v4 (SVCv4) framework without a human at every step
requires deciding in advance what the standard means where its text is silent or ambiguous. An automated pipeline must
know which ClinVar classification strings count as a pathogenic assertion, which population frequency figure `POP_FRQ`
compares against a threshold, what evidence licenses the *All* exon-relevance tier, and which sentence to follow when a
supplement contradicts itself. These interpretations are clinical judgements about the standard rather than engineering
implementation choices, and each one can move a variant's total by several points. This document records each
interpretation alongside the supplement it rests on, the alternatives considered, and the direction in which the
rejected reading errs, so a clinical reviewer can audit the rules without reading service code.

A consistent discipline governs every interpretation in this document. First, the system fixes each reading once in
policy before scoring any variant. Choosing a missense predictor, a disease-allele frequency threshold (DAFT) method, or
a ClinVar review-status floor after inspecting the resulting points commits the multiple-testing error that
Supplementary Material 6 (SM6) warns against for predictors. Second, when the framework specifies a disposition for an
unsettled input, [`themis.svcv4`](../../themis/svcv4) applies that disposition directly rather than inventing a general
rule to pick the least-committed value, which would systematically bias point totals in one direction. Finally, when a
judgement input remains genuinely open and changes the resulting classification band, the caller reports no single class
and instead shows the point total under each competing reading.

When a Variant Curation Expert Panel (VCEP) publishes a gene-specific criteria specification, that specification
supersedes the general readings in this document for the gene it covers. The evidence layer fetches any published VCEP
specification directly so the analyst can apply it
([`evidence-interfaces.md`](evidence-interfaces.md#goal-does-a-vcep-publish-its-own-criteria-specification)).

## Background

SVCv4 classifies a variant relative to a **Monogenic disease entity** (MDE), which pairs a gene, a disease, and a mode
of inheritance. Under SM21, an allelic disorder that arises through a different molecular mechanism is a distinct
disease entity and is scored separately. Each applicable evidence code contributes positive (pathogenic) or negative
(benign) points, and the summed total maps onto a classification band: Benign (`B`), Likely Benign (`LB`), Variant of
Uncertain Significance (`VUS`, subdivided into `VUS-high`, `VUS-mid`, and `VUS-low`), Likely Pathogenic (`LP`), or
Pathogenic (`P`).

The framework modifies straight addition in two places. First, positive predictive points are scaled by a two-axis
matrix that crosses the entity's loss-of-function **molecular mechanism** level (`Established` ×1.0, `Likely` ×0.5,
`Suspected` ×0.25, `Uncertain` ×0) with the assessed exon's **exon relevance** tier (`All`, `Most`, or `Few`). Second,
the curated **gene-disease validity** level both caps the final classification band and gates whether a positive
mechanism multiplier may be claimed at all. Evidence codes group into families by what they measure: population
frequency (`POP_FRQ`, `POP_HMZ`), in-silico prediction (`*_PRD`), other classified variants at the same amino-acid
residue or exon (`*_INF`), functional assays (`*_FXN`, `SPL_SPA`), clinical case observations (`CLN_*`), and phenotype
specificity and family co-segregation (`LOC_PHE`, `LOC_SEG`).

The normative specification lives in the per-topic **Supplementary Materials**, cited throughout this document as SM*n*
([`evidence-interfaces.md`](evidence-interfaces.md#background), [`../../GLOSSARY.md`](../../GLOSSARY.md)). Because the
July 2026 release is a pilot, several supplements disagree with one another, a few disagree with their own tables or
figures, and SM17 (non-coding variants) has not yet been released. In addition, the text is written for a human analyst:
the supplements frequently delegate a decision to "an experienced analyst with a broad understanding of the pathogenesis
of genetic disease" without stating a quantitative test. Those delegations are intentional hand-offs to clinical
judgement that the model performs with explicit reasoning and that a curator can override. This document covers the
narrower surface where the standard commits to a testable rule that requires careful reading to extract, or where the
standard commits to two incompatible rules at once.

## Non-goals

- **Guidance for human clinical curation.** These readings exist to make an autonomous classification run reproducible
  and auditable. They do not advise clinical curators, and none of them has been validated against clinical outcomes.
- **Inventing numeric thresholds where the standard states none.** When a supplement leaves a threshold open to clinical
  judgement, the evidence services return the underlying biological signals so the analyst can weigh them. Readings that
  would require inventing an unstated cutoff are rejected rather than calibrated here.
- **Re-arguing settled parts of the standard.** Where the framework is internally consistent, the library follows it
  even when the rule is strict. The conflicts collected in
  [Where the standard contradicts itself](#where-the-standard-contradicts-itself) are limited to internal contradictions
  in the pilot text or calculator that alter a variant's score or class.

## Design

### What this repository carries of the standard

An open-source implementation of SVCv4 needs the specification's machine-readable vocabulary and numbers: evidence code
names, point values, thresholds, caps, and matrix multipliers. [`themis/svcv4/data/`](../../themis/svcv4/data) records
our transcription of those values as typed Python constants, organised into one module per framework area and annotated
value-by-value with the supplement and line number each figure comes from. The repository does not copy the prose of the
Supplementary Materials. Those documents remain in the team's private pilot corpus checkout, and the SM*n* citations in
this document and in [`themis/svcv4/data/`](../../themis/svcv4/data) resolve against a pinned revision of that corpus.
Pinning the corpus revision ensures that line-number citations never drift silently; updating to a newer draft means
re-reading the cited passages against the new revision.

The codebase verifies this transcription through static structure checks, live oracle diffs, and provenance notes on
image-derived tables:

- **Typed structure and import-time invariants.** Every transcribed value is a typed literal checked against the
  dataclasses in [`themis/svcv4/reference.py`](../../themis/svcv4/reference.py). Invariants that static types cannot
  express—such as classification bands tiling the real number line without gaps, gene-disease validity gate rows
  matching the proto enum levels, and matrix axes matching the enum values callers select by—run automatically when
  [`themis/svcv4/reference.py`](../../themis/svcv4/reference.py) imports the data modules.
- **Oracle comparison against the ClinGen Pilot Calculator.** The calculator is a separate artefact maintained by Baylor
  College of Medicine, and its code is never vendored or quoted in this repository. Instead,
  [`tools/svcv4-oracle/`](../../tools/svcv4-oracle/README.md) fetches the calculator's JavaScript scoring bundle at run
  time, evaluates it in memory, and diffs every cap and decision-tree path against
  [`themis/svcv4/data/`](../../themis/svcv4/data). Where a supplement and the calculator disagree,
  [`themis.svcv4`](../../themis/svcv4) follows the supplement and pins the divergence in
  [`tools/svcv4-oracle/`](../../tools/svcv4-oracle/README.md) so the difference remains machine-checked.
- **Provenance on image-only tables.** Several values in the pilot appear only inside raster images, including SM3's
  DAFT binning grids, SM5's co-segregation figure, and SM20's functional assay control-count grids. For each of these,
  the transcription in [`themis/svcv4/data/`](../../themis/svcv4/data) records which image file was read, at what
  resolution, on what date, and whether every cell was legible.

The pilot release requests that analyses performed under the draft framework be shared only at abstract level until the
final manuscript is published. That restriction governs published variant analyses rather than open-source scoring
software, and this repository publishes no clinical analyses.

### When an input stays open

During a classification run, the model can encounter a decision node that the available evidence does not settle. An
obvious fallback would be to pick the most conservative candidate value automatically, or to pass candidate values into
a library helper that returns the lowest resulting classification band.

That fallback breaks on both clinical and structural grounds. Clinically, SVCv4 states no general conservatism rule;
several supplements explicitly resolve ambiguity *toward* stronger pathogenic evidence, including SM6 on missense vs.
splice path selection, SM9 on exon deletions, and SM13 on duplications. Structurally, a code helper in
[`themis/svcv4/classify.py`](../../themis/svcv4/classify.py) that collapsed competing readings into a single class would
embed clinical judgement in code: it could not represent an unresolved splice branch colour or the two separate
exon-relevance calls in a missense workflow, it would discard the `VUS` sub-band (`VUS-high` vs. `VUS-low`), and it
could not order the gene-disease validity gate's terminal outcomes (`Variant in Gene of Uncertain Significance` and
`Variant Not Applicable to This Disease`), which are not pathogenicity bands at all.

Instead, the agent and [`themis.svcv4`](../../themis/svcv4) resolve unsettled inputs in the following order of
precedence:

1. **Apply the framework's own disposition when one exists.** In these cases the input is settled by rule: SM18 assigns
   `Uncertain` (×0) to an unknown or unassessed molecular mechanism, SM3 instructs the analyst to err toward the higher
   DAFT bin when prevalence or penetrance is uncertain, SM20 scores a non-concordant functional assay at `0.0`, and SM3
   leaves a below-floor `POP_HMZ` observation count unscored (`None`).
1. **Apply SM18's admission filter before treating an exon-relevance call as open.** When the transcript inventory and
   expression data answer SM18's own admission criteria
   ([Exon relevance: what the tiers admit](#exon-relevance-what-the-tiers-admit)), that reading settles the tier.
   Falling back to the weaker tier is reserved for cases where the evidence genuinely does not separate `All` from
   `Most`, or where an analyst explicitly declines to judge.
1. **Fetch missing evidence before declaring an input open.** An input that is unknown only because a service call was
   skipped is incomplete work, not an open clinical question.
1. **Report remaining class-determinative ambiguity as no single class.** When competing readings survive the steps
   above and lead to different classification bands, the run reports no single class and outputs the point tally under
   each reading so a curator can see exactly which judgement call separates them.

### Frequency: which figure, and what disqualifies one

To score `POP_FRQ` in [`themis/svcv4/frequency.py`](../../themis/svcv4/frequency.py), the system must choose which
gnomAD allele frequency statistic to compare against the disease-allele frequency threshold (DAFT). Querying only gnomAD
exomes or only gnomAD genomes would miss variants called exclusively in the other callset; for example, variant
`17-4932533-G-T` has zero allele count in gnomAD exomes, a clean call in genomes, and a joint allele count of one
([Appendix A](#appendix-a-measurements-behind-the-readings)). Following ClinGen's VCEP Review Committee gnomAD v4
guidance (v3.0, June 2025), the `POP_FRQ` input is the **joint** (exome plus genome) Grpmax filtering allele frequency
(FAF), which takes the maximum 95% confidence lower bound across gnomAD's genetic-ancestry groups. Unless a VCEP
specification overrides it, this joint Grpmax FAF is used on **both** sides of the `POP_FRQ` comparison: for the
assessed variant's own frequency, and for every known pathogenic variant entering a pathogenic-variants DAFT.

Because gnomAD computes the joint frequency across exome and genome callsets without requiring the site to pass variant
quality control in both, the joint block can mask per-dataset QC failures. For instance, variant `17-4932566-A-T`
reports a joint Grpmax FAF of `1.81e-05` with only a frequency-discrepancy flag on the joint record, even though it
fails the variant-QC filter in both the exome (`3.1e-07`) and genome (`1.76e-04`) datasets
([Appendix A](#appendix-a-measurements-behind-the-readings)). The evidence layer therefore evaluates the variant-QC
filter per dataset.

> **Filter-failing datasets count against rarity, never toward benignity.** As VCEP specifications state verbatim, a
> variant whose underlying dataset fails QC cannot establish that the allele is common enough to be benign, nor can it
> establish that the allele is genuinely absent or rare. In
> [`themis/svcv4/frequency.py`](../../themis/svcv4/frequency.py), a filter-failing FAF cannot anchor a DAFT and yields
> no `POP_FRQ` determination (`points=None`). Scoring it as `0.0` would assert that the variant is rarer than its
> threshold, which would falsely satisfy the rarity precondition that SM4 requires before awarding clinical case points
> (`CLN_*`). By contrast, gnomAD's descriptive caveat flags (such as low-complexity region or segmental duplication
> warnings) are not QC filter failures and are passed through unscored for the analyst to weigh.

#### The DAFT method order is SM3's, and it binds strictly

SM3 lists four ways to obtain a DAFT:

1. A curated VCEP or community-established threshold.
1. The Whiffin/Ware calculator method (from disease prevalence, allelic heterogeneity, locus heterogeneity, and
   penetrance).
1. The prevalence/penetrance binning tables (SM3 Tables 1–6).
1. The known-pathogenic-variants method (the maximum gnomAD FAF across at least ten known pathogenic variants in the
   gene).

SM3 states that this ordering holds "generally" and that the pathogenic-variants method should be used only when the
earlier methods are "not appropriate". If the ordering were treated as a loose preference, an analyst or agent could
compute both the calculator DAFT and the pathogenic-variants DAFT and pick whichever produced the preferred score. That
choice changes classifications: on one gene with complete calculator parameters (prevalence `1/4000`, allelic
heterogeneity `0.10`, locus heterogeneity `0.60`, penetrance `0.80`), the calculator method yields a DAFT of `9.375e-6`
and `POP_FRQ = -6.0`, whereas the pathogenic-variants method yields a DAFT of `6.51e-5` and `POP_FRQ = -3.0`—a
three-point swing on method choice alone ([Appendix A](#appendix-a-measurements-behind-the-readings)). Because SM3 omits
the algebraic formula for the calculator method, [`themis/svcv4/frequency.py`](../../themis/svcv4/frequency.py) recovers
the exact expression from the underlying Whiffin/Ware paper and the pilot calculator
([Appendix C](#appendix-c-two-derivations-worked)).

We therefore enforce SM3's hierarchy as a **strict** ordering where the analyst must affirm or reject each method's
preconditions *before* computing a threshold. No code helper automatically falls back between methods, because whether
parameter estimates are "appropriate" is a clinical judgement about the disease literature rather than a check on
whether numeric arguments were supplied. While SM3 permits using the pathogenic-variants method as an offline sanity
check, the runtime interface never computes two methods side by side, preventing the agent from reasoning backwards from
the resulting points.

#### The review-status floor for a pathogenic-variants DAFT is an input, not a convention

When the earlier DAFT methods are inapplicable and the analyst reaches the pathogenic-variants method,
[`themis/svcv4/frequency.py`](../../themis/svcv4/frequency.py) derives the threshold from the gene's known pathogenic
variants in ClinVar. That derivation depends on the minimum ClinVar review status (star rating) required to admit a
record into the pool. Across all 206 ClinGen CSpec VCEP specifications, not a single specification pins a star-rating
floor on a frequency comparator: one panel sets a 2-star floor on a different comparator, 31 constrain the comparator by
submission provenance instead, and 89 leave "known pathogenic" undefined
([Appendix A](#appendix-a-measurements-behind-the-readings)). At the same time, admitting 0-star records can rest an
entire gene's threshold on a single unreviewed submission; on the gene measured in
[Appendix A](#appendix-a-measurements-behind-the-readings), dropping a single 0-star record moves `POP_FRQ` from `-3.0`
to `-6.0`.

Because no clinical convention exists in the registry, [`themis/svcv4/frequency.py`](../../themis/svcv4/frequency.py)
requires the minimum review status as an explicit caller argument with no default value and stamps the chosen floor on
the returned result (`KNOWN_PATHOGENIC_REVIEW_STATUS_FLOOR = 1`, one star / criteria provided, is the policy constant
callers pass unless a VCEP specifies otherwise). That single review-status floor governs **both** the ten-variant
minimum pool size precondition and the variant that sets the maximum frequency, ensuring the count and the maximum are
drawn from the same population.

Because a pathogenic-variants DAFT that is set too low awards false benign points (`POP_FRQ < 0`), two additional
safeguards apply to the pool:

- **No post-hoc review of 0-star records when the pool falls below ten.** SM3 suggests that when a gene has fewer than
  ten qualifying pathogenic variants, an analyst might inspect 0-star records individually to top up the pool. We do not
  adopt that suggestion, because deciding whether to admit 0-star records after seeing that the 1-star pool falls short
  re-introduces the choose-after-seeing-the-answer defect.
- **Truncated pools or missing frequencies cannot anchor a threshold.** If a ClinVar query returns a truncated list of
  variants ordered by anything other than allele frequency, taking the maximum over that prefix produces only a lower
  bound on the true maximum. An artificially low DAFT would make the assessed variant look common relative to the
  threshold and award benign points that the full pool would not support. Therefore, the evidence layer carries pool
  truncation as explicit state rather than a prose note, and any pool member whose gnomAD frequency has not been fetched
  counts toward neither the ten-variant precondition nor the maximum.

Independent of star rating, any ClinVar record with conflicting classifications is excluded by the classification gate
described next.

### Which ClinVar classifications count as a pathogenic assertion

ClinVar's aggregate germline classification is a structured phrase rather than a closed enum, and two different parts of
SVCv4 read the same ClinVar records for different purposes: SM19's informative-variant rules (`*_INF`) and SM18's
gene-level pathogenic-variant density ask whether a variant is disease-causing, whereas SM3's pathogenic-variants DAFT
asks for the highest population frequency compatible with a Mendelian pathogenic allele. A naive substring check for
`"athogenic"` fails immediately because it admits `"Conflicting classifications of pathogenicity"`, which on one gene
inflated the DAFT ninefold by anchoring it on a synonymous variant
([Appendix A](#appendix-a-measurements-behind-the-readings)).

[`themis/svcv4/clinvar_classification.py`](../../themis/svcv4/clinvar_classification.py) therefore parses the aggregate
classification string using its punctuation grammar and exposes two distinct gates: a wide gate (`is_pathogenic`) for
SM19 and SM18, and a narrow gate (`is_unqualified_pathogenic`) for SM3's DAFT.

```
ClinVar Aggregate Classification String
  │
  ├─ Split on ";" ──► Leading ACMG part           Trailing non-ACMG part(s)
  │                   (e.g. "Pathogenic/Likely    (e.g. "risk factor",
  │                    pathogenic")                "drug response")
  │                        │
  │                        ▼
  │                   Split on "/" ──► Every "/" token must be in the gate's allowed set
  │
  ├──► Wide gate (SM19 *_INF, SM18 density):
  │      Allows unqualified ("Pathogenic", "Likely pathogenic") AND
  │      penetrance/risk terms ("Pathogenic, low penetrance",
  │      "Likely pathogenic, low penetrance", "Established risk allele",
  │      "Likely risk allele"). Ignores ";" tails.
  │
  └──► Narrow gate (SM3 pathogenic-variants DAFT):
         Allows ONLY unqualified ("Pathogenic", "Likely pathogenic")
         with NO ";" tail at all.
```

In ClinVar's punctuation grammar, `/` and `;` play different roles:

- **`/` joins co-equal ACMG classification terms** that the aggregate sits between, such as
  `"Pathogenic/Likely pathogenic"`. Because every token around a `/` is an ACMG pathogenicity assertion, every token
  must belong to the gate's allowed set for the record to qualify.
- **`;` appends a non-ACMG secondary assertion**, such as `"Pathogenic; risk factor"` or `"Pathogenic; drug response"`.
  For SM19 and SM18, a trailing `;` clause does not negate the leading ACMG pathogenicity assertion.
- **Conflicting and empty classifications never qualify.** `"Conflicting classifications of pathogenicity"` is rejected
  at every star rating. An empty classification string is also rejected as a normal non-pathogenic outcome rather than a
  parsing error, because ClinVar's evidence-only submissions routinely assert clinical or functional observations
  without a germline classification.

The wide and narrow gates diverge on reduced-penetrance and risk-allele terms. Across ClinVar, 340 records carry a
penetrance or risk qualifier spelled in 19 different ways, and 283 of those carry no unqualified pathogenic term
([Appendix A](#appendix-a-measurements-behind-the-readings)). ClinVar formats these as distinct classification tokens:
`"Pathogenic, low penetrance"`, `"Likely pathogenic, low penetrance"`, `"Established risk allele"`, and
`"Likely risk allele"` (whereas `"Uncertain risk allele"` asserts uncertainty and is rejected by both gates).

For SM3's pathogenic-variants DAFT, admitting a reduced-penetrance or risk allele—or a record carrying a `; risk factor`
tail—breaks the frequency model: an incomplete-penetrance risk allele can circulate at a population frequency orders of
magnitude higher than a fully penetrant Mendelian allele. On one gene in
[Appendix A](#appendix-a-measurements-behind-the-readings), a single 2-star risk-factor allele present at ~1% in
Europeans lifts the gene's DAFT two orders of magnitude above its Mendelian threshold. The narrow gate
(`is_unqualified_pathogenic`) therefore requires every `/`-separated token to be unqualified `"Pathogenic"` or
`"Likely pathogenic"` and rejects any record with a `;` tail.

By contrast, SM19's informative-variant rules (`*_INF`) and SM18's pathogenic-variant density ask whether a variant is a
known disease-causing change at that residue or in that gene, regardless of its population frequency. A
reduced-penetrance pathogenic allele is valid evidence that the residue does not tolerate the change and that the gene
harbours pathogenic variation (widening the density pool on two complement genes increased their counts from 156 to 248
and from 107 to 151 records; [Appendix A](#appendix-a-measurements-behind-the-readings)). Both SM19 and SM18 therefore
use the wide gate (`is_pathogenic`).

Finally, when the queried variant itself carries an expert-panel consensus classification in ClinVar, the analyst may
adopt that consensus classification—following its cited PMIDs into the primary literature—unless patient-specific
context makes it inapplicable. What licenses adoption is the review-status phrase naming the expert panel tier
(`reviewed by expert panel` or `practice guideline`), rather than the numeric star count alone.

### In-silico prediction: one predictor, chosen in advance

To score `MIS_PRD` for a missense variant, an obvious temptation when multiple calibrated predictors are available is to
inspect several scores and pick the strongest or take a majority vote. SM6 forbids this: because missense metapredictors
are heavily correlated, evaluating multiple predictors on a variant and choosing after seeing their scores is multiple
testing. At the same time, SM6 distinguishes per-variant cherry-picking from per-gene policy: it encourages laboratories
to select distinct calibrated predictors for specific genes where the default predictor performs poorly, provided the
choice is fixed before evaluating any variant under assessment, and it permits downgrading a predictor's evidence
strength on a gene where it is known to over-call until a gene-specific calibration is published.

[`themis/svcv4/predictor_policy.py`](../../themis/svcv4/predictor_policy.py) and
[`themis/svcv4/data/predictor_policy.json`](../../themis/svcv4/data/predictor_policy.json) enforce this by fixing the
predictor choice in a static, curator-owned policy table keyed by stable HGNC ID (so gene symbol reassignments cannot
silently rebind a rule to a different gene). The policy specifies a global default predictor, per-gene predictor
overrides, and per-gene downgrade flags. While a frontier model can help survey the calibration literature when curators
update the policy, the frozen policy governs every classification run.

In the current policy ([`themis/svcv4/data/predictor_policy.json`](../../themis/svcv4/data/predictor_policy.json)), the
default predictor is **BayesDel** (no-allele-frequency build), with a per-gene override to **AlphaMissense** for `PKD1`
(`HGNC:9008`). The comparative evidence supporting both entries is detailed in
[Appendix B](#appendix-b-the-predictor-policy-evidence). Because the policy binds one predictor per gene, requesting a
second predictor's score from the annotation service is asking about a different SVCv4 line, never a second opinion on
`MIS_PRD`.

### Mechanism: a rubric an analyst scores, not a field to retrieve

For loss-of-function and splice workflows, SM18 scales positive predictive points by the entity's molecular mechanism
level (`Established` ×1.0, `Likely` ×0.5, `Suspected` ×0.25, `Uncertain` ×0) and imports that four-tier scale from the
GenCC Mechanism Curation Working Group's
[*Recommendation for Loss of Function Mechanism Curation*](https://clinicalgenome.org/site/assets/files/10698/draft_lof_framework_position_statement.pdf)
(v1.0, September 2025, draft; transcribed in
[`themis/svcv4/data/gencc-lof-mechanism-framework.md`](../../themis/svcv4/data/gencc-lof-mechanism-framework.md)). Under
that rubric, an analyst totals weighted evidence lines into a mechanism score that maps onto `Established` (≥0.99),
`Likely` (0.90–0.98), `Suspected` (0.50–0.89), or `Uncertain` (≤0.49), subject to the precondition that the entity's
gene-disease validity is `Moderate` or higher.

An obvious implementation would try to look up the pre-computed GenCC mechanism tier from a public database feed. That
fails because **no database publishes per-gene curations on the GenCC loss-of-function mechanism scale for any gene**.
Consequently, SM18's manual fallback—having an analyst score the rubric from the available genetic and functional
evidence—is the normal path for every classification. When a run cites the mechanism framework, it cites the GenCC
rubric in
[`themis/svcv4/data/gencc-lof-mechanism-framework.md`](../../themis/svcv4/data/gencc-lof-mechanism-framework.md) rather
than a retrieved database field, and the agent scores the rubric by reading the gene-scoped curated signals from
`GeneDisease.DescribeGene` alongside the literature evidence layer.

The GenCC framework defines only one machine-readable shortcut: a ClinGen **dosage-sensitivity haploinsufficiency (HI)
score of `3`** ("Sufficient evidence for dosage pathogenicity") establishes an `Established` loss-of-function mechanism
for a *monoallelic* disease entity. Lower or special HI scores (`0`, `1`, `2`, `30` for autosomal recessive phenotype,
or `40` for dosage sensitivity unlikely) do not map to `Likely`, `Suspected`, or `Uncertain`; they simply mean the HI
shortcut does not apply and the analyst must score the GenCC rubric directly. Furthermore, ClinGen curates HI scores at
the **gene** level rather than per disease entity. On a gene that harbours both a loss-of-function disease entity and a
gain-of-function allelic disorder (SM21), applying the gene-level HI=3 shortcut to the gain-of-function entity would
award a ×1.0 loss-of-function multiplier to the wrong disease. The analyst must therefore verify that the assessed MDE
is the monoallelic loss-of-function entity that the HI curation describes.

> **`Uncertain` (×0) is the floor when evidence cannot support a mechanism call, never an automatic default when a
> haploinsufficiency score is absent.** Automatically defaulting every gene without an HI=3 curation to `Uncertain`
> would discard all of the variant-density, null-enrichment, and functional evidence that the GenCC rubric evaluates.
> When the rubric evidence genuinely lands at `Uncertain`, a zeroed multiplier is a valid, transparent outcome
> ([`../PRODUCT.md`](../PRODUCT.md) §6). Conversely, if the caller claims a mechanism above `Uncertain` while the MDE's
> gene-disease validity is below `Moderate`, [`themis/svcv4/classify.py`](../../themis/svcv4/classify.py) raises a
> `ValueError` so the model resolves the contradiction rather than silently coercing the mechanism downward.

### Exon relevance: what the tiers admit

On paths scaled by the mechanism-by-exon-relevance matrix, SM18 defines three exon-relevance tiers—`All`, `Most`, and
`Few`—using two limbs: which of the gene's transcripts include the assessed exon (the **membership limb**) and the
relative expression of exon-including transcripts versus exon-omitting transcripts across tissues (the **abundance
limb**). While transcript membership is a deterministic interval check on genomic coordinates, the abundance limb is an
irreducible clinical judgement: SM18 does not assign numeric cutoffs to "functionally equivalent", "reasonably well
expressed", or the boundary between "most but not all" and "not in clinically relevant transcripts". Consequently, the
evidence service never returns an exon-relevance tier as if the standard had computed it; the analyst states the tier as
a judgement over an explicit transcript denominator, naming any expression threshold used.

> **gnomAD pext cannot by itself establish `All`.** gnomAD's proportion expressed across transcripts (pext) can show
> that an exon has no drop in relative expression across tissues, which argues against `Few`, but it cannot distinguish
> `All` from `Most`—the distinction that doubles positive predictive points from ×0.5 to ×1.0. Reducing pext to a
> boolean "dip / no-dip" flag or a single gene-wide scalar would either systematically bias scores upward toward `All`
> or hide tissue-specific splicing. The evidence service therefore returns the per-exon, per-tissue pext and GTEx
> profile for the analyst to read alongside the transcript inventory.

When reading SM18's transcript membership and admission rules, each of the following misreadings errs by shifting
classifications across bands:

- **An empty "spans-but-skips" transcript group does not establish `All`.** A transcript can omit an exon either by
  splicing around it (its genomic span covers the exon, but the exon is skipped in the mature transcript) or by starting
  downstream or terminating upstream of the exon. On one gene in
  [Appendix A](#appendix-a-measurements-behind-the-readings), a curated RefSeq transcript stops short of the assessed
  exon; treating an empty "spans-but-skips" list as proof that no transcript omits the exon would falsely force `All`
  without evaluating whether the shorter isoform is biologically relevant. As SM18 states explicitly, the analyst must
  evaluate the overall abundance of transcripts including or omitting the exon, not merely the presence or absence of
  specific spliced isoforms.
- **SM18's transcript admission filter applies before evaluating either `All` or `Most`.** Inside the definition of
  `All`, SM18 excludes transcripts that are non-coding, subject to nonsense-mediated decay (NMD), or otherwise
  non-productive, and defines `Most` over that same admitted set of coding transcripts. Both the membership limb and the
  abundance limb operate over this filtered inventory. When no admitted coding transcript omits the exon, the analyst
  still evaluates the abundance limb over the admitted denominator and records the call as an explicit judgement rather
  than claiming the standard automatically forced `All`.
- **Evaluating `All` vs. `Most` over the unfiltered transcript inventory inverts SM18's exclusion filter.** On one gene
  in [Appendix A](#appendix-a-measurements-behind-the-readings), all 49 coding transcripts across Ensembl and RefSeq
  include the assessed exon and the only transcripts omitting it are non-coding lncRNA records; running the membership
  test before filtering out non-coding transcripts drops a nonsense variant from `+6.0` (`Likely Pathogenic`) to `+3.0`
  (`VUS-mid`) solely because of a non-coding transcript.

Because SM18 leaves open which transcript annotation set defines "all transcripts" and because RefSeq and Ensembl have
very different curation policies—RefSeq frequently publishes an order of magnitude more "transcript variant" records for
a gene than Ensembl—**callers must never compute a raw ratio of transcript counts across an unfiltered annotation
dump**. Instead, the evidence service reports Ensembl and RefSeq transcript inventories in their own separate namespaces
so the analyst can state the admitted denominator explicitly.

### Functional assays: which deposit speaks, and about what

[`themis/services/evidence/upstreams/mavedb.py`](../../themis/services/evidence/upstreams/mavedb.py) retrieves and
selects high-throughput functional assay deposits from MaveDB, and
[`themis/svcv4/functional.py`](../../themis/svcv4/functional.py) scores them (`*_FXN`, SM20). Because multiple MaveDB
score sets can cover the same gene or variant and reach different conclusions, selecting which deposit and calibration
to read is an interpretive decision rather than a database sort order:

- **Direct allele matches rank ahead of derived protein-consequence matches, before considering publication date.** A
  MaveDB deposit may map its scores to nucleotide changes (cDNA/genomic HGVS) or to protein amino-acid substitutions.
  When a caller queries a specific nucleotide change, a score set matched directly on that nucleotide allele speaks
  about the exact change requested, whereas a score set matched via the predicted amino-acid consequence speaks only
  about the protein substitution (ignoring any splice effect of the nucleotide change). Ranking directness ahead of
  publication date prevents a newer protein-level saturation screen from overwriting an older nucleotide-level screen
  (such as saturation genome editing) that assayed the exact genomic allele.
- **Depositor-calibrated OddsPath bins rank ahead of raw scores, and a primary marker restricts which calibrations are
  read.** [`themis/services/evidence/upstreams/mavedb.py`](../../themis/services/evidence/upstreams/mavedb.py) ranks a
  calibrated deposit ahead of an uncalibrated one, and when a deposit includes an OddsPath calibration that maps assay
  scores onto SVCv4 evidence strengths, [`themis/svcv4/functional.py`](../../themis/svcv4/functional.py) scores that
  calibration rather than the uncalibrated score. Where a deposit includes multiple calibrations and marks one as
  primary, [`themis/services/evidence/upstreams/mavedb.py`](../../themis/services/evidence/upstreams/mavedb.py) reads
  only the primary calibration; where multiple calibrations are marked primary, there is no ground for choosing and the
  upstream adapter refuses the score set rather than guessing; where none is marked primary, all of the deposit's
  calibrations are read in URN order. Reading non-primary calibrations when a primary is marked, or relying on raw JSON
  array order, manufactures false evidence: on one `TP53` truncating variant
  ([Appendix C](#appendix-c-two-derivations-worked)), non-primary calibrations in one deposit yielded a `Supporting`
  benign functional call (`-1.0`) where the depositor's primary calibration placed the variant in the indeterminate
  range (`0.0`), while honouring the primary marker allowed the coding and protein matches to agree on a `Strong`
  pathogenic deposit (`OddsPath = 50.2`). Similarly, on `BRCA1`, reading calibrations in array order shifted a variant's
  `OddsPath` from `0.0383` to `0.078` purely on serialisation order ([Appendix C](#appendix-c-two-derivations-worked)).
- **An assay only scores points when its readout matches the MDE's disease mechanism.** Two functional assays on the
  same residue can produce opposite numeric results without contradicting each other biologically because they measure
  different molecular functions. For example, for the same `TP53` residue, one deposit is a saturation genome editing
  screen over exons 5–8 measuring loss of function, whereas another is a reporter-cell enrichment screen measuring
  dominant-negative effect. A truncation at that residue removes the oligomerisation domain, so it causes loss of
  function without acting as a dominant-negative monomer, and it scores abnormal in the first assay and normal in the
  second. Under SM20, the analyst must check whether the assay measures the function relevant to the assessed MDE's
  mechanism; an assay that measures a non-concordant mechanism scores `0.0`.

### Clinical and locus observations: the categorical readings

The seven clinical and locus evidence codes (`CLN_aff`, `CLN_den`, `CLN_uaf`, `CLN_cis`, `CLN_trn`, `LOC_phe`,
`LOC_seg`) are arithmetic functions *downstream* of categorical clinical readings made by the analyst: the proband's
phenotype specificity tier, whether SM4's caveat exclusions hold, whether parentage was confirmed for a de novo call,
each relative's genotype and affected status, the disease penetrance band, the classification of an in-trans variant,
and the diagnostic yield of testing. [`themis/svcv4/observations.py`](../../themis/svcv4/observations.py) computes the
points from those categorical inputs so the analyst never performs table lookups or arithmetic by hand.

The helpers in [`themis/svcv4/observations.py`](../../themis/svcv4/observations.py) structure their categorical inputs
to prevent common reading errors in SM4 and SM18:

- **SM4's affected-proband sub-row requires three separate boolean affirmations.** In SM4's affected-proband table, the
  top sub-row awards full points only when the negation of three caveats holds simultaneously (an `AND` across all
  three), whereas the lower sub-rows apply when one or more caveats are present (an `OR`). Asking an analyst for a
  single combined boolean ("do any caveats apply?") invites logic errors on negation; the helper therefore requires the
  analyst to affirm each condition separately.
- **Phenotype specificity takes an integer numerator and cohort denominator, never a pre-rounded percentage.** SM4 bins
  phenotype specificity by the diagnostic yield or mutation detection rate in a defined patient cohort. Requiring the
  integer count and cohort size keeps the sample size and bin-boundary gaps visible, whereas passing a rounded
  percentage hides whether a rate rests on 2 of 3 cases or 200 of 300.
- **Silence in a clinical source does not establish exclusion of a caveat.** If an analyst treats a paper's silence
  about alternative genetic causes or consanguinity as proof that those caveats were excluded, every incomplete case
  report automatically qualifies for SM4's highest-scoring sub-row. Instead, the analyst must evaluate SM4's sub-rows in
  order: check whether the source establishes or leaves open the lower sub-rows' caveats before affirming the top
  exclusion sub-row. We likewise reject an invented rule that would "spend" a caveat once to lower the phenotype tier
  and then ignore the same caveat when selecting the sub-row; SM4 states no such rule, and applying it would silently
  upgrade scores whenever a tier rationale mentions a caveat.

Alongside those helpers, precondition checks in [`themis/svcv4/classify.py`](../../themis/svcv4/classify.py) enforce
SM4's frequency gate across all clinical codes: SM4's affected-proband code may only award positive points at the two
rare `POP_FRQ` assignments (`0.0` or `+0.5`), and no frequency-conditioned clinical code may award points when `POP_FRQ`
is outside that gate, omitted from the tally, or undetermined (`None`).

By contrast, several clinical and locus questions remain deliberately unsettled by code helpers because encoding a
single rule would hide an unresolved ambiguity in the standard or the primary evidence:

- **SM5's non-segregation branch**, where SM5 contradicts itself across its text, range header, and figure
  ([Co-segregation](#co-segregation)).
- **Interaction between SM18's exon-relevance waiver and SM19's informative variants (`*_INF`)**, where the standard
  does not state whether the same pathogenic variants used to waive exon-relevance scaling on `MIS_PRD` (SM18 §17) may
  also be counted again for `*_INF` points.
- **Specific versus consistent phenotype**, which often turns on whether a distinguishing clinical examination was
  performed and negative or simply never performed—a distinction that a binary input would collapse by treating an
  unperformed exam as negative.
- **Whether the per-individual benign clinical codes (`CLN_uaf`, `CLN_cis`) carry an unstated code-level floor**,
  discussed in
  [De novo observations, and the two per-individual clinical codes](#de-novo-observations-and-the-two-per-individual-clinical-codes).

### X-linked entities: dominant and recessive

An MDE's mode of inheritance in
[`schema/proto/themis/svcv4/models/svcv4.proto`](../../schema/proto/themis/svcv4/models/svcv4.proto)
(`ModeOfInheritance`) takes one of five values:

- Autosomal dominant (`MODE_OF_INHERITANCE_AUTOSOMAL_DOMINANT`)
- Autosomal recessive (`MODE_OF_INHERITANCE_AUTOSOMAL_RECESSIVE`)
- Semidominant (`MODE_OF_INHERITANCE_SEMIDOMINANT`)
- X-linked dominant (`MODE_OF_INHERITANCE_X_LINKED_DOMINANT`)
- X-linked recessive (`MODE_OF_INHERITANCE_X_LINKED_RECESSIVE`)

This five-mode vocabulary matches ClinGen's own [`svcv4-model`](https://github.com/clingen-data-model/svcv4-model)
schema and is used everywhere in Themis: when registering an entity for curation, when routing inside
[`themis.svcv4`](../../themis/svcv4), and when recording a classification run. External curated databases (such as
GenCC, OMIM, and PanelApp) use their own mode strings, and the evidence services return those strings verbatim rather
than mapping them in code, because deciding which curated disease record matches the assessed MDE belongs to the analyst
([`evidence-interfaces.md`](evidence-interfaces.md#goal-is-the-gene-disease-relationship-valid-and-by-what-mechanism)).

An obvious simplification would collapse X-linked inheritance into a single `X-linked` value. ClinGen's gene-disease
validity curations often record unqualified "X-linked", and the pilot calculator's workflow headings print
`"Autosomal Recessive / X-linked"` as a single combined branch. However, collapsing X-linked inheritance breaks on SM3
and SM4, which route X-linked dominant and X-linked recessive entities to different scoring tables:

| Framework area                                                    | X-linked dominant (`XLD`)                                                                         | X-linked recessive (`XLR`)                                                                                    |
| :---------------------------------------------------------------- | :------------------------------------------------------------------------------------------------ | :------------------------------------------------------------------------------------------------------------ |
| **`POP_FRQ` binning (`SM3` §34)**                                 | Male prevalence → Table 3<br>Female prevalence → **Table 4**<br>Combined prevalence → **Table 6** | Male prevalence → Table 3<br>Female prevalence → **Table 5**<br>Combined prevalence → **No table (unbinned)** |
| **Affected proband (`SM4` §75)**                                  | XY proband → Monoallelic table<br>XX proband → **Monoallelic table**                              | XY proband → Monoallelic table<br>XX proband → **Biallelic table** (depends on *in trans* variant)            |
| **Population homozygotes/hemizygotes (`POP_HMZ`, `SM3` Table 7)** | Recessive / X-linked table                                                                        | Recessive / X-linked table                                                                                    |
| **Unaffected observations (`CLN_UAF`, `SM4` Table 5)**            | Recessive / X-linked column                                                                       | Recessive / X-linked column                                                                                   |
| **Co-segregation (`LOC_SEG`, `SM5` Figure)**                      | Single X-linked row block                                                                         | Single X-linked row block                                                                                     |

The two places where the split changes points are:

- **Population frequency binning (`SM3` §34).** SM3 states that the Whiffin/Ware calculator method should not be used
  for X-linked MDEs and directs the analyst to the binning tables instead. Which binning table applies depends on both
  the sex of the prevalence estimate and whether the X-linked MDE is dominant or recessive: a male prevalence estimate
  uses Table 3 for both; a female prevalence estimate uses Table 4 for X-linked dominant and Table 5 for X-linked
  recessive; and a combined-sex prevalence estimate uses Table 6, which SM3 provides *only* for X-linked dominant MDEs.
  An X-linked recessive MDE with only a combined-sex prevalence estimate has no binning table in SM3 at all.
- **Affected XX probands (`SM4` §75).** While SM4 scores an affected XY (hemizygous) proband from the monoallelic
  disorder table for all X-linked MDEs, it splits on affected XX probands: under an X-linked dominant MDE, an affected
  XX proband is scored from the monoallelic table, whereas under an X-linked recessive MDE she is scored from the
  biallelic table, where points depend on whether a second variant is present *in trans* and how that second variant is
  classified. A single heterozygous affected female therefore lands in different tables and receives different points
  depending solely on whether the X-linked entity is dominant or recessive.

Every other code in the framework (`POP_HMZ`, `CLN_UAF`, and `LOC_SEG`) treats X-linked dominant and X-linked recessive
identically. Even so, every X-linked MDE must declare `X_LINKED_DOMINANT` or `X_LINKED_RECESSIVE` up front, because
registration cannot predict which evidence codes a variant in that entity will trigger. SM4 notes that the clinical
genetics community is moving away from dividing X-linked disorders into dominant and recessive and that a future
revision will refine this model; until then, [`themis.svcv4`](../../themis/svcv4) implements the split written in the
pilot text.

**When a worked practice example in the supplements conflicts with SM3's rule, we follow the rule.** One of the pilot's
worked practice variants sits in an X-linked recessive MDE, yet its worked frequency section looks up SM3 Table 6—the
combined-prevalence table restricted to X-linked dominant MDEs. Following the practice example would score a recessive
entity against a dominant threshold. In our reference evaluation set, we score that variant according to SM3's rule and
record the departure explicitly so reviewers can see why the reference answer differs from the pilot worksheet's
practice block.

### Criteria specifications: a VCEP profile is ACMG-2015

SM3 ranks a published VCEP or community frequency threshold first among its DAFT methods, so a panel's specification
takes precedence over generic computed thresholds for its gene. However, the ClinGen Criteria Specification (CSpec)
Registry currently serves **ACMG/AMP-2015** specifications (using codes such as `PM2_Supporting`, `BS1`, `BA1`, and
`PS3`), and no SVCv4 profile exists in the registry yet. Bridging CSpec entries into an SVCv4 run therefore requires two
boundaries:

- **CSpec evidence codes, strength labels, and combining rules are never auto-translated into SVCv4 points.** An
  ACMG-2015 criterion modification (such as downgrading `PM3` or modifying `PS3` assay requirements) is returned to the
  analyst as structured text to reason about when reading the corresponding SVCv4 supplement, never as a numeric value
  added into the SVCv4 point tally.
- **A VCEP frequency threshold enters SM3's ordering at the rung its own derivation earns, not at method 1 by virtue of
  who published it.** A registry entry stating a population-frequency threshold with no derivation anywhere in the
  registry is a finding to report, not a first-rung DAFT.

### Where the standard contradicts itself

While transcribing the supplements and cross-checking them against the ClinGen Pilot Calculator via
[`tools/svcv4-oracle/`](../../tools/svcv4-oracle/README.md), we identified several internal contradictions in the
standard that change variant scores or classifications. We submitted the full catalogue of inconsistencies to the
ClinGen SVCv4 working group in July 2026; that feedback document lives in the private pilot corpus checkout because it
quotes the draft supplements extensively, and anyone investigating a supplement edge case should read it in full first.
Two examples from that catalogue illustrate the need to verify every table cell: SM6's predictor calibration figure
prints one predictor-threshold cell out of monotonic order relative to its neighbours (where only one monotonic reading
is possible), and SM3 contains two unreconciled descriptions of the pathogenic-variants DAFT method that disagree on
which frequency statistic to read and on whether a pool smaller than ten means the method "should not be used" or
"should be reconsidered".

Across all such conflicts, our general rule is to **reproduce every class-determinative number from the normative text
of the supplements rather than trusting a summary header, worked example, or calculator bundle**. The subsections below
record the conflicts that directly affect [`themis.svcv4`](../../themis/svcv4).

#### Co-segregation

SM5 (`LOC_SEG`) contradicts itself in two places:

- **Negative points for non-segregation versus a non-negative range header.** One section of SM5 recommends awarding
  negative points (down to `-4.0`) when a variant fails to co-segregate with disease in affected family members across
  three inheritance contexts. However, SM5's title block and the immediately following section both state the range of
  `LOC_SEG` as `0.0` to `+4.0`. The ClinGen Pilot Calculator enforces the `0.0` floor, whereas
  [`themis/svcv4/data/`](../../themis/svcv4/data) and [`themis/svcv4/classify.py`](../../themis/svcv4/classify.py)
  follow SM5's prose recommendation and permit negative `LOC_SEG` scores down to `-4.0` (and likewise omit a `0.0` floor
  on the combined `LOC` concept cap so it does not undo a negative `LOC_SEG` score). This divergence is pinned in
  [`tools/svcv4-oracle/`](../../tools/svcv4-oracle/README.md) until SM5 is reconciled.
- **Unaffected non-carriers in autosomal recessive pedigrees.** In SM5's co-segregation figure, the autosomal-recessive
  row awards `+0.4` points per unaffected relative who is "heterozygous or wild type", which would award pathogenic
  co-segregation points for relatives who do not carry the assessed variant at all. By contrast, SM5 §28 restricts
  unaffected autosomal-recessive co-segregation scoring to heterozygous carriers. We follow §28's text: scoring
  wild-type non-carriers would treat the absence of the variant as evidence about the variant, and no other inheritance
  row in SM5's figure scores non-carriers.

#### De novo observations, and the two per-individual clinical codes

In SM4, de novo observations (`CLN_DNV`) are weighted per proband and summed across probands without stating a
code-level maximum. Whereas the ClinGen Pilot Calculator imposes an upper cap on `CLN_DNV` (and is the side expected to
move), [`themis/svcv4/data/`](../../themis/svcv4/data) follows SM4's text and applies no upper cap on `CLN_DNV`, pinning
the difference in [`tools/svcv4-oracle/`](../../tools/svcv4-oracle/README.md).

The two per-individual benign clinical codes—unaffected observations (`CLN_UAF`) and observations *in cis* with a known
pathogenic variant (`CLN_CIS`)—have the same structure in the negative direction: SM4 states a per-individual tariff for
each observation and specifies no code-level floor on the sum across individuals. Clamping either code to the magnitude
of its largest single-individual tariff would mistake a single-observation tariff for a multi-observation cap and
truncate legitimate benign evidence. Here both [`themis.svcv4`](../../themis/svcv4) and the pilot calculator leave the
negative sum uncapped.

#### Semidominant entities in `POP_HMZ` and `CLN_UAF`

For both `POP_HMZ` (population homozygotes/hemizygotes) and `CLN_UAF` (unaffected clinical observations), the standard
provides two scoring tables: a dominant table and a recessive/X-linked table. In the ClinGen Pilot Calculator, selecting
a semidominant mode of inheritance exposes *both* tables for `POP_HMZ` and *both* tables for `CLN_UAF`.

In the supplements, however, each code assigns semidominant entities to a single table:

- **SM3 Table 7 (`POP_HMZ`)** places semidominant disorders in the recessive and X-linked table, pricing homozygous
  observations in a semidominant MDE at the recessive/X-linked rate.
- **SM4 Table 5 (`CLN_UAF`)** places semidominant disorders in the same column as autosomal dominant disorders, pricing
  unaffected carriers at the dominant rate.

Following our rule that supplement tables take precedence over the calculator
([What this repository carries of the standard](#what-this-repository-carries-of-the-standard)),
[`themis.svcv4`](../../themis/svcv4) routes a semidominant MDE to the recessive/X-linked table for `POP_HMZ` and to the
dominant table for `CLN_UAF`. In the curator UI
([`svcv4-worksheet-transcription.md`](svcv4-worksheet-transcription.md)), the worksheet preserves the calculator's
verbatim table wording and footnotes while routing semidominant curations to the single supplement-designated table per
code.

#### The validity gate against a benign call

Under SM18, an MDE's curated gene-disease validity level caps the final classification (for example, `Limited` validity
caps a variant at `VUS`, and `Disputed` or `Refuted` maps to terminal non-disease outcomes). It remains open whether
SM18 intends the validity gate to block a `Benign` or `Likely Benign` call—for instance, a variant with strong
population frequency evidence (`POP_FRQ = -6.0`)—when the gene's validity is `Limited` or below. A literal reading of
SM18's gate table applies the cap to all bands, whereas the ClinGen Pilot Calculator implements no validity gate at all
and maps point totals directly to `B`/`LB`/`VUS`/`LP`/`P`. Because the calculator omits the gate entirely, its behaviour
cannot settle whether benign calls should pass through uncapped.

#### SM18 substitutes a floor for the GenCC framework's cap

When an MDE's gene-disease validity is `Limited` or below, the GenCC loss-of-function mechanism framework and SM18 state
conflicting rules for the molecular mechanism multiplier
([`themis/svcv4/gene_disease_validity.py`](../../themis/svcv4/gene_disease_validity.py)):

- **The GenCC framework** states that for genes with `Limited` validity or below, the mechanism level should be *capped
  at `Suspected`* (multiplier ×0.25).
- **SM18** reproduces the preceding precondition sentence nearly verbatim, but concludes that for an MDE below
  `Moderate` validity the mechanism must be set to *`Uncertain`* (multiplier ×0).

The difference between `Suspected` (×0.25) and `Uncertain` (×0) zeroes out the entire positive predictive evidence on a
path. Whether the substitution is deliberate for the variant-classification context or an unintended tightening is not
decidable from either document. [`themis/svcv4/classify.py`](../../themis/svcv4/classify.py) enforces SM18's stricter
rule, rejecting any positive mechanism level (`Established`, `Likely`, or `Suspected`) when the gate level is below
`Moderate`. This divergence has specific consequences for classification and reference evaluation:

- **The difference affects classifications only at `Limited` validity (and GenCC's `Supportive` tier, which maps to the
  `Limited` gate) and only in the downward direction.** Below `Limited`, the validity gate is terminal regardless of
  points, and at `Limited` the gate already caps pathogenic totals at `VUS`. However, when a variant has moderate
  negative evidence alongside positive predictive evidence, zeroing the predictive points under `Uncertain` (×0) can
  drop the total from `VUS` into `Likely Benign`, whereas `Suspected` (×0.25) would have retained enough positive points
  to keep the variant in `VUS`.
- **SM18's rule supports contrapositive inference from a curator's worksheet; the GenCC framework's rule does not.**
  Under SM18, if a curator's reference worksheet scores the mechanism as `Suspected`, `Likely`, or `Established`, that
  choice implies the MDE's validity is at least `Moderate`—allowing a reference evaluation on a gene lacking a published
  ClinGen curation to infer `Moderate` validity and reach `Likely Pathogenic`. Under the GenCC rule, `Suspected` would
  also be legal at `Limited` validity, so no such inference would hold.
- **Neither document assigns a default validity level when no curation exists at all.** An MDE with no gene-disease
  validity curation is neither "Moderate or higher" nor "Limited or below". Rather than manufacturing a validity level
  from absence, the library requires the caller to supply an explicit gate level so the judgement remains visible.

#### The `Suspected × Most` matrix cell

SM18 describes the (`Suspected` mechanism, `Most` exon relevance) cell of the predictive scaling matrix in two
incompatible ways. SM18 Figure 1 prints `0%` (`0.0`) inside the cell, whereas SM18 §6 remarks that the combination of
`Suspected` and `Most` was "deliberately not created", phrasing that sounds as if the cell were omitted rather than set
to zero. If an implementer read §6 as an omission and multiplied the marginal axis weights (`0.25 × 0.5`), every
loss-of-function and splice path landing in that cell would receive `12.5%` (`0.125`) of its initial predictive points
instead of zero. [`themis/svcv4/data/matrix.py`](../../themis/svcv4/data/matrix.py) records Figure 1's explicit `0.0`
(`OMITTED_CELL = ('Suspected', 'Most')`) so callers never fall back to multiplying the axes.

#### A small pool at the last DAFT method

Under the strict four-rung DAFT hierarchy
([The DAFT method order is SM3's, and it binds strictly](#the-daft-method-order-is-sm3s-and-it-binds-strictly)), an
analyst only reaches rung 4 (the known-pathogenic-variants method) after ruling out rungs 1, 2, and 3—including the
prevalence/penetrance binning tables at rung 3, which require only a broad bin estimate for prevalence and penetrance.
If the gene then turns out to have fewer than ten qualifying pathogenic variants in ClinVar, SM3 states that the
pathogenic-variants method must not be used and names no fallback method, even though SM3 elsewhere claims that every
variant is assessable for `POP_FRQ`.

Those two statements in SM3 cannot both hold when a gene lacks prevalence/penetrance estimates and has fewer than ten
pathogenic variants. Because lowering the ClinVar review-status floor to pad the pool or returning to a binning method
whose preconditions were already rejected would both violate SM3's rules,
[`themis/svcv4/frequency.py`](../../themis/svcv4/frequency.py) returns `POP_FRQ` as undetermined (`points=None`) and
records the pool-size shortfall in the derivation string.

## Open questions

- **Curator-supplied epidemiological parameters for DAFT methods 2 and 3.** Disease prevalence, allelic heterogeneity,
  locus heterogeneity, and penetrance have no reliable automated source across genes. While
  [`themis/svcv4/data/`](../../themis/svcv4/data) transcribes SM3's binning grids and
  [`themis/svcv4/frequency.py`](../../themis/svcv4/frequency.py) implements the Whiffin/Ware calculator formula, the
  epidemiological parameter estimates themselves remain manual analyst or curator inputs.
- **Policy triggers for per-gene predictor downgrades.** SM6 allows downgrading a missense predictor's evidence strength
  on a gene where it over-calls, distinct from substituting a different predictor. What empirical signal should trigger
  a policy-time downgrade in [`themis/svcv4/data/predictor_policy.json`](../../themis/svcv4/data/predictor_policy.json),
  and how that downgrade maps onto `MIS_PRD` tiers, remains open; SM6's informal cue ("when other predictors suggest
  lower impact") cannot be evaluated per variant without violating SM6's single-predictor rule, so any trigger must be
  evaluated offline at policy-authoring time across a gene.
- **External feeds and override recording for the GenCC mechanism rubric.** Because no public feed currently distributes
  per-gene scores on the GenCC loss-of-function mechanism scale, the analyst scores the rubric on every run. It remains
  open whether ClinGen or GenCC will publish a machine-readable feed in the future, how curator overrides to individual
  rubric rows should be persisted, and what fraction of reference-set runs land at `Uncertain` under autonomous rubric
  scoring.
- **Tissue expression outside GTEx's 54-tissue panel.** SM18's exon-relevance abundance limb compares transcript
  expression within disease-relevant tissues, but GTEx v8/v10 includes no retina, no megakaryocytes or platelets, and no
  ocular trabecular meshwork. Consequently, for inherited retinal dystrophies, platelet disorders, and primary
  congenital glaucoma entities, the curated evidence service cannot answer the abundance limb and the exon-relevance
  call rests on transcript structure alone. Until curated tissue-specific RNA-seq panels are integrated, the analyst
  must report the abundance limb as unestablished and name the missing tissue rather than assembling ad-hoc expression
  figures from the open web that cannot be deterministically reproduced.

## Appendix

### Appendix A: measurements behind the readings

Each row below records a live measurement against gnomAD, ClinVar, the CSpec Registry, or transcript annotations that
decided one of the interpretations in [Design](#design).

| Measurement                                                                                                                                                                                                                                                                                                                                                                                  | What it decided                                                                                                                                                                                                                                                                                   |
| :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Variant `17-4932566-A-T` has exome FAF `3.1e-07`, genome FAF `1.76e-04`, and joint FAF `1.81e-05`; it fails the variant-QC filter in **both** the exome and genome callsets while its joint block reports only a frequency-discrepancy flag.                                                                                                                                                 | The joint Grpmax FAF is the `POP_FRQ` input, and the variant-QC filter gate must be checked per dataset rather than on the joint block alone.                                                                                                                                                     |
| Variant `17-4932533-G-T` has an allele count of zero in the exome callset, a clean call in the genome callset, and a joint count of one.                                                                                                                                                                                                                                                     | A per-dataset exome read misses genome-only alleles; a callset with zero observations contributes no numerator to a dataset-specific read.                                                                                                                                                        |
| On one gene where the source block supplies all four Whiffin/Ware calculator inputs (prevalence `1/4000`, allelic heterogeneity `0.10`, locus heterogeneity `0.60`, penetrance `0.80`), the calculator method yields a DAFT of `9.375e-6` (variant/DAFT ratio `49.3`, `POP_FRQ = -6.0`), whereas the pathogenic-variants method yields a DAFT of `6.51e-5` (ratio `7.10`, `POP_FRQ = -3.0`). | SM3's DAFT method hierarchy must bind strictly with preconditions affirmed before computing a threshold, because method choice alone swings `POP_FRQ` by 3.0 points.                                                                                                                              |
| On that same gene, the pathogenic-variants DAFT (`6.51e-5`) is anchored entirely by a single 0-star ClinVar submission; excluding 0-star submissions moves `POP_FRQ` from `-3.0` to `-6.0`.                                                                                                                                                                                                  | The ClinVar review-status floor is a required caller argument on the pathogenic-variants DAFT, and SM3's suggestion to inspect 0-star records when the pool is small is not adopted.                                                                                                              |
| Across all 206 ClinGen CSpec VCEP specifications (registry text), zero specifications pin a ClinVar star-rating floor on a *frequency* comparator: one panel sets a 2-star floor on a non-frequency comparator, 31 constrain the comparator by submission provenance instead, and 89 leave "known pathogenic" undefined.                                                                     | `min_review_status` in [`themis/svcv4/frequency.py`](../../themis/svcv4/frequency.py) has no default value because there is no registry-wide convention to encode.                                                                                                                                |
| Across ClinVar, **340** records carry a reduced-penetrance or risk-allele classification term spelled in **19** distinct ways; only **57** of those 340 also carry an unqualified pathogenic term.                                                                                                                                                                                           | Both classification gates in [`themis/svcv4/clinvar_classification.py`](../../themis/svcv4/clinvar_classification.py) parse aggregate strings token by token; filtering at query time for unqualified terms alone would leave the other 283 records invisible to SM19 (`*_INF`) and SM18 density. |
| A naive substring match on `"athogenic"` admits `"Conflicting classifications of pathogenicity"`, which on one gene inflated the pathogenic-variants DAFT ninefold by anchoring the maximum on a synonymous variant.                                                                                                                                                                         | Pathogenicity membership is parsed token-by-token using ClinVar's `/` and `;` grammar, never by substring matching.                                                                                                                                                                               |
| A single 2-star risk-factor allele present at ~1% allele frequency in European ancestry populations sets its gene's pathogenic-variants DAFT two orders of magnitude above a Mendelian threshold.                                                                                                                                                                                            | The narrow DAFT gate (`is_unqualified_pathogenic`) rejects all penetrance-qualified terms, risk-allele terms, and records carrying a `;` tail.                                                                                                                                                    |
| Using the wide classification gate (`is_pathogenic`) for SM18's gene-level pathogenic-variant density increased qualifying variant counts on two complement genes from 156 to 248 and from 107 to 151 records.                                                                                                                                                                               | SM18 density uses the wide gate rather than the narrow DAFT gate because low-penetrance pathogenic alleles still demonstrate disease-causing variation in the gene.                                                                                                                               |
| On one gene, all 49 coding transcripts across Ensembl and RefSeq include the assessed exon and every transcript omitting the exon is non-coding; evaluating `All` vs. `Most` before filtering out non-coding transcripts drops a nonsense variant from `+6.0` (`Likely Pathogenic`) to `+3.0` (`VUS-mid`).                                                                                   | SM18's transcript admission filter runs before evaluating either `All` or `Most`.                                                                                                                                                                                                                 |
| On another gene, a curated RefSeq transcript terminates upstream of the assessed exon without splicing around it, failing the simple "spans-but-skips" check while omitting the exon.                                                                                                                                                                                                        | An empty "spans-but-skips" transcript list does not establish `All`; transcripts that stop short of the locus also omit the exon.                                                                                                                                                                 |

### Appendix B: the predictor-policy evidence

The evidence behind [`themis/svcv4/data/predictor_policy.json`](../../themis/svcv4/data/predictor_policy.json) comes
from two comparative evaluations rather than from SM6's list of calibrated predictors (which is a precondition for
eligibility, not a comparative ranking). Because one of the two evaluations is an unpublished conference poster, this
appendix summarises only its relative ranking of predictors and omits its raw accuracy numbers, which remain the
authors' to publish.

**Global default: BayesDel (no-allele-frequency build).** Selecting the no-allele-frequency build (`BayesDel_noAF`) is
required by the calibration and genome build: ClinGen calibrated the no-allele-frequency flavour (labelled simply
`"BayesDel"` in SM6's calibration figure), and no GRCh38 pre-computed score set exists for the allele-frequency build.
In an unpublished VCGS/MCRI comparative poster (Ciotta, De Fazio, Lunke, and Stark; no DOI or PMID), the authors
evaluated five calibrated predictors—BayesDel, REVEL, VARITY_R, AlphaMissense, and MutPred2—across the laboratory's own
ClinVar-submitted missense variants against internal clinical classifications. Across the full multi-gene dataset,
BayesDel ranked first overall and first on pathogenic/likely-pathogenic sensitivity, trailing only VARITY_R on
benign/likely-benign specificity.

**Per-gene override for `PKD1` (`HGNC:9008`): AlphaMissense.** On `PKD1` specifically, that same VCGS/MCRI comparison
ranked BayesDel last among the five predictors on pathogenic prediction rate, behind AlphaMissense. Independently, a
medRxiv preprint on `PKD1` evaluated SIFT, PolyPhen-2, CADD, REVEL, and AlphaMissense against an ACMG/AMP-reclassified
`PKD1` truth set and found that AlphaMissense was the only tool among the five with zero truth-set misclassifications.
Although MutPred2, REVEL, and VARITY_R scored higher than AlphaMissense on raw `PKD1` pathogenic call rate in the
VCGS/MCRI comparison, all three were ruled out:

- **MutPred2** ranked last on benign/likely-benign specificity across the full laboratory dataset, indicating that its
  high positive call rate comes from over-calling pathogenicity.
- **REVEL** was evaluated directly in the `PKD1` preprint alongside AlphaMissense and misclassified truth-set variants
  where AlphaMissense made no errors.
- **VARITY_R** had no `PKD1`-specific benign specificity measurement in either study.

Neither study shows that AlphaMissense performs ideally on `PKD1`—only that its errors on `PKD1` are uninformative
omissions rather than false calls, whereas BayesDel ranks last on `PKD1` sensitivity. Because the preprint did not
include BayesDel, the two tools were not compared head-to-head on its truth set.

**Methodological limitation of threshold-applied comparisons.** Both studies evaluate predictors at a fixed score
threshold against binary classifications, which conflates two properties that SVCv4 separates: intrinsic rank
discrimination (how well the continuous score separates pathogenic from benign variants) and threshold calibration
(whether the published cut-points sit at the intended likelihood ratios on a given gene). Neither study publishes
per-variant raw scores that would allow a threshold-free ROC/AUC comparison on `PKD1`. Because every predictor in the
VCGS/MCRI study performed worse on `PKD1` than on the full dataset, the poster authors recommend calibrating
gene-specific thresholds for `PKD1`; until a `PKD1`-specific calibration is published, substituting AlphaMissense in
[`themis/svcv4/data/predictor_policy.json`](../../themis/svcv4/data/predictor_policy.json) is the interim gene-level
policy that SM6 sanctions.

### Appendix C: two derivations, worked

**Recovering the Whiffin/Ware DAFT formula (SM3).** SM3 names the calculator method and its four input
parameters—disease prevalence, allelic heterogeneity (`hetA`), locus/genetic heterogeneity (`hetG`), and penetrance
(`pen`)—but never prints the algebraic formula. [`themis/svcv4/frequency.py`](../../themis/svcv4/frequency.py) recovers
the formula from Whiffin et al. (PMID 28518168) and verifies it against the ClinGen Pilot Calculator:

- **Monoallelic inheritance:** $\\text{DAFT} = \\frac{1}{2} \\times \\text{prevalence} \\times \\text{hetA} \\times
  \\text{hetG} \\times \\frac{1}{\\text{pen}}$
- **Biallelic inheritance:** $\\text{DAFT} = \\sqrt{\\text{prevalence}} \\times \\text{hetA} \\times
  \\sqrt{\\text{hetG}} \\times \\frac{1}{\\sqrt{\\text{pen}}}$

The calculator rounds the computed DAFT to three significant figures. In both inheritance formulas, allelic
heterogeneity (`hetA`, the maximum fraction of disease cases attributable to any single allele) enters linearly outside
the square root, which reveals an arithmetic error in SM3's own worked example where the `hetA` factor was omitted.
Licensing governs how we use the upstream sources: the Whiffin et al. paper (PMID 28518168) is published under **CC BY
4.0** (permitting reuse with attribution), the web calculator application source is licensed under **LGPL-2.1** (so we
cite and reimplement the mathematical formula in [`themis/svcv4/frequency.py`](../../themis/svcv4/frequency.py) without
copying code), and the paper's companion analysis repository carries no open-source licence and does not contain the web
app.

**Why MaveDB primary calibration markers must be honoured (SM20).** On a `TP53` truncating variant, one MaveDB deposit
carries multiple OddsPath calibrations where the **non-primary** calibrations bin the variant's score as `Supporting`
benign (`-1.0` points), whereas the deposit's **primary** calibration places the same score in the indeterminate (`0.0`)
range. Restricting [`themis/services/evidence/upstreams/mavedb.py`](../../themis/services/evidence/upstreams/mavedb.py)
to the depositor's primary calibration removes that spurious benign call, and the nucleotide and protein representations
of the variant then agree on a separate deposit that scores the variant as `Strong` pathogenic (`OddsPath = 50.2`). In a
second case on `BRCA1`, reading calibrations in raw JSON array order rather than by the primary marker shifted a
variant's `OddsPath` from `0.0383` to `0.078` solely on array ordering, and two `BRCA1` score sets binned an identical
numeric assay score at two different evidence strengths.
