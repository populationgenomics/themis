# Design: the SVCv4 worksheet transcription

**Related:** [`curation-surface.md`](curation-surface.md) (the surface these workflows render in — what a worksheet
stores, who sees it, and the run-review loop it is the reference for);
[`evidence-interfaces.md`](evidence-interfaces.md) (`themis.svcv4`, the library whose thresholds and per-observation
prices the transcription is held to, and the calculator oracle); [`svcv4-interpretations.md`](svcv4-interpretations.md)
(the readings applied where the standard contradicts itself, two of which the worksheet mirrors); terms in
[`../../GLOSSARY.md`](../../GLOSSARY.md).

## Overview

The curation worksheet's workflows are the ClinGen Pilot Calculator's, transcribed verbatim with the arithmetic removed.
This doc decides what "verbatim" means where the calculator and the supplements part, what the worksheet derives rather
than asks, and how the transcription is held to the framework.

- **Verbatim, arithmetic removed.** Every title, applicability line, row and note is the calculator's wording; the
  points, totals and class band are dropped, and the mechanism × exon-relevance matrix is drawn as its two axes rather
  than its multipliers.
- **The calculator governs.** It is the source wherever it prints a workflow; two workflows it scores without printing
  come from the supplements, and where a supplement disagrees with the calculator or with itself the disagreement is
  recorded on the workflow it affects.
- **`POP_FRQ`'s row is derived from the library's own thresholds**, and the rarity gate that row triggers is enforced —
  at the cost of the status signal on the nine workflows it bars.
- **The transcription is held to texts the repository does not carry.** Fidelity to the calculator's page and the
  supplements is a manual gate against a local copy; what CI checks is the transcription against `themis.svcv4`, through
  two exported readings.

## Background

Curators know the calculator's workflow view. Reproducing its wording exactly is what makes the worksheet answerable
without a second manual, and what makes an answer comparable to one given in the calculator itself; a paraphrase still
renders and still stores, and is silently a different question.

Three texts state the framework's workflows, and one library states its numbers. The **calculator** is what the pilot's
participants answered through, so it is the source of record. **SM4** and **SM5** — the framework's Supplementary
Materials on clinical observations and on phenotype and segregation — carry the codes the calculator scores without
printing a workflow for. `themis.svcv4` prices every cell and bins every frequency
([`evidence-interfaces.md`](evidence-interfaces.md)), from a transcription of the supplements made independently of the
worksheet's.

A **workflow** is one of the calculator's question blocks; a **cell** is one row of its decision tree, named by a stable
id built from the evidence code and the row. **DAFT** is the disease allele frequency threshold and **FAF** the
variant's gnomAD filtering allele frequency; their ratio bins `POP_FRQ`, and a high enough bin bars four clinical and
locus codes outright — the **rarity gate**.

## Design

### Verbatim, with the arithmetic removed

Each workflow's title, applicability line, row wording and notes are transcribed verbatim, in the calculator's order.
Three things are dropped, all of them arithmetic: the points beside each row, the per-workflow and running totals, and
the derived band. Nothing semantic goes with them: a row's *label* carries the meaning and its points carry only the
weight — `≥68–<82%` is the diagnostic-yield band, `3` is what SVCv4 pays for it.

Two places need a decision rather than a transcription. **The mechanism × exon-relevance matrix is drawn as its two
axes**, never as the multipliers the calculator prints in its cells: those cells *are* the arithmetic. **Which workflows
are on screen is routed, as in the calculator**, by the mode of inheritance and the consequence class — both the
curator's own statement ([`curation-surface.md`](curation-surface.md) §The mode of inheritance is the curator's answer)
— and until each is answered the workflows it selects between are absent, with the page saying what each unanswered axis
leaves off it.

**The reference material is mirrored too.** `POP_FRQ` carries the calculator's two pop-ups — the
maximum-credible-frequency method, and SM3's six DAFT lookup tables with their gnomAD allele counts — since without them
a curator has to leave for the calculator to answer the one workflow that takes numbers rather than a description. Both
are transcribed from the same capture as the workflows, and the thresholds are checked the same way. The thresholds also
have a second reading, the library's own transcription of the same six tables read independently off SM3's images, and a
test joins the two; where SM3 prints a capped `0.05` with a star it defines nowhere, the calculator's uncapped value
governs.

