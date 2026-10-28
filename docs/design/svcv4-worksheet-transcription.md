# SVCv4 Worksheet Transcription

**Related:**

- [Curation surface](curation-surface.md) — worksheet layout, blind capture, and run-review integration
- [Evidence interfaces](evidence-interfaces.md) — observation schemas and scoring interfaces in `themis.svcv4`
- [SVCv4 interpretations](svcv4-interpretations.md) — framework ambiguities and decisions in `themis.svcv4`
- [SVCv4 classification widget](svcv4-classification-widget.md) — notebook widget for variant classification

## Overview

The worksheet's cells, row labels, column headers, footnotes, and helper tables are a verbatim transcription of the
ClinGen SVCv4 Pilot Calculator (`svcv4.clinicalgenome.org`), extended from the v4.0 supplements (SM4, SM5) where the
calculator omits a table the framework defines, and holding no points. Curators know the calculator's workflow view:
reproducing its wording exactly is what makes the worksheet ([curation-surface.md](curation-surface.md)) answerable
without a second manual, and what makes an answer comparable to one given in the calculator itself—a paraphrase still
renders and still stores, and is silently a different question.

The worksheet transcribes its 60 workflows in
[`apps/web/src/curation/workflows/`](../../apps/web/src/curation/workflows/): 58 directly from the ClinGen Pilot
Calculator plus two from Supplementary Material 4 (SM4) and Supplementary Material 5 (SM5) where the calculator offers
only a blank numeric score box (`CLN_CCS` and the non-segregation branch of `LOC_SEG`). All point weights, category
subtotals, classification bands, and matrix multipliers are omitted from the surface so curators score each observation
on its merits rather than toward a target threshold. Because workflows vary in shape—from radio tables and integer
counters to a two-axis phenotype-specificity selector—each workflow is written as a plain React component rather than
interpreted from a declarative schema, and each worksheet pins a manually bumped transcription version string
([`version.ts`](../../apps/web/src/curation/version.ts)) so historical submissions keep the version stamp they were
opened under.

The worksheet computes only two mechanical consequences of the curator's inputs: in `POP_FRQ`, typing a disease allele
frequency threshold (DAFT) and a filtering allele frequency (FAF) highlights the corresponding derived
relative-frequency row across four contiguous bins (`<(1.5x of DAFT)`, `>=( 1.5x of DAFT ) - <(5x of DAFT )`,
`>=(5x of DAFT - <(15x of DAFT)`, and `>=(15x of DAFT)`); and when `POP_FRQ` is assessed in either of the two upper bins
(`≥5×` or `≥15× DAFT`), the worksheet enforces the calculator's rarity-gate note by locking the status of the eight
calculator-sourced clinical and locus workflows (`CLN_AFF`, `CLN_DNV`, `LOC_PHE`, and `LOC_SEG`) to *not applicable*.
Enforcing that note in the browser costs the independent status signal on those eight workflows, because leaving the
note as prose would retain a reference answer that contradicts the framework on its face. Transcription fidelity is
pinned by three test suites: [`fidelity.test.ts`](../../apps/web/src/curation/workflows/fidelity.test.ts) against local
captures of the calculator and supplements, and
[`test_cell_inventory.py`](../../themis/curation/tests/test_cell_inventory.py) and
[`test_daft_tables.py`](../../themis/curation/tests/test_daft_tables.py) against [`themis/svcv4/`](../../themis/svcv4/)
in CI.

## Background

