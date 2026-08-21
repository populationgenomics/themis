# Design: reading SVCv4 — the interpretations the evidence layer applies

> **SVCv4 is a draft standard, and this is evaluation software.** The framework was released as a **pilot in July
> 2026**; its point values, thresholds, code names and data model **may change before publication** (target: *Genetics
> in Medicine*, ~Jan 2027), and Supplementary Material 17 (non-coding variants) is unreleased. Nothing here is a
> validated implementation of the standard, nothing here is for clinical or diagnostic use, and no output built on it is
> a clinical variant classification. Every reading recorded below has to be re-verified against the final published
> standard and the [ClinGen Pilot Calculator](https://calculator.clinicalgenome.org/v4/pilot/ui/classification).

**Related:** [`evidence-interfaces.md`](evidence-interfaces.md) (the rpcs and the in-sandbox library that apply these
readings), [`analysis-scenarios.md`](analysis-scenarios.md) (the graded reference set a reading is scored against),
[`literature-evidence-layer.md`](literature-evidence-layer.md) (the paper-derived evidence beside the curated sources),
[`../PRODUCT.md`](../PRODUCT.md) (the product frame: §6 facts + judgement, §12 the Spike); terms in
[`../../GLOSSARY.md`](../../GLOSSARY.md).

## Overview

Running SVCv4 without a human at every step means deciding, in advance, what the standard *means* in the places where it
does not say: which ClinVar classification terms count as a pathogenic assertion, which frequency figure `POP_FRQ` is
scored against, what licenses the *All* exon-relevance tier, which of two contradictory sentences in the same supplement
to follow. These are clinical judgements about a clinical standard, not engineering choices, and they move
classification outcomes by whole points. This doc is where each one is recorded, with the supplement it rests on, the
alternatives, and the direction the wrong choice errs in — so a clinical reviewer can check them without reading service
code.

Three decisions govern all of them.

- **A reading is made once and written down, never per variant.** Choosing after seeing the answer — which predictor,
  which frequency method, which review-status floor — is the multiple-testing defect SM6 names, and it applies to every
  choice with a direction, not only to predictors.
- **Where the framework supplies a disposition for an unsettled input, that disposition is applied.** The standard has
  no general "take the least-committed value" principle, and inventing one would systematically move totals in one
  direction.
- **What stays open and class-determinative is reported as no class**, with the total each reading yields, rather than
  as one of them.

A published VCEP criteria specification supersedes every reading here for the gene it covers. Where one exists, it is
fetched rather than reasoned about
([`evidence-interfaces.md`](evidence-interfaces.md#goal-does-a-vcep-publish-its-own-criteria-specification)).

## Background

SVCv4 scores a variant against a **mono-disease entity** (MDE: gene × phenotype × inheritance × mechanism), not against
a gene. Each evidence code contributes points, the points sum, and the sum bands to a class. Two structures make the sum
more than addition. Positive *predictive* points are scaled by a **matrix** of molecular mechanism (Established / Likely
/ Suspected / Uncertain → ×1.0 / 0.5 / 0.25 / 0) against exon relevance (**All / Most / Few**). And the curated
**gene-disease validity** level both caps the resulting class and gates the mechanism multiplier. Codes are named by
family — `POP_FRQ` / `POP_HMZ` frequency, `*_PRD` in-silico prediction, `*_INF` other classified variants at the same
codon or exon, `*_FXN` functional assays, `CLN_*` clinical, `LOC_*` phenotype and segregation.

The normative text is the per-topic **Supplementary Materials**, cited throughout as SM*n*. The full term list,
including the abbreviations used below, is the vocabulary table in
[`evidence-interfaces.md`](evidence-interfaces.md#vocabulary).

Two things about the source text shape everything here. It is a **pilot**: several supplements disagree with each other,
a few disagree with themselves, and one (SM17, non-coding) does not exist yet. And it is written **for an analyst**: the
framework repeatedly hands a decision to "an experienced analyst with a broad understanding of the pathogenesis of
genetic disease" and states no test. Those hand-offs are not gaps to be closed by code — the model makes them, with
shown reasoning, and a curator can override. What is recorded here is the narrower set: where the standard *does* commit
to something and reading it takes work, and where it commits to two incompatible things.

## Non-goals

- **Guidance for human curation.** These readings exist to make an autonomous run reproducible and auditable. They are
  not advice to a curator, and none of them is validated against a clinical outcome.
- **Filling the framework's gaps with invented numbers.** Where the standard states no threshold, no threshold is
  supplied — the signals go to the analyst instead. A reading that would have to name a quantity the standard declines
  to name is rejected, not calibrated here.
- **Re-litigating the standard.** Where the framework is internally consistent and simply strict, it is followed. The
  contradictions collected in [Where the standard contradicts itself](#where-the-standard-contradicts-itself) are the
  ones that changed an outcome.

## When an input stays open

A run reaches nodes the model cannot settle. The tempting rule — take the least-committed value — is not one SVCv4
supports: the standard states no general conservatism principle, and SM6, SM9 and SM13 each resolve *toward* more
pathogenic evidence. A function that picked a class from the competing readings would also be judgement in code: it
could not express an open splice colour or the missense workflow's two separate exon calls, it drops the VUS sub-band,
and it cannot rank the validity gate's two terminal outcomes, which are not classes at all. The precedence applied
instead, in order:

1. **Where the framework supplies a disposition, apply it — the input is not open.** SM18 zeroes the initial evidence
   points on an Unknown or unassessed mechanism; SM3 errs high on an uncertain DAFT; a non-concordant functional assay
   takes 0.0; a below-floor `POP_HMZ` scores nothing.
1. **Where the shown evidence answers SM18's own admission filter, that read is the answer**
   ([exon relevance](#exon-relevance-what-the-tiers-admit)). Taking the weaker of the competing readings is the fallback
   for an analyst who declines to judge, or for evidence that genuinely does not separate — not the rule.
1. **Open because a retrieval never happened is not open.** Retrieve it.
1. **What remains open and class-determinative is reported as no class**, with the total each reading yields, rather
   than as one of them.

## Frequency: which figure, and what disqualifies one

The `POP_FRQ` input is the **joint** (exome plus genome) Grpmax filtering allele frequency — gnomAD's filtering
frequency maximised over its genetic-ancestry groups — per ClinGen's VCEP Review Committee gnomAD guidance (v3.0, June
2025). A per-dataset read scores zero for a variant called only in genomes, which reads back as absence. The figure
applies to **both** sides of the comparison: the variant's own FAF, and every FAF entering a pathogenic-variants DAFT. A
VCEP that specifies its own rule supersedes it.

The joint figure is not quality-screened, so the variant-QC verdict is a separate gate, read **per dataset**.

> **The asymmetry, which VCEPs state verbatim: a filter-failing dataset counts against rarity, never toward benignity.**
> Such a FAF scores zero and anchors no DAFT. gnomAD's own caveat flags are caveats rather than QC verdicts, so they
> ride along unscored, for the analyst to weigh.

### The DAFT method order is SM3's, and it binds strictly

SM3 ranks its methods: a curated VCEP or community threshold first, the calculator method as the first *computed*
choice, the pathogenic-variants method generally third and usable only where the prior methods are "not appropriate".
The ordering is applied as a **strict** one, with each method's precondition affirmed *before* any DAFT is computed. SM3
ranks the methods only "generally", which would otherwise let a method be picked for the answer it gives — and the
spread is not marginal: on one gene the calculator method yields `POP_FRQ` −6.0 where the pathogenic-variants method
yields −3.0 ([Appendix A](#appendix-a-measurements-behind-the-readings)). The expression the methods compute is not in
SM3 either, and is recovered rather than transcribed ([Appendix C](#appendix-c-two-derivations-worked)).

No selector picks between the methods automatically. "Appropriate" is SM3's word for a judgement about the *parameter
estimates*, and the presence of arguments cannot stand in for it. Using the pathogenic-variants method as a cross-check
is sanctioned by SM3, but stays an offline exercise: a comparison the agent can see is one it can reason backwards from.

### The review-status floor for a pathogenic-variants DAFT is an input, not a convention

A DAFT derived from the gene's known pathogenic variants rests on which ClinVar records are admitted, and the review
status they must carry is a **required argument with no default**, stamped on the result. There is no convention to
encode: across all 206 ClinGen CSpec VCEP specifications, none pins review status on a *frequency* comparator
([Appendix A](#appendix-a-measurements-behind-the-readings)). A default would assert a norm that does not exist, on a
choice that moves the answer by whole points, and would silently override a VCEP that specifies its own. One star
(criteria provided) is the value used, and it governs **both** the ten-variant precondition and the variant that sets
the maximum — one pool, one bar, or the count and the threshold are drawn from different populations.

Two further readings follow the same direction of caution, because this method's failure mode is a false *benign* call.

- **The Working Group's suggestion to review the zero-star records individually where the pool falls short of ten is not
  adopted.** Deciding admission after seeing whether the pool clears the bar re-opens exactly the
  choose-after-the-result defect the floor closes.
- **A bounded pool yields a lower bound.** A pool fetched in an order that is not by frequency can have its maximum
  taken over a prefix, and a threshold that is too low awards benign points the evidence does not support. So truncation
  is carried as state to branch on rather than as a caveat in prose, and a pool member whose frequency was never fetched
  counts toward no precondition and is scored as nothing at all.

The other half of "known pathogenic" is not a star count: a record whose classification conflicts is excluded by the
classification gate at every review status.

## Which ClinVar classifications count as a pathogenic assertion

ClinVar's aggregate germline classification is a *sentence*, not an enum, and the informative-variant rules (SM19) and
the pathogenic-variants DAFT (SM3) read the same records through **different** gates. Both parse the aggregate term by
term; a substring test does not survive contact with the vocabulary
([Appendix A](#appendix-a-measurements-behind-the-readings)).

**The separators carry the grammar, and they mean different things.**

- A **`/`** joins co-equal ACMG classification terms the aggregate is genuinely between — "Pathogenic/Likely
  pathogenic". Every term around a `/` is an ACMG assertion, so every one of them has to be a pathogenic assertion for
  the record to be a member.
- A **`;`** appends an assertion that is *not* an ACMG classification at all — "Pathogenic; risk factor", "Pathogenic;
  drug response". The tail says nothing about pathogenicity and does not disqualify the leading ACMG term.
- "Conflicting classifications of pathogenicity" is not a pathogenic assertion, and neither is an empty term. An empty
  term is common rather than exceptional: its dominant cause is a record carrying no germline classification at all —
  ClinVar's evidence-only submissions, which assert observations without a classification — so it is excluded as an
  ordinary outcome rather than treated as a malformed record.

**The penetrance and risk-allele terms are where the two gates part.** ClinVar spells reduced penetrance and risk
alleles as classification terms of their own: "Pathogenic, low penetrance", "Likely pathogenic, low penetrance",
"Established risk allele", "Likely risk allele". The wider gate admits all four; "Uncertain risk allele" is not among
them, because it asserts uncertainty rather than risk.

SM3's DAFT reads the same pool through a strictly **narrower** gate that excludes the penetrance-qualified and
risk-allele terms. The reason is a frequency argument, and it is specific to the DAFT: such an allele reaches a
population frequency a fully-penetrant one could not, so admitting it lifts the threshold the method is deriving — one
2-star risk-factor allele at ~1% in Europeans sets its gene's threshold two orders of magnitude above a Mendelian one.
SM19 states no matching condition, and a reduced-penetrance allele can be sound evidence for what a residue tolerates,
so the informative-variant rules keep the wide gate.

**The pathogenic-variant density SM18 reads follows the wide pool too**, for the same reason: a low-penetrance
pathogenic allele is disease-causing variation in the gene whatever its frequency.

**An expert-panel record may be adopted.** Where the queried allele itself carries an expert-panel consensus
classification, that classification may be adopted — following its cited PMIDs into the papers behind it — unless
patient-specific circumstances make it inapplicable. What licenses this is the review status *phrase* (which panel, at
which tier), not the star count.

## In-silico prediction: one predictor, chosen in advance

SVCv4 requires the missense `MIS_PRD` score to come from **one** calibrated predictor **chosen before the variant is
scored**: evaluating several and taking the best is multiple testing over metapredictors that are heavily correlated
(SM6). What SM6 bans is choosing per variant, not choosing per gene — it explicitly encourages distinct predictors for
specific genes "so long as they are selected in advance of the evaluation of a given VBC", and allows downgrading a
predictor's evidence strength for a gene it over-calls until a gene-specific calibration exists.

So the choice is exercised once, at policy time, and the policy is **per gene by construction**: a default, per-gene
overrides, and per-gene downgrade flags. Choosing the entries is expert work done against the calibration literature; a
frontier model can assist, but the result is curator-owned and then frozen, keyed on the gene's HGNC id so a symbol
reassignment cannot move an entry to a different gene.

The current policy is BayesDel by default and AlphaMissense for one gene; the evidence behind both entries — which is
*not* SM6's approved list — is [Appendix B](#appendix-b-the-predictor-policy-evidence). Asking an interface for a second
predictor's score is asking about a different SVCv4 line, never a second opinion on this one.

## Mechanism: a rubric an analyst scores, not a field to retrieve

The Established / Likely / Suspected / Uncertain level driving the ×1.0 / 0.5 / 0.25 / 0 multiplier is the scale of the
GenCC Mechanism Curation Working Group's
[*Recommendation for Loss of Function Mechanism Curation*](https://clinicalgenome.org/site/assets/files/10698/draft_lof_framework_position_statement.pdf)
(v1.0, September 2025, draft), which SM18 imports. On that scale an evidence score bands ≥0.99 Established, 0.90–0.98
Likely, 0.50–0.89 Suspected and ≤0.49 Uncertain, and is scored only at gene-disease validity Moderate or above.

**No per-gene curation on that scale is published, for any gene.** So a mechanism statement citing the framework cites
that document, not a retrieved field, and SM18's fallback is the standing case: an analyst with a broad understanding of
the pathogenesis of genetic disease classifies the entity manually. The model makes the call the way SM18 asks the
analyst to, reading the gene-scoped curated signals, the sources' mechanism narratives, and the narrative the literature
interface returns.

The framework's one machine-readable equivalence is ClinGen's **dosage-sensitivity haploinsufficiency score**: its top
value stands in for Established on a *monoallelic* entity. Any lower score returns the call to the rubric rather than
yielding a weaker tier — the two out-of-range values in that vocabulary mean "recessive" and "dosage sensitivity
unlikely", which are not mechanism levels. That score is curated **per gene**, and on a gene carrying both a
loss-of-function and a gain-of-function entity (SM21), reading a gene-level haploinsufficiency score as the chosen
entity's is exactly the error SM18 exists to prevent — it would scale the multiplier on every LoF path.

> **Uncertain (×0) is the floor when the evidence genuinely will not support a call — never a default fired by an absent
> haploinsufficiency score**, which would discard exactly the evidence a curator uses. A zeroed multiplier is a
> legitimate, shown outcome: "unknown is a valid result" (PRODUCT §6). A claim of a level above Uncertain under a lesser
> validity tier is a contradiction for the model to resolve and resubmit, not one to coerce downward.

## Exon relevance: what the tiers admit

SM18 defines All / Most / Few over which of the gene's transcripts carry the assessed exon, and then asks for the
*abundance* of the carrying transcripts versus the omitting ones. The membership half is an interval test on genomic
coordinates — a fact, and the one the definitions are written over. The abundance half is a judgement, and it stays the
analyst's: SM18 calibrates neither "functionally equivalent" nor "reasonably well expressed", and states no quantity
between "most but not all" and "not in clinically relevant transcripts". So the tier is never stated as though the
standard had supplied it — it is reported as a judgement over a stated denominator, and any threshold it rests on is the
analyst's, named as such.

> **pext cannot establish "All".** gnomAD's proportion-expressed-across-transcripts supports "no evidence of
> differential exclusion", which argues against *Few* and does not separate All from Most — the half that carries the
> points. A dip / no-dip boolean would structurally only ever answer All, and would bias every total upward. The same
> reason makes a scalar pext useless: it answers neither question SM18 asks, so the per-exon, per-tissue profile is what
> a reading is made over.

Three readings of SM18 are settled here, because each alternative fails in the dangerous direction.

- **An empty "spans but skips" group licenses nothing.** A curated isoform *stopping short* of the locus omits the exon
  as surely as one splicing past it, and SM18 states the operative test outright: the overall abundance of transcripts
  including or omitting the exon should be evaluated, *not just the presence or absence of distinct transcript
  isoforms*. Reading an empty group as forcing *All* converts a curator judgement into a value at exactly the cases
  where the tier is genuinely open.
- **SM18's admission filter runs before either tier**, so emptying the omitting set is not the two-limb route — the
  reading that settles *All* versus *Most* from the membership limb together with the abundance limb rather than from
  either alone. The filter's exclusion sentence sits *inside* the *All* definition and *Most* is defined over that same
  admitted set, so both limbs are read over the filtered inventory. Admitting no omitter is a legitimate outcome; the
  analyst is then on the abundance limb, carries its obligations, and reports the tier as a judgement over a stated
  denominator. What is forbidden is reading an empty set as the standard's own *All*.
- **Running the two limbs over the *unfiltered* inventory inverts the filter.** On a gene whose every coding transcript
  carries the exon exactly and whose only omitters are non-coding, that rule moves a nonsense variant from Likely
  Pathogenic to VUS on the strength of a lncRNA record ([Appendix A](#appendix-a-measurements-behind-the-readings)).

One consequence for whoever reads a transcript inventory: **no ratio may be taken out of it.** The denominator is an
annotation set's whole current list, not a curated census. RefSeq publishes every "transcript variant" record it holds,
which can be an order of magnitude more records than the Ensembl set for the same gene, and the direction it errs in is
not constant. Which annotation set defines "all transcripts" is a question SM18 leaves open, so each set's denominator
is read in its own namespace and stated.

## Functional assays: which deposit speaks, and about what

A MAVE deposit is evidence about the allele it was mapped to, and several deposits can score one allele and disagree. So
which deposit answers, and what its result is evidence *for*, are readings rather than a serialisation order.

- **A deposit reached under the allele that was asked about is about that change; one reached under a derived allele is
  about its consequence.** One variant can be deposited at the nucleotide level in one score set and at the protein
  level in another, so both forms are asked — but match directness ranks ahead of publication date. Without that
  ordering, a protein-level deposit published a day later takes over the answer for the nucleotide change that a caller
  asked about precisely because it matches that change.
- **Calibration ranks ahead of a bare score, and the depositor settles which calibration speaks.** Where a deposit marks
  one calibration primary, only that one is read; a deposit marking several is a case with no ground for choosing, and
  guessing would put the arbitrariness back unlabelled. This is not a formality: reading a deposit's non-primary
  calibrations manufactures a benign functional criterion that its primary calibration does not support
  ([Appendix C](#appendix-c-two-derivations-worked)).
- **Two deposits can both be right about different questions.** For the same TP53 residue, one deposit is saturation
  genome editing over exons 5–8 reading loss of function, and another is a reporter-cell enrichment assay reading
  *dominant-negative* effect. A truncation at that residue removes the oligomerisation domain, so it is loss-of-function
  and not dominant-negative: neither result is out of range, and the analyst's call is which question the MDE's
  mechanism asks. `*_FXN` scores an assay only where it measures the disease-relevant function and agrees with the
  mechanism; a non-concordant assay takes 0.0.

## Clinical and locus observations: the categorical readings

The seven `CLN_*` and `LOC_*` codes are deterministic *downstream* of a categorical reading, and the reading is
irreducibly the analyst's: the phenotype tier, whether each of SM4's conjuncts is affirmed, parentage, each relative's
genotype and affected status, the penetrance band, the in-trans class, which yield figure applies. Three of those
readings are stated here because a reasonable analyst lands elsewhere without them.

- **SM4's affected-proband column is a conjunction against a disjunction, so it takes three affirmations, not one
  judgement.** The cell text is an AND against an OR, and a hand-derivation slips exactly there.
- **Phenotype specificity is a count over a stated cohort, never a percentage.** The choice of cohort is the whole
  uncertainty, and counts keep the bin gaps visible where a rounded rate hides them.
- **Silence is not exclusion.** On the reading that a source saying nothing about a caveat thereby excludes it, SM4's
  first sub-row conjunction does no work at all — any silent source satisfies it. The reading applied is an ordered
  test: does the source state a later sub-row's disjunct, and only then is the first affirmed. A neighbouring reading
  invents a tie-break — that a fact spent establishing the phenotype tier is not charged again in the sub-row — and is
  rejected too: it appears nowhere in the table, and it would silently apply to any case whose tier rationale names a
  caveat.

Two framework invariants bind alongside: SM4's affected-proband code is unavailable outside two `POP_FRQ` values, and no
conditioned code may award points outside its gate or with no `POP_FRQ` in the tally at all.

Four readings are **not** settled. Each is a place where a convenience would hide the conflict rather than resolve it:

- SM5's non-segregation branch, where the supplement contradicts itself three ways ([below](#co-segregation));
- whether SM18's waiver lets the same pathogenic variants fund informative-variant points as well;
- specific-versus-consistent phenotype, which turns on the *absence* of an examination, so a two-valued reading takes
  silence for a negative;
- and whether the two per-individual clinical codes carry a code-level cap, which the framework does not state.

## Criteria specifications: a VCEP profile is ACMG-2015

SM3 ranks a curated VCEP or community threshold first among its DAFT methods, so a panel's own specification outranks
every computed value for the gene it covers. But the ClinGen CSpec Registry serves **ACMG/AMP-2015** profiles and there
is no SVCv4 profile in it. Two readings follow.

- **A panel's points, criteria-combining rules and code names are not SVCv4's** and are not translated into them. A
  panel's point value is a *string* to reason about, not a number to add.
- **A threshold enters SM3's ordering at the rung its own derivation earns**, not at method 1 by virtue of who published
  it. A registry entry stating a population-frequency threshold with no derivation anywhere in the registry is a finding
  to report, not a first-rung DAFT.

## Where the standard contradicts itself

Each of these changed, or could change, a class. The full inventory of framework inconsistencies found while
implementing, with the reading applied to each, is the feedback submitted to the SVCv4 working group in July 2026, held
with the team's pilot-corpus materials rather than in this repository, since the corpus is embargoed to the working
group. Read that feedback whole before raising anything about a supplement, since a search for one section will miss the
others that bear on it. Two instances not stated elsewhere here: SM6's figure prints one predictor-threshold cell
non-monotonically against its neighbours, with only one monotonic reading available; and SM3 carries two unreconciled
versions of the pathogenic-variants method, differing on whether a small pool means the method "should not be used" or
"should be reconsidered" and on which frequency statistic to take.

The general rule this leads to: **reproduce a class-determinative number rather than trusting it.**

### Co-segregation

SM5 disagrees with itself. One section recommends a *negative* award for non-segregation in three inheritance contexts,
while the code's range is stated as non-negative twice, in the title block and in the following section. Both readings
cite SM5. The pilot calculator follows the range; the reading applied follows the recommendation. Neither side moves
until SM5 is reconciled.

### De novo observations, and the two per-individual clinical codes

SM4 sums de novo points across probands under no cap, and the weight is stated *per proband*, so an upper bound would be
an invention and none is applied. The two per-individual clinical codes have the same shape and are read the same way:
SM4 weights both per individual and states no code-level cap, so clamping either to the largest single tariff reads a
tariff as a code range and truncates a legitimate multi-observation total in the benign direction. The pilot calculator
clamps; it is the side expected to move.

### The validity gate against a benign call

The gate caps the class by the MDE's curated validity level. Whether it is also meant to block a *benign* call in a gene
below the validity threshold is open. A literal reading of the gate table produces that, and the pilot calculator
implements no gate at all — it maps summed points straight to a band — so no calculator behaviour can validate either
reading.

### SM18 substitutes a floor for the GenCC framework's cap

The GenCC framework SM18 imports says mechanism levels should be *capped at Suspected* for genes at Limited validity or
below. SM18 carries the preceding precondition sentence near-verbatim and then states the opposite: such an MDE is to be
considered *Uncertain*. Suspected scales the initial predictive points by 0.25 and Uncertain by 0, so the readings
differ by a path's whole positive predictive evidence. SM18 is the reading applied. Whether the substitution is
deliberate for the variant-classification context or an unintended tightening is not decidable from either document.

- **The reach is one level wide and one-directional.** At Limited the class gate already caps at VUS, so the mechanism
  level cannot move a pathogenic call there; below Limited the gate is terminal. The divergence therefore bites only at
  Limited — and at the GenCC-only classification that maps to it — and only downward: zeroing the positive predictive
  points can carry a case from VUS to likely benign that Suspected would have left at VUS.
- **SM18's form is usable contrapositively; the framework's is not.** A curator who scored the mechanism Suspected has
  thereby asserted validity is Moderate or higher — which is how a case with no ClinGen curation at all reaches a
  validity level and its Likely Pathogenic total. Under the framework, Suspected is permitted at Limited and the same
  inference is invalid.
- **Neither document addresses an MDE with no validity curation at all**, which is neither "Moderate or higher" nor
  "Limited or below". That case is refused rather than assigned a level manufactured from the absence, so the call stays
  with the model.

### The `Suspected × Most` matrix cell

Every derived document describes this cell wrongly: the vendored scoring reference, its mirror and the corpus summary
all describe it as deliberately not created, at a non-zero multiplier, where the source matrix shows zero. A fresh
implementer reading any of them would award that multiplier across every LoF and splice path.

### A small pool at the last DAFT method

Under the strict method ordering, reaching the pathogenic-variants method means the three earlier methods were already
ruled out — including the binning method, whose preconditions are only the inheritance mode plus prevalence and
penetrance to a bin — so there is nothing above to return to and nothing below. SM3 disqualifies the method outright
when the pool is too small, names no alternative, and elsewhere asserts that all variants are assessable for the
criterion. Those two statements are inconsistent, and the inconsistency is the standard's. Neither relaxing the star
floor to reach the pool size nor substituting a method whose preconditions were already denied is available, so
`POP_FRQ` is undetermined and the classification says so.

## Open questions

- **Two content gaps in the vendored scoring reference**, found and not yet closed. The homozygote code's preconditions
  omit SM3's requirement that penetrance be near 100% and affected individuals not be expected in population databases,
  so a model driving off the encoding applies the negative points at any penetrance. And the affected-proband code's
  second table is encoded with three of its five rows.
- **The calculator method's DAFT inputs have no automated source.** Prevalence, penetrance and heterogeneity — and, for
  the binning method, which of SM3's grids applies plus two of the same estimates — stay curator inputs. The grids
  themselves are transcribed and the cell is looked up.
- **Predictor downgrade.** SM6 allows reducing a predictor's evidence strength for a gene pending a gene-specific
  calibration, distinct from swapping the predictor. What triggers a downgrade and how it is recorded is undecided —
  SM6's own cue, other predictors suggesting lower impact, is the multi-predictor comparison it bans per variant, so it
  can only be exercised at policy time, over genes.
- **Mechanism-level evidence.** Whether per-gene curations on the GenCC framework's scale are ever published as a feed —
  none exist today, for any gene, so the level is always the analyst's; how a curator override is recorded; and how
  often the call lands Uncertain across the practice variants, which needs measurement.
- **Expression outside GTEx's tissue panel.** SM18's abundance limb compares transcripts *within* a tissue, and GTEx
  samples no retina, no megakaryocyte or platelet, and no trabecular meshwork. So for an inherited retinal dystrophy, a
  platelet disorder or a glaucoma entity the limb is unanswerable from the curated sources, and the tier rests on
  structure alone. Until that changes the limb is reported unestablished and the gap named: a figure assembled over the
  open web is one nobody can re-derive.

## Appendix A: measurements behind the readings

Each row is a live measurement or a registry count that decided a reading above.

| Measurement                                                                                                                                                                                                                                                                                                                                          | What it decided                                                                                                                            |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `17-4932566-A-T`: exome FAF 3.1e-07, genome 1.76e-04, joint 1.81e-05; fails the variant-QC filter in **both** datasets while its joint block reports only a frequency-discrepancy flag.                                                                                                                                                              | The joint FAF is the `POP_FRQ` input, and the QC gate is read per dataset.                                                                 |
| `17-4932533-G-T` is zero-count in the exome callset and clean in the genome, with a joint count of one.                                                                                                                                                                                                                                              | A callset calling no allele contributes no numerator.                                                                                      |
| On one gene the source block supplies all four calculator inputs — prevalence 1/4000, allelic heterogeneity 0.10, locus heterogeneity 0.60, penetrance 0.80 — giving a DAFT of 9.375e-6, a ratio of 49.3 and `POP_FRQ` −6.0, where a pathogenic-variants DAFT of 6.51e-5 gives a ratio of 7.10 and `POP_FRQ` −3.0.                                   | The DAFT method order is SM3's and binds strictly: three points of benign evidence turn on method choice alone.                            |
| That same gene's pathogenic-variants DAFT rests entirely on one 0-star record; dropping it moves `POP_FRQ` from −3.0 to −6.0.                                                                                                                                                                                                                        | The review-status floor is a required argument, and the zero-star review suggestion is not adopted.                                        |
| Across all 206 CSpec VCEP specifications, none pins review status on a *frequency* comparator: one group sets a 2-star floor but on a different comparator, 31 constrain the comparator by provenance instead, and 89 leave "known pathogenic" undefined. (Registry text only — a VCEP pushing detail into a linked PDF appendix would not surface.) | The floor has no default: there is no convention to encode.                                                                                |
| Registry-wide, **340** ClinVar records carry a penetrance or risk qualifier, spelled **19** different ways; only **57** also carry an unqualified pathogenic term.                                                                                                                                                                                   | Both gates parse the aggregate term by term; a narrower search term leaves the other 283 invisible to the search as well as to the filter. |
| A substring test on "athogenic" admits "Conflicting classifications of pathogenicity": measured on one gene, a ninefold DAFT inflation, anchored on a synonymous variant.                                                                                                                                                                            | Membership is read term by term, with the `/` and `;` grammar, never by substring.                                                         |
| One 2-star, ~1%-in-Europeans risk-factor allele sets its gene's whole threshold two orders of magnitude above a Mendelian one.                                                                                                                                                                                                                       | The DAFT gate rejects any record carrying a non-ACMG tail rather than ranking which tails disqualify.                                      |
| Widening the density term moved two complement genes from 156 to 248 and from 107 to 151 records.                                                                                                                                                                                                                                                    | The density follows the wide pool, not the DAFT's narrow gate.                                                                             |
| On one gene, every one of the 49 coding transcripts in both annotation sets carries the assessed exon exactly and each omitter is non-coding; reading the tier off the unfiltered inventory would move a nonsense variant from +6.0 to +3.0 — Likely Pathogenic to VUS-mid.                                                                          | SM18's admission filter runs before either tier.                                                                                           |
| Another gene has a curated RefSeq transcript stopping short of the assessed exon, so it fails the admission rule and reaches its tier by a later SM18 section.                                                                                                                                                                                       | An empty spans-but-skips group licenses nothing.                                                                                           |

## Appendix B: the predictor-policy evidence

Neither source below is SM6's approved list; that list is a precondition on the choice, not a reason for it.

**Default: BayesDel, no-allele-frequency flavour.** The flavour is forced, not preferred: ClinGen calibrated the
no-allele-frequency build — SM6's figure labels the row bare "BayesDel" — and there is no GRCh38 build of the
allele-frequency one at all.

A VCGS/MCRI poster (Ciotta, De Fazio, Lunke, Stark; unpublished, no DOI) scored 14,475 ClinVar missense variants against
one laboratory's internal classifications:

| Predictor     | P/LP correct | B/LB correct |
| ------------- | ------------ | ------------ |
| BayesDel      | 79%          | 64%          |
| REVEL         | 76%          | 56%          |
| VARITY_R      | 76%          | 66%          |
| AlphaMissense | 65%          | 57%          |
| MutPred2      | 64%          | 13%          |

**Override: AlphaMissense for PKD1.** The same poster's per-gene pathogenic-prediction rates put BayesDel worst on PKD1
at 38%, against AlphaMissense 46%, REVEL 50%, VARITY_R 62% and MutPred2 66%; and a medRxiv PKD1 preprint finds
AlphaMissense the only one of five tools to misclassify no truth-set variant. Neither source shows AlphaMissense is
*good* on PKD1 — only less bad than the default, with errors that are omissions rather than wrong calls — and BayesDel
is absent from the preprint's comparison, so the two are never measured head to head.

**What these tables cannot settle.** Every figure above is *threshold-applied* accuracy: a predictor's score run through
one cut-point and scored against a label. That conflates two properties SVCv4 keeps apart — how well a score *ranks*
variants (discrimination) and whether its published thresholds sit where the calibration says (calibration) — so a
predictor that ranks well can trail a rival here purely on where its cut-point falls, and vice versa. The threshold-free
comparison is rank agreement, and it is not derivable from these tables: neither source publishes per-variant scores.
This is part of why the poster's own recommendation is gene-specific threshold calibration, and the PKD1 entry is the
interim answer until one exists — the shape SM6 sanctions.

## Appendix C: two derivations, worked

One reading each below rests on a derivation too long for the argument it supports.

**The DAFT formula is not in SM3.** SM3 states the method and the parameters but never the expression, so it is
recovered from the paper and the pilot calculator's own source: monoallelic
`(1/2) * prevalence * hetA * hetG * (1/pen)`, biallelic `sqrt(prevalence) * hetA * sqrt(hetG) * (1/sqrt(pen))`. The
calculator rounds the result to three significant figures. Allelic heterogeneity is a linear numerator term in **both**
branches and never under a root, which is what makes SM3's own worked example erroneous: no reading of the method drops
the term. Licences differ per artefact and decide what may be reused — the paper (PMID 28518168) is **CC BY 4.0**,
redistributable with attribution; the calculator app's source is **LGPL-2.1**, so cite and reimplement but never vendor;
and the paper's *analysis* repository carries no licence at all and does not contain the app.

**A depositor's primary calibration marker changes a criterion.** One TP53 truncating variant reads a benign functional
criterion at Supporting strength from one deposit's **non-primary** calibrations, while that deposit's primary
calibration bins the score nowhere at all — the criterion was an artefact of reading a calibration the depositor did not
put forward. Honouring the primary marker withdraws it, and the coding and protein forms of that variant then agree on a
different deposit's pathogenic Strong result at OddsPath 50.2. Reading calibrations in serialised order instead moved
one BRCA1 score from OddsPath 0.0383 to 0.078 on nothing but array order, and two BRCA1 score sets bin one identical
score at two different strengths.