### Components, a pinned version, and rowspans

The calculator's page instantiates a few shared wordings per consequence class — the splice sub-workflow, the matrix —
and each is written once as a component and rendered per class, so the copies cannot drift apart on the first framework
revision. Components rather than a data file driving a generic renderer: the workflows *are* the page, and a generic
renderer is what makes a form look like a generic form.

**A worksheet pins the transcription it was answered against.** A version constant in the module is bumped by hand when
a change alters what an answer *means* — a reworded row, a changed option set, a workflow added or removed — and is
recorded on the worksheet at assignment. Not derived from the build: a version that moves on every deploy pins nothing.
Worksheets in flight keep their version; a comparison across versions is the reader's to notice.

**Rowspans are transcribed, not flattened.** The calculator spans one description over several rows — a criterion over
the variants counted under it, an inheritance pattern over its zygosities — and the transcription carries that
description on each cell it covers, so a row that names only a position in a count ("First LP Variant") still reaches
the curator with its subject. One composition — description, row, the row's own qualifier — is what renders, what the
stored label carries and what the exported cell inventory lists, so the three cannot disagree about what was asked.

### Which source a workflow comes from, and where the framework disagrees with itself

The calculator does not print a workflow for everything it scores, so two come from the supplements: `CLN_CCS`, which
the calculator scores with no table anywhere on its page, and `LOC_SEG`'s non-segregation branch, whose four segregation
tables in the calculator carry only positive co-segregation rows — leaving a curator no way to record the observation
that zeroes both locus codes. Each workflow declares its source, and the declaration is load-bearing (§The rarity gate
is enforced).

Where the supplements state something the calculator cannot, it is recorded where it is enforceable:

- **A value the framework declines to give.** SM4 routes a case-control odds ratio near or below 1.0 to a statistician
  rather than to a number. The row is carried in the library's reference with no points and reported as *unvalued* —
  distinct from a cell nobody mapped, which stays an error — so the curator can record the determination.
- **A value the framework disputes with itself.** SM5's prose recommends a negative award for a non-segregation while
  stating the code's range as starting at zero; the calculator implements the range and the library the prose
  ([`svcv4-interpretations.md`](svcv4-interpretations.md) §Co-segregation). The worksheet's rows take the library's
  reading, so the two do not diverge further.
- **A question the framework leaves open.** SM5 grants benignity for autosomal-recessive non-segregation with
  homozygosity in one section and withholds it from autosomal recessive outright in another. Both readings are in the
  text, so the worksheet offers both rows and the curator records which they took, in the nearest-cell-not-chosen field.

A divergence the transcription turns up that the calculator, the supplements and the oracle's pinned divergences do not
already cover is a question for the author before it is transcribed, not a judgement the transcriber makes.

### The transcription is checked against texts the repository does not carry

Provenance is checked, not asserted: a fidelity test holds every label, and every description a table spans over its
rows, verbatim against the three source texts — the calculator's page and the two supplements — and reports which source
each workflow's wording comes from. Those texts stay out of the repository: one is a capture of a logged-in calculator
page and two are supplements' running text, and what this repository carries of the framework is its code names and its
values, never a supplement's prose. So the check is a manual gate, run against a local copy a maintainer points it at,
in the shape the calculator oracle already takes ([`evidence-interfaces.md`](evidence-interfaces.md)).

What CI runs is everything needing no capture: the module's unit tests, and two exported readings held against the
library in Python — the cell inventory, which says every cell a curator can answer is one the framework prices or
declines to value, and the DAFT tables.

### `POP_FRQ`'s row is derived, from the library's own thresholds

The calculator takes two numbers — the DAFT and the variant's FAF — and derives which of four frequency rows the variant
falls in. The worksheet does the same, prints the multiple above the rows so the curator can check the arithmetic, and
makes the rows unclickable: a curator who disagrees with the row corrects a number. The multiple is not a judgement, so
a selection would not be evidence of one — it would be an opportunity to mis-click, on the one workflow where a
mis-click also changes which other workflows apply.