During the SVCv4 pilot, human curators work from two primary documents: the ClinGen Pilot Calculator (an interactive
HTML page that presents the framework's evidence codes as fill-in tables) and the numbered Supplementary Materials
(SM1–SM18) that define the underlying rules and reference tables.

The calculator organizes the 23 SVCv4 evidence codes into 30 self-contained question blocks, which the curation surface
calls *workflows*. A single evidence code can split into more than one workflow when the calculator prints separate
tables for different modes of inheritance (for example, `POP_HMZ` has one table for autosomal dominant inheritance and
another for autosomal recessive or X-linked inheritance) or for different variant classes (for example, `EXP_FUN` has
separate tables for splicing assays and general functional assays). Within each workflow, each decision-tree leaf—a
radio option, a numeric counter, or a grid cell—is a *cell* with a stable key within that workflow.

For population frequency (`POP_FRQ`), Supplementary Material 3 (SM3) compares two allele frequencies:

- The *disease allele frequency threshold* (DAFT) is the highest allele frequency in a reference population compatible
  with the condition's prevalence, penetrance, and genetic heterogeneity under its mode of inheritance.
- The *filtering allele frequency* (FAF) is the variant's observed allele frequency in a population database such as
  gnomAD after statistical filtering.

The ratio `FAF / DAFT` places the variant into one of four contiguous relative-frequency bins: `<1.5×`, `1.5×–<5×`,
`5×–<15×`, or `≥15× DAFT` (scoring 0, −1, −3, and −6 points). Because a variant at `≥5× DAFT` is too common in the
general population to cause a rare Mendelian condition on its own, the calculator's *rarity gate* bars `CLN_AFF`,
`CLN_DNV`, `LOC_PHE`, and `LOC_SEG` whenever `POP_FRQ` scores below `−1.0` at `≥5× DAFT` or above (see
[`svcv4-interpretations.md`](svcv4-interpretations.md#frequency-which-figure-and-what-disqualifies-one) and
[`evidence-interfaces.md`](evidence-interfaces.md#the-in-sandbox-library-themissvcv4)).

Separately from the web worksheet, [`themis/svcv4/`](../../themis/svcv4/) encodes the same framework in Python for
automated and agent-assisted classification ([evidence-interfaces.md](evidence-interfaces.md),
[svcv4-interpretations.md](svcv4-interpretations.md)). Where the calculator and the supplements disagree, or where the
supplements contradict themselves, the worksheet and the Python library align their cell representations so their
outputs can be compared directly during run review.

## Non-goals

- **Building a point-scoring calculator.** The worksheet captures which evidence cells apply and why
  ([curation-surface.md](curation-surface.md)); it never displays point values, category subtotals, or a final
  classification band.
- **Committing the pre-publication source documents to the repository.** The ClinGen Pilot Calculator HTML and
  Supplementary Materials PDF are pre-publication artifacts and are not stored in the repository; only the structured
  transcription and its structural digests live in source control.
- **Resolving clinical questions through automated inference.** Aside from selecting the `POP_FRQ` bin from the two
  frequencies the curator types and enforcing the rarity gate, the worksheet performs no automated interpretation of
  clinical or molecular data.

## Design

### Transcribing the calculator without its arithmetic

A straightforward way to build a curation form for SVCv4 would be to design clean form controls around the domain
concepts in `themis.svcv4`. That approach fails as a human benchmark: if a human curator and `themis.svcv4` disagree on
a variant, we need to know whether they read the evidence differently or whether our reworded form steered the curator
toward a different answer than the ClinGen Pilot Calculator would have.

The worksheet therefore transcribes the ClinGen Pilot Calculator verbatim—headings, applicability lines, row labels,
bracketed caveats, footnotes, and table structure—while stripping every number that expresses a score. Point columns,
point-range headers, running totals, and classification bands are removed. Where Supplementary Material 18 (SM18)
combines functional or predictive evidence (`EXP_FUN`, `PRE_SPL`, `PRE_MIS`, `PRE_IND`) with phenotype specificity
(`LOC_PHE`) using a grid of point multipliers (`1.0×`, `0.5×`, `0.25×`, `0×`), showing the multiplier grid would leak
the scoring weights. The worksheet replaces the multiplier grid with two independent single-choice selectors—one for the
assay or predictor strength and one for the phenotype specificity—so the curator records the same two observations
without seeing how the framework multiplies them:

```
ClinGen Pilot Calculator (PRE_MIS):
┌──────────────────────────────────┬─────────────────────────────────────────┐
│ REVEL score                      │ Phenotype specificity (multiplier)      │
│                                  │ Highly (1.0x)  Mod (0.5x)  Low (0.25x)  │
├──────────────────────────────────┼─────────────────────────────────────────┤
│ >= 0.932              (4 pts)    │   [ ] 4.0       [ ] 2.0      [ ] 1.0    │
│ [0.773, 0.932)        (2 pts)    │   [ ] 2.0       [ ] 1.0      [ ] 0.5    │
│ [0.644, 0.773)        (1 pt)     │   [ ] 1.0       [ ] 0.5      [ ] 0.25   │
└──────────────────────────────────┴─────────────────────────────────────────┘

Worksheet transcription (pre_mis):
┌────────────────────────────────────────────────────────────────────────────┐
│ Prediction / score                                                         │
│   ( ) Missense predictor: High score (e.g. REVEL >=0.932)                  │
│   ( ) Missense predictor: Moderate score (e.g. REVEL [0.773, 0.932))       │
│   ( ) Missense predictor: Low score (e.g. REVEL [0.644, 0.773))            │
│   ...                                                                      │
│ Phenotype specificity                                                      │
│   ( ) Highly specific phenotype                                            │
│   ( ) Consistent and moderately specific phenotype                         │
│   ( ) Consistent but low specificity phenotype                             │
│   ( ) Phenotype not consistent OR no phenotype specificity (e.g. "any...") │
└────────────────────────────────────────────────────────────────────────────┘
```

Which workflows appear on screen is routed using the same two inputs as the calculator:

1. **Mode of inheritance**, read from the variant's registration (`AUTOSOMAL_DOMINANT`, `AUTOSOMAL_RECESSIVE`,
   `X_LINKED`, `SEMIDOMINANT`, or `MITOCHONDRIAL`) and fixed for the worksheet.
1. **Consequence class**, chosen by the curator at the top of the worksheet (*Nonsense / frameshift*, *Canonical splice
   (±1,2)*, *Initiation codon*, *Single or multi-exon deletion*, *Missense / in-frame indel*, or *Silent / intronic /
   non-coding*), which shows or hides the consequence-specific workflows (`NUL_JAJ`, `NUL_INF`, `NUL_AIC`, `LOC_HOT`,
   `PRE_MIS`, `PRE_IND`, and `EXP_FUN`'s general-assay table). Leaving the consequence class unset shows every workflow
   that matches the mode of inheritance.

The calculator also links two read-only reference pop-ups from the `POP_FRQ` row. Both are transcribed into
[`apps/web/src/curation/reference/`](../../apps/web/src/curation/reference/) and opened from `<ReferenceModals>` in the
`POP_FRQ` workflow header (neither modal is interactive):

1. **"Use Calculator Approach"** ([`daft-calculator.tsx`](../../apps/web/src/curation/reference/daft-calculator.tsx),
   dialog title *"Determine Maximum Credible Population Allele Frequency"*): displays the transcribed calculator prose
   instructions, the linearised formula
   `Maximum Credible Population Allele Frequency = (Prevalence × Heterogenity) / Penetrance`, and an external link to
   `https://cardiodb.org/allelefrequencyapp/`.
1. **"Use Binning Approach"** ([`daft-binning.tsx`](../../apps/web/src/curation/reference/daft-binning.tsx), dialog
   title *"Disease Allele Frequency Threshold - Binning Approach"*): displays SM3's six DAFT lookup tables (Autosomal
   Dominant, Autosomal Recessive, X-linked Dominant Female, X-linked Dominant Male, X-linked Dominant Combined, and
   X-linked Recessive) with penetrance columns (80%, 50%, 20%) and
   `Equivalent to gnomAD v4 AC of: (based on AN = 1,600,000)`.

Those lookup tables contain starred cells where the calculator's modal and SM3 / `themis.svcv4` diverge (SM3 Tables 2
and 5 cap three `p = 0.25` autosomal-dominant cells at `0.05*` while the calculator's modal prints the uncapped equation
results `0.0632`, `0.1`, and `0.0707`, and nine starred cells in the transcribed DAFT tables differ from the library's
copy by the rounded allele count). Because a curator using the Pilot Calculator sees the calculator's modal verbatim,
the worksheet's reference modal transcribes the calculator's values directly; `themis.svcv4` encodes SM3's values
([`themis/svcv4/data/population.py`](../../themis/svcv4/data/population.py),
[svcv4-interpretations.md](svcv4-interpretations.md)), and the differences are pinned in
[`themis/curation/tests/test_daft_tables.py`](../../themis/curation/tests/test_daft_tables.py).

### Workflow components, version pinning, and spanned descriptions

A tempting way to implement 30 workflows is to define a JSON or protobuf schema for tables, rows, and inputs, and build
a single generic renderer. In practice, the 30 workflows use five distinct interaction shapes (single-choice radio
lists, per-row numeric counters, two-axis SM18 selectors, derived frequency bins, and multi-field proband/segregation
counters), along with table-specific footnotes, bracketed caveats, and conditional cell disabling. A declarative schema
expressive enough to represent every layout quirk would be as complex as the UI code it replaces, while adding a layer
of indirection between the source text and the rendered DOM.

Instead, each workflow is a plain React component in
[`apps/web/src/curation/workflows/`](../../apps/web/src/curation/workflows/) built from a small set of shared
presentational primitives in [`apps/web/src/curation/ui/`](../../apps/web/src/curation/ui/). Because the workflow
definitions live in code rather than in database rows,
[`apps/web/src/curation/version.ts`](../../apps/web/src/curation/version.ts) exports a manually bumped string stamp
(`WORKFLOWS_VERSION`, e.g. `"svcv4-pilot-2026-07.5"`) that is recorded on each worksheet row when created. Any change to
a workflow key, cell key, or rendered wording bumps `WORKFLOWS_VERSION`; bumping it does not migrate worksheets in
flight, so existing worksheets keep the `workflows_version` string they were opened under.

Several calculator tables group adjacent rows under a shared description cell that spans multiple rows (`rowspan`). In
`NUL_INF` (predicted NMD-escaping loss-of-function variants), for instance, the calculator spans the description *"P/LP
variant in this exon for the same MDE predicted to lead transcript to NMD"* across two rows whose own labels are only
*"First LP Variant"* and *"1+ P Variant OR 2+ LP Variants"*. If a cell recorded only its own row label, the stored
[`FieldValue`](../../schema/proto/themis/curation/models/curation.proto) in a submitted assessment would read
`"First LP Variant"` without saying what kind of variant or where it sits, and collapsing the spanned description into a
footnote would strip the condition that distinguishes those rows from the rest of the table.

Each cell under a calculator rowspan therefore carries the spanned text as its `group` alongside its row `label` and
optional `detail` ([`types.ts`](../../apps/web/src/curation/workflows/types.ts)). The UI primitives use `group` to
render a shared section header above contiguous rows that share the same spanned text, and compose the stored
[`FieldValue`](../../schema/proto/themis/curation/models/curation.proto) label and exported cell inventory description
as `group — label — detail`:

```
Worksheet rendering (nul_inf):
┌────────────────────────────────────────────────────────────────────────────┐
│ P/LP variant in this exon for the same MDE predicted to lead transcript    │
│ to NMD                                                                     │
├────────────────────────────────────────────────────────────────────────────┤
│   ( ) First LP Variant                                                     │
│   ( ) 1+ P Variant OR 2+ LP Variants                                       │
└────────────────────────────────────────────────────────────────────────────┘

Stored FieldValue.label / exported cell description:
"P/LP variant in this exon for the same MDE predicted to lead transcript to NMD — First LP Variant"
```

### Where the supplements fill in or disagree with the calculator

The ClinGen Pilot Calculator is the primary source wherever it prints a decision tree. For 28 of the 30 workflows, the
calculator prints the complete table and the worksheet transcribes it directly (`source: "calculator"`).

For the remaining two workflows, the calculator prints only a blank numeric score box and tells the curator to consult
the supplements:

- **`CLN_CCS` (`cln_ccs`)** — case-control evidence, whose decision tree (p-value, effect size, replication, and study
  design) is defined in SM4.
- **`LOC_SEG` non-segregation (`loc_seg_non_segregation`)** — evidence against pathogenicity from non-segregation, whose
  decision tree is defined in SM5 alongside the four calculator-printed co-segregation tables (`loc_seg_ad`,
  `loc_seg_ar`, `loc_seg_sd`, and `loc_seg_xl`).

Both workflows are transcribed from their respective supplements and tagged with `source: "supplement"`. Where the
calculator and the supplements disagree—or where a supplement contradicts itself—the transcription takes an explicit
stance aligned with [`themis/svcv4/`](../../themis/svcv4/) ([svcv4-interpretations.md](svcv4-interpretations.md)):

1. **Cells with no point value (`CLN_CCS.or_near_or_below_1`).** SM4 defines a case-control cell for an odds ratio near
   or below 1.0 in a well-powered study, but assigns it no point value (recommending holistic evaluation instead). The
   row is carried in the library's reference with no points and reported as unvalued—distinct from a cell nobody mapped,
   which stays an error—so the curator can record the determination directly rather than falling back to *nearest
   alternative*.
1. **Disputed direction of points (`LOC_SEG` non-segregation).** SM5's prose (§33) recommends a negative award (`-4.0`
   across `ad`, `ar_homozygous`, and `xl`, and `0.0` in its closing note for autosomal-recessive conditions without
   homozygosity) for a non-segregation while stating the code's range in the summary header and §32 as starting at zero
   (`0 to 4`); the calculator implements the range and the library implements the prose
   ([`svcv4-interpretations.md`](svcv4-interpretations.md#co-segregation)). The worksheet's rows
   ([`loc.tsx`](../../apps/web/src/curation/workflows/loc.tsx)) take the library's reading, so the two do not diverge
   further.
1. **Internal contradiction in SM5 over autosomal-recessive non-segregation.** SM5 grants benignity for
   autosomal-recessive non-segregation with homozygosity in one section (*"Variants observed in homozygosity in
   unaffected individuals (primarily for AR conditions)"*) and withholds it from autosomal recessive outright in an
   explanatory note below the table. Both readings are in the text, so the worksheet offers both rows and the curator
   records which they took in the nearest-cell-not-chosen field.

A divergence the transcription turns up that the calculator, the supplements, and the oracle's pinned divergences do not
already cover is a question for the author before it is transcribed, not a judgement the transcriber makes.

### Checking the transcription against external sources and `themis.svcv4`

Because the ClinGen Pilot Calculator HTML and Supplementary Materials PDF are pre-publication documents, their raw prose
is not committed to the repository. At the same time, every cell key, label, and threshold in the worksheet must stay
faithful both to those external documents and to [`themis/svcv4/`](../../themis/svcv4/).

Verification is split across three test suites corresponding to what lives in the repository:

```
External captures (local only)                 Repository (CI on every run)
┌────────────────────────────┐                 ┌──────────────────────────────┐
│ Pilot Calculator HTML      │──fidelity.test─▶│ apps/web/src/curation/       │
│ Supplementary Materials    │  (on change)    │   workflows/ & reference/    │
└────────────────────────────┘                 └──────────────┬───────────────┘
                                                              │
                                                 cell-inventory.json & DAFT
                                                              │
                                                              ▼
                                               ┌──────────────────────────────┐
                                               │ themis/curation/tests/       │
                                               │   test_cell_inventory.py     │
                                               │   test_daft_tables.py        │
                                               └──────────────┬───────────────┘
                                                              │
                                                              ▼
                                               ┌──────────────────────────────┐
                                               │ themis/svcv4/                │
                                               │   observations.py            │
                                               │   data/population.py         │
                                               └──────────────────────────────┘
```

1. **[`apps/web/src/curation/workflows/fidelity.test.ts`](../../apps/web/src/curation/workflows/fidelity.test.ts)**
   holds every label and every rowspan description verbatim against the three source texts (the Pilot Calculator HTML
   and the SM4 and SM5 supplement texts, pointed to via local paths when present and skipped in CI), checks each
   workflow's declared `source` in both directions, and verifies that stripped point strings do not appear in the
   rendered output.
1. **[`themis/curation/tests/test_cell_inventory.py`](../../themis/curation/tests/test_cell_inventory.py)** checks that
   the `cell_id`s emitted across all workflows (via the checked-in snapshot
   `apps/web/src/curation/workflows/cell-inventory.json`, kept in lockstep with the TypeScript workflow definitions on
   every test run) are the domain of the library's cell reference in
   [`themis/svcv4/observations.py`](../../themis/svcv4/observations.py) (plus `CLN_CCS.or_near_or_below_1`), and asserts
   that the frequency rows marked `barsRarityGatedCodes: true` are exactly those the library's reference prices below
   `-1.0`.
1. **[`themis/curation/tests/test_daft_tables.py`](../../themis/curation/tests/test_daft_tables.py)** checks the
   transcribed DAFT tables against the library's copy in
   [`themis/svcv4/data/population.py`](../../themis/svcv4/data/population.py) and asserts the nine starred cells differ
   by the rounded allele count (along with the three `0.05*` cap differences).

### Deriving the `POP_FRQ` frequency bin and enforcing the rarity gate

Every workflow other than `POP_FRQ` is purely a transcription of the curator's choices. In `POP_FRQ`
([`pop.tsx`](../../apps/web/src/curation/workflows/pop.tsx),
[`frequency.ts`](../../apps/web/src/curation/workflows/frequency.ts)), however, the curator enters two numbers—the
Disease Allele Frequency Threshold (`pop_frq.daft`) and the Variant GrpMax Filtering Allele Frequency
(`pop_frq.faf`)—and the worksheet highlights the matching row across four contiguous relative-frequency bins
(`FRQ_BINS`) that partition `[0, ∞)` with no gap:

1. **`POP_FRQ.bin.lt_1_5x`**: `"Frequency of VBC <(1.5x of DAFT)"` (`minMultiple: 0`, `barsRarityGatedCodes: false`)
1. **`POP_FRQ.bin.1_5x_to_5x`**: `"Frequency of VBC >=( 1.5x of DAFT ) - <(5x of DAFT )"` (`minMultiple: 1.5`,
   `barsRarityGatedCodes: false`)
1. **`POP_FRQ.bin.5x_to_15x`**: `"Frequency of VBC >=(5x of DAFT - <(15x of DAFT)"` (`minMultiple: 5`,
   `barsRarityGatedCodes: true`)
1. **`POP_FRQ.bin.ge_15x`**: `"Frequency of VBC >=(15x of DAFT)"` (`minMultiple: 15`, `barsRarityGatedCodes: true`)

The four rows are rendered by `<DerivedRows>` and are never clickable (there is no manual radio fallback). When a
variant is absent from gnomAD, the curator enters `0` as the transcribed prompt on `pop_frq.faf` instructs (*"If variant
is absent from gnomAD, enter 0. If no GrpMax AF calculated, enter Total AF"*), which is a valid numeric FAF that selects
the first bin (`<(1.5x of DAFT)`).

`evaluateFrequency(daftRaw, fafRaw)` returns a discriminated union of three states:

- **`incomplete`**: either field is blank — no row is highlighted and `<RatioNote>` displays
  `"Enter both numbers and the frequency row follows from them."`
- **`unreadable`**: either field is non-numeric, negative, `> 1`, or `DAFT == 0` — no row is highlighted and
  `<RatioNote>` displays the specific validation error (`reason`).
- **`resolved`**: `0 < daft <= 1` and `0 <= faf <= 1` — computes `multiple = faf / daft`, selects the matching bin from
  `FRQ_BINS` (`>=` at the boundary multiples `1.5`, `5`, and `15`), and reads `barsRarityGatedCodes` (`true` for the
  upper two bins, `multiple >= 5`):

```
Multiple (FAF / DAFT):
  0                  1.5                     5                     15
  ├───────────────────┼──────────────────────┼──────────────────────┼────────────▶
  [ <(1.5x of DAFT)  )[ >=(1.5x) - <(5x)    )[ >=(5x) - <(15x)     )[ >=(15x)
  barsRarityGatedCodes: false                 barsRarityGatedCodes: true
                                              └──────────────────────┬───────────┘
                                                                     │
                                                      POP_FRQ status == ASSESSED
                                                                     │
                                                                     ▼
                                                     Rarity gate locks 8 calculator
                                                     workflows (CLN_AFF, CLN_DNV,
                                                     LOC_PHE, LOC_SEG) to
                                                     status = NOT_APPLICABLE
```

The calculator prints, under `POP_FRQ`, that `CLN_AFF`, `CLN_DNV`, `LOC_PHE`, and `LOC_SEG` do not apply when the
frequency scores below −1.0—the third row (`>=5x of DAFT`) and above. The worksheet enforces that note in
[`rarity-gate.ts`](../../apps/web/src/curation/workflows/rarity-gate.ts). The gate rides on the rows rather than on the
points the calculator words it in, since the worksheet holds none: the barring rows are marked with
`barsRarityGatedCodes: true` in [`frequency.ts`](../../apps/web/src/curation/workflows/frequency.ts),
`isRarityGateClosed()` reads `bin.barsRarityGatedCodes`, and
[`test_cell_inventory.py`](../../themis/curation/tests/test_cell_inventory.py) asserts that the marked rows are exactly
those the library's reference prices below −1.0. Nine transcribed workflows carry the four gated codes—eight from the
calculator (`cln_aff_ad`, `cln_aff_arxl`, `cln_dnv`, `loc_phe`, `loc_seg_ad`, `loc_seg_ar`, `loc_seg_sd`, and
`loc_seg_xl`) and one from SM5 (`loc_seg_non_segregation`). Because non-segregation is benign evidence and does not
exist in the calculator, `barredWorkflowIds` checks both the workflow's evidence code and `source === "calculator"`,
barring only the eight calculator-sourced workflows and leaving `loc_seg_non_segregation` open.

Two details keep the gate predictable as the curator edits in
[`worksheet.tsx`](../../apps/web/src/curation/ui/worksheet.tsx):

- **Only workflows the curator has already touched are persisted as `NOT_APPLICABLE`.** While
  `barred = barredWorkflowIds(ALL_WORKFLOWS, popFrq)` is computed over `ALL_WORKFLOWS` (so `StatusPicker` visually shows
  `"Not applicable"` and locks the status buttons on every visible gated workflow while the gate is active), the
  `useEffect` in `worksheet.tsx` only persists `status: AssessmentStatus.NOT_APPLICABLE` to the database for gated
  workflows that already have a draft entry in `latest.current`. It deliberately does not manufacture draft rows for
  untouched workflows.
- **Un-gating restores prior statuses within the current page session.** `worksheet.tsx` remembers the pre-gate status
  of those touched workflows in `preGateStatus.current` (an in-memory React ref) so that if the curator edits `POP_FRQ`
  back below `5× DAFT` within the same session, their prior status is restored while leaving any cells they already
  filled in intact.

Enforcing the rarity gate in the browser has a real cost: for the eight calculator-sourced workflows carrying the four
gated codes, the worksheet's status is determined mechanically from `POP_FRQ` just as `themis.svcv4` determines it, so
run review loses the independent status signal on whether the curator applied the note. We accept that cost because
leaving the rarity gate as prose keeps the status signal only at the cost of a reference that contradicts the framework
on its face—a wrong answer retained to measure whether it was wrong.

## Alternatives considered

- **Rewriting or simplifying row labels into house vocabulary.** A paraphrase still renders and still stores, and is
  silently a different question: if a human curator and `themis.svcv4` disagree, we could no longer tell whether they
  read the evidence differently or read different forms.
- **Displaying point values, category subtotals, or classification bands.** Showing scores anchors the curator toward a
  target threshold rather than evaluating each observation on its merits ([curation-surface.md](curation-surface.md)).
- **Derive the matrix multiplier and the totals.** That would be scoring arithmetic with no statement on this side to
  pin against—a second implementation in the sense the frequency comparison is not.
- **Leave the rarity gate as prose and score whether the curator applied it.** Keeps the status signal, at the cost of a
  reference that contradicts the framework on its face—a wrong answer retained to measure whether it was wrong.
- **Bar `LOC_SEG`'s non-segregation branch along with the calculator's four co-segregation tables.** Leaves a curator
  unable to record an observation the worksheet built somewhere to record.
- **Render the DAFT tables from the library's reference rather than transcribing them.** Drops the allele-count column
  the calculator shows, and loses the second reading that caught the nine starred cells.
- **Declarative workflow schema with a generic renderer.** Representing workflows as JSON or protobuf descriptors would
  allow Python and TypeScript to read the same file directly, but encoding five interaction shapes, custom per-table
  caveats, and spanned row headers would recreate a bespoke UI DSL inside JSON while making workflows harder to audit
  against the calculator HTML. Exporting `cell-inventory.json` from the TypeScript definitions and joining it against
  `themis.svcv4` in [`test_cell_inventory.py`](../../themis/curation/tests/test_cell_inventory.py) gives the same
  cross-language check without the schema indirection.

## Open questions

- **Should a framework-set status from the rarity gate be distinguishable in storage from a curator-chosen status?** An
  additive field on the `Assessment` proto recording whether `NOT_APPLICABLE` was set by the rarity gate rather than
  chosen directly by the curator would make the gate's restoration durable across page reloads and give the run-review
  loop back the status signal that browser enforcement costs.