What would make deriving it unsafe is a second implementation of framework arithmetic with nothing auditing it, and that
does not apply: `themis.svcv4` bins the multiple from the same thresholds, the worksheet declares those multiples beside
the rows whose labels state them, and the exported cell inventory is asserted to *be* the library's bins, in order, at
the library's values. Three properties the two implementations share are pinned rather than assumed: the boundary is
closed to the lower edge (the calculator's `>=` labels govern over SM3's `>`); the comparison is exact rather than
floating-point, since a plain double division puts an ordinary 15× multiple one row below where the library's decimal
arithmetic puts it; and a threshold that is blank, unreadable or zero selects no row, as does a frequency that is blank,
unreadable, negative or above one, with the screen saying which. A frequency of zero is a value, not an absence of one —
the framework asks for 0 where the variant is absent from gnomAD — and bins into the lowest row.

### The rarity gate is enforced, and what that costs

The calculator prints, under `POP_FRQ`, that `CLN_AFF`, `CLN_DNV`, `LOC_PHE` and `LOC_SEG` do not apply when the
frequency scores below −1.0 — the third row and above. The worksheet enforces it: while the frequency bars a workflow,
its status is set to *not applicable* and its status picker locked. The gate rides on the rows rather than on the points
the calculator words it in, since the worksheet holds none: the barring rows are marked in the frequency module, and the
cell-inventory test asserts the marked rows are exactly those the library's reference prices below −1.0. It applies only
where the frequency is *scored* — *no data* and *not applicable* are findings about the frequency, and neither
establishes one to gate on.

**It reaches only the workflows the calculator prints.** Nine transcribed workflows carry the four codes; eight are the
calculator's and one — `LOC_SEG`'s non-segregation branch — comes from SM5, so the note's author was quantifying over a
set that could not contain it. Every row of the calculator's four co-segregation tables awards points toward pathogenic,
which is what a rarity gate exists to stop accumulating, whereas the non-segregation branch awards benignity, agreeing
with the very frequency that fired the gate; barring it would make the worksheet worse than the calculator, where the
observation is unrecordable anywhere. So the gate keys on each workflow's declared source as well as its code, and the
fidelity test holds each declaration against the sources in both directions.

**The cost is a lost signal.** A run is judged on matching the curator's status, and for those nine workflows the status
is computed on both sides, so matching it says nothing. What stays curator-stated is the `POP_FRQ` answer the gate
derives from, and the prose on each barred workflow. Only the status moves: every field already captured stays,
correcting the frequency gives back the status the gate displaced, and that restoration is held for the life of the page
rather than stored. The gate is decided over *every* workflow, never the ones routing has put on screen, and it writes
only where the curator has already answered — so a change of inheritance cannot read as the gate lifting, and no row the
curator never wrote reaches a submission.

## Alternatives considered

- **The workflows as a data file driving a generic renderer.** The indirection's only consumer is the renderer directly
  below it, and a generic renderer is what makes a form look like a generic form.
- **Have the curator select the frequency row.** An unaudited mis-click on the one workflow that also gates four others;
  the derivation is joined to the library, so it is not a second implementation.
- **Derive the matrix multiplier and the totals.** Scoring arithmetic with no statement on this side to pin against — a
  second implementation in the sense the frequency comparison is not.
- **Bar `LOC_SEG`'s non-segregation branch along with the calculator's four co-segregation tables.** Leaves a curator
  unable to record an observation the worksheet built somewhere to record.
- **Leave the rarity gate as prose and score whether the curator applied it.** Keeps the status signal, at the cost of a
  reference that contradicts the framework on its face — a wrong answer retained to measure whether it was wrong.
- **Render the DAFT tables from the library's reference rather than transcribing them.** Drops the allele-count column
  the calculator shows, and loses the second reading that caught the starred cells.

## Open questions

- **Whether a framework-set status should be distinguishable from a curator's in storage.** A stored mark would make the
  rarity gate's restoration durable rather than page-lived, and would give the run-review loop back the signal
  enforcement costs — it could score the statuses the curator chose and ignore the computed ones. It is an additive
  proto field.
