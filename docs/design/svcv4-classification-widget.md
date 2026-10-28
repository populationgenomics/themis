# Design: the SVCv4 classification widget

**Related:** [`document-widgets.md`](document-widgets.md) (what a widget is, and the guard rule a curator's judgement
follows); [`evidence-interfaces.md`](evidence-interfaces.md) (the evidence RPCs and `themis.svcv4`, the library that
computes every point); [`curation-surface.md`](curation-surface.md) (the curator's worksheet, whose vocabulary this
widget shares); [`workbench-workspace.md`](workbench-workspace.md) (how a curator's edit becomes a commit);
[`agent-output-rendering.md`](agent-output-rendering.md) (why agent text is drawn as plain text).

## Overview

A classification run ends in an SVCv4 point tally. Each evidence code that the variant's workflow admits receives a
status, a point value, the supporting evidence, and the reasoning that connects them, and the points sum to a total that
falls in a class band. Today the agent writes all of this into the working document as markdown. Those markdown tables
change shape from run to run, the model types every number in them by hand, and a curator has nowhere to record that
they checked a code.

The SVCv4 classification widget replaces that markdown with one typed block defined in
[`svcv4_classification.proto`](../../schema/proto/themis/widgets/models/svcv4_classification.proto). The library that
computes the tally also builds the widget's payload by running the classification itself, so every point, total, and
class that the curator sees comes from the library rather than the model. The agent supplies only what the library
cannot know: the identifiers, each code's evidence and reasoning, and how open the agent considers each call. A code
that the agent does not restate keeps the reasoning that the committed payload holds for it, so a curator's tick on an
untouched code survives the agent's next turn.

The payload lists every code that the paths taken admit, in the reference's fixed order, whether or not the code was
scored. Every code therefore sits in the same place in every run, and a code that the agent never addressed cannot
silently drop out because the library refuses to build a payload that leaves one without a status.

A curator records judgements in three places: a tick and a note on each code, on the routing, and on the record as a
whole. Each judgement is a guard in the sense of [`document-widgets.md`](document-widgets.md), so it clears when the
agent changes what it judges. The agent reads the notes at the start of its next turn and answers them by revising the
record, which clears the note it answered.

The widget uses colour for one thing only: the direction and size of each code's points, because that is what a reader
scans for. Its centrepiece is a points ruler pinned to the top of the widget. The ruler draws the class bands along an
axis, stacks each code's points outward from zero with benign to the left and pathogenic to the right, and places a
needle at the total. Tallies that the library computed under other values of the judgement inputs appear on the same
ruler as further needles, so a curator sees at a glance what would move the variant across a band edge.

## Background

SVCv4 (the ClinGen pilot of ACMG V4) classifies a variant against a monogenic disease entity (MDE: gene, disease, and
mode of inheritance) by summing signed evidence code points into class bands:

```
  total  ≤ -4      -4 < t ≤ -1    -1 < t < 2    2 ≤ t < 4    4 ≤ t < 6    6 ≤ t < 10    ≥ 10
  band   B         LB             VUS-low       VUS-mid      VUS-high     LP            P
```

Two families of codes reach the total differently. The **independent** codes belong to the population (`POP_`), clinical
(`CLN_`), and locus (`LOC_`) families; the framework clamps each independent code to its own range and adds them. The
**variant-type** codes are scored on a **path** through the decision tree that the variant's molecular consequence
selects: a missense variant's amino-acid path sums `MIS_PRD`, `MIS_FXN`, and `MIS_INF`, while a nonsense variant's null
path sums the `NUL_` codes on the null arm. Inside a decision tree, the questions that the variant answers on the way
down select a **decision-tree arm**, which fixes which cells its codes are priced from and what caps them. The splice
tree names its four arms by colour: **yellow** where a splice effect is likely and the resulting frameshift predicts
nonsense-mediated decay (NMD), **orange** where a splice effect is likely and NMD is not predicted, **blue** where the
impact is uncertain, and **violet** where a splice effect is unlikely. Two arms can give a code the same points for
different reasons: a predicted splice effect enters both yellow and orange at +3, because on yellow the transcript is
degraded and on orange the altered product escapes decay.

A path scales its positive predictive points by a **matrix multiplier** built from the mechanism level and the exon
relevance (or exon relevance alone on the missense amino-acid path) and caps its subtotal. When a variant has more than
one candidate path (such as a missense variant with both amino-acid and splice paths), the **max-path rule** counts the
more positive one, so only one path is counted in the tally. Finally, the entity's **gene-disease validity (GDV) gate**
caps the classification by the strength of the gene-disease relationship: Moderate validity caps the outcome at LP,
Limited caps it at VUS, and weaker or disputed validity replaces the class with "Variant in Gene of Uncertain
Significance" or "Do not report". For the full scoring rules and interpretations, see
[`svcv4-interpretations.md`](svcv4-interpretations.md),
[`svcv4-worksheet-transcription.md`](svcv4-worksheet-transcription.md), [`curation-surface.md`](curation-surface.md),
and [`evidence-interfaces.md`](evidence-interfaces.md#the-in-sandbox-library-themissvcv4).

Three concepts from [`themis/svcv4/`](../../themis/svcv4) shape what the widget displays:

- **Per-observation cell ids vs. tree-tier path codes:** Independent codes (such as `CLN_DNV`) price each observation by
  a table row with a stable cell id (`CLN_DNV.specific.unconfirmed`) shared with the curator's worksheet
  ([`curation-surface.md`](curation-surface.md)), whereas variant-type path codes are priced from the decision-tree tier
  and arm selected by the variant.
- **Audit trail and gated classification:** `themis.svcv4` returns a `Classification` containing the audit trail of
  scored lines, the candidate paths, the point total, the class band and VUS sub-band, and the GDV-gated classification.
- **Open judgement inputs and sensitivity tables:** When evidence leaves more than one value standing for a judgement
  input (such as mechanism level or exon relevance), the input is **open** and the run reports the tally under each
  surviving value alongside a **sensitivity** table varying each input across its plausible range.

The working document follows an outline that the kickoff fixes. Its "Evidence assessment" section carries each code's
evidence, cell, and reasoning, and its "Point tally" section carries the audit trail, the total, the class, and the
sensitivity table. Both sections are markdown today.

## Non-goals

- **Computing anything in the client.** Points, totals, bands, and classes arrive in the payload from the library. A
  second implementation in the renderer would be a second place to audit against the ClinGen calculator
  ([`document-widgets.md`](document-widgets.md) §Non-goals).
- **Editing the classification in the widget.** A user may change only guards, so a curator cannot change a point, a
  cell, or a routing decision in the widget. A curator who disagrees says so in a note or in the conversation, and the
  agent reruns the library.
- **Choosing the decision tree.** The ClinGen calculator lets a curator pick the variant type because the curator drives
  it. Here the agent makes the routing call and states its reasons, so the widget shows the one route taken rather than
  offering the others, whose codes would all be empty.

## Design

### One classification, end to end

Consider a missense variant in FBN1, classified against Marfan syndrome, followed from the agent's call to a curator's
note and back.

The agent has assembled its judgement inputs: the gene's calibrated predictor places the variant at +1 on the amino-acid
path, the splice path scores 0, the population frequency code scores 0, the proband is a de novo observation with a
specific phenotype and unconfirmed parentage (+2), and the phenotype's diagnostic yield scores `LOC_PHE` at +4. The
agent passes those inputs to the widget builder in [`themis/svcv4/widget.py`](../../themis/svcv4/widget.py) via the
sandbox helper in
[`themis/services/sandbox_worker/guest/widgets.py`](../../themis/services/sandbox_worker/guest/widgets.py), alongside
the routing metadata, the evidence and reasoning for each code, and two sensitivity alternatives (`parentage confirmed`
and `MIS_PRD at 0`).

The builder runs the main classification and each alternative itself, fills in every point, total, and band from the
results, and checks that the agent's annotations agree with them. The helper writes the payload to `assets/svcv4.binpb`,
and the agent embeds it in the working document with `::embed[assets/svcv4.binpb]`. The curator sees:

<!-- screen-ignore: synthetic example narrative -->

```
┌ SVCv4 classification  draft framework, evaluation only              ☐ Reviewed  ✎ Note ┐
│ Likely pathogenic  +7                                                                 │
│ NM_000138.5:c.7003C>T · FBN1 · Marfan syndrome · autosomal dominant                   │
├───────────────────────────────────────────────────────────────── pinned while scrolling ┤
│                                          2                              1              │
│  B │    LB    │ VUS-low │ VUS-mid │ VUS-high ┆│      LP        │   P ┆                  │
│              0 ▐CLN_DNV▐██LOC_PHE███▐MIS▌                                              │
│ -4        -1  0         2         4         6                10                        │
│ 1 if parentage confirmed +12 P    2 if MIS_PRD at 0 +6 LP                              │
│ 1 of 16 codes reviewed · 2 need review                ☐ Show only codes needing review │
├ Routing ────────────────────────────────────────────────────────── ☐ Reviewed  ✎ Note ┤
│ Variant  NM_000138.5:c.7003C>T  NP_000129.3:p.(Arg2335Trp)                             │
│ Entity   Marfan syndrome MONDO:0007947 · autosomal dominant · loss of function         │
│          ClinGen Gene Validity: Definitive, gate Definitive                            │
│ Paths    amino-acid (MIS_) +1 counted · splice blue (SPL_) 0 not counted               │
├ Population ────────────────────────────────────────────────────────────────────────────┤
│ ▸ POP_FRQ  Population frequency                    │     0   settled       ☑ saved  ✎  │
│ ▸ POP_HMZ  Homozygous observations                      no data            ☐        ✎  │
├ Clinical ──────────────────────────────────────────────────────────────────────────────┤
│ ▾ CLN_DNV  De novo observations                    ▐█   +2   ● leaning      ☐          │
│     ┌ Your note   saved                                                  Edit  Remove ┐ │
│     │ The clinic letter confirms parentage by trio exome.                             │ │
│     └──────────────────────────────────────────────────────────────────────────────────┘ │
│     Cell        1 × CLN_DNV.specific.unconfirmed  SPECIFIC · unconfirmed parentage  +2 │
│     Evidence    paper  A proband with ectopia lentis … carries the variant de novo.    │
│                 case   The referral says the parents have not been tested.             │
│     Why         The phenotype is specific to Marfan syndrome, …                        │
│     Not chosen  CLN_DNV.specific.confirmed, +7. Ruled out: no parental testing         │
│     Confidence  leaning. A trio result would settle it.                                │
│     What moves it  parentage confirmed: +12 Pathogenic                                 │
│   …                                                                                    │
├ Variant effect · amino-acid (MIS_) ───────────────────────────────────────── +1 counted ┤
│ ▸ MIS_PRD  Amino-acid change prediction            ▐    +1   settled        ☐        ✎  │
│   …                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

The curator has ticked `POP_FRQ` and written a note on `CLN_DNV`: "The clinic letter confirms parentage by trio exome."
Each action is a commit that the browser published on the revision shown. On its next turn, the agent's helper prints
the note, and the agent reprices `CLN_DNV` at +7, restates that one code, reruns the builder, and pushes. The new
payload shows `CLN_DNV` at +7 and a total of +12, Pathogenic. `CLN_DNV`'s note judged the old content, so it clears. The
tick on `POP_FRQ` stays because the agent did not restate `POP_FRQ`, the builder carried its reasoning across from the
committed payload, and nothing in `POP_FRQ` changed. Had the curator ticked the whole record, that root sign-off would
clear because the record changed.

### The library builds the payload

The obvious way to produce the payload is for the agent to build the protobuf message itself: call `classify_variant`,
read the result, and copy each total and band into the fields. That approach needs no new code in the library, but it
breaks the guarantee that the widget exists to provide. A model copying numbers into a message will eventually copy one
wrongly, or edit a point by hand when the numbers do not come out as expected, and a payload built that way would look
identical to one built from the calculator.

The library therefore provides a builder beside `classify_variant` in
[`themis/svcv4/widget.py`](../../themis/svcv4/widget.py). The builder takes the same classification inputs, runs the
classification itself, and fills every computed field from the result. It also takes each alternative as a set of inputs
and runs it the same way. Nothing in the sandbox prevents the agent from constructing a raw protobuf message by hand in
code mode, but the skill instructions direct it to call the builder, and a payload produced by the builder guarantees
that every number came from a library tally. The builder also rejects inconsistent annotations: an annotation that sets
a computed field, per-observation cells whose priced sum differs from the points the code contributed to the tally, or a
scored code with no rationale.

Running the classification inside the builder, rather than accepting a finished `Classification` result, matters for a
second reason. A result object records what the tally was, but not every input that produced it: the mechanism level and
the exon relevance survive only as a combined multiplier, and the decision-tree arm that the variant took survives only
in a path's label. Because the builder receives the inputs directly, the routes and paths it writes into the payload
name the arm, the mechanism level, and the exon relevance, and those always match the points beside them.

The payload's CEL validation rules in
[`svcv4_classification.proto`](../../schema/proto/themis/widgets/models/svcv4_classification.proto) enforce the
consistency that the renderer relies on: a scored code carries points and a rationale, while any unscored code carries a
reason why it was not scored; exactly one path is the one counted; and the reported class agrees with the tally. A
revision draws only if its payload passes its schema rules, and a pushed revision must keep drawing later, so tightening
a rule after release would hide every earlier revision that violates it. Validation rules can loosen later but never
tighten, so they are written into the schema from the start.

The entity's required `mode_of_inheritance` field is a rule added after the first release. A record written before that
change stated its mode only in a string field that the schema has since retired, so an older record fails the new rule:
it no longer draws, and reading it back rejects it. We took that break in a single change, which
[`proto.md`](proto.md#schema-evolution) allows where version skew costs nothing real, because the only stored records at
the time were disposable classification runs on dev. The break lasts for good, since nothing rewrites those old dev
records; the run's next build of the widget writes a payload that states its mode of inheritance and loads cleanly.

### A revision carries its reasoning forward

The obvious way for the agent to revise the record is to rebuild the entire payload on every turn, including all
annotations. That approach breaks the curator's ticks. Each turn starts with the agent's scratch files gone, so the
agent would write each code's evidence and reasoning afresh, and prose written twice never produces the exact same
bytes. Because every tick guards its code's reasoning, every tick would clear on every turn, including ticks on codes
that no new evidence touched.

The builder therefore starts from the committed payload. A code that the agent restates receives the new annotation. A
code that the agent omits keeps the annotation from the committed payload, provided everything the library computed for
that code is unchanged; for a code on a path, the path's arm and the judgement inputs that scaled it must also be
unchanged. The builder in [`themis/svcv4/widget.py`](../../themis/svcv4/widget.py) enforces the full set of equality
checks. A clinical code also requires `POP_FRQ` to be unchanged, because the rarity gate reads `POP_FRQ`'s points to
decide whether the clinical codes apply at all. Whenever any of these computed values moves for an omitted code, the
builder refuses the build and names the code, so carried-forward prose never sits beside numbers it was not written
about. In the FBN1 example, after the curator's note:

| Code                                     | Committed | New tally | Restated | The builder                               |
| ---------------------------------------- | --------- | --------- | -------- | ----------------------------------------- |
| `POP_FRQ`                                | 0         | 0         | no       | carries the annotation; the tick stands   |
| `CLN_DNV`                                | +2        | +7        | yes      | takes the new annotation; the note clears |
| `CLN_DNV`, had the agent not restated it | +2        | +7        | no       | refuses: its points moved                 |

Points can also stay put while their meaning changes. A splice predictor code whose path moved from the yellow arm to
the orange arm keeps its +3 and is still refused, because the two arms award +3 for different reasons (§Background). The
routing's annotation carries across while the consequence, the paths taken, and the gate level remain unchanged.

The cost of carrying annotations forward is that stale prose can survive when the agent revises a code's evidence
without changing its points and forgets to restate the code. The skill instructions require the agent to restate every
code whose evidence it touched, and the alternative of rewriting every code on every turn would make curator ticks
worthless.

### Every code the paths taken admit, in the reference's order

The obvious payload lists only the codes that the tally counted. That approach fails in two ways. First, a code that the
agent never assessed would not appear, and a missing row is the hardest omission for a curator to spot. Second, a code's
position would shift from run to run as other codes came and went, which prevents a curator from reading a
classification by spatial memory.

The payload therefore lists every code that the paths taken admit: all independent codes, plus every code of each path
onto which the variant was routed. A nonsense variant on the null arm lists the `NUL_` codes and omits the `CDS_` codes,
and a missense variant with no predicted splice effect has no splice path and omits the `SPL_` codes. Codes appear in
the order that the library's reference declares them, and the client groups them by family without reordering. Each code
carries an assessment status from the curator worksheet's vocabulary: scored, not applicable when a framework
precondition bars the code (such as the rarity gate barring clinical codes for a common variant), or no data when the
code applies and no evidence determines it. The builder refuses a payload that leaves any admitted code without a
status, forcing the agent to account for every code so a reader can distinguish an assessed zero from an unexamined
code.

The cost falls on the agent, which must state a status and a reason for up to twenty codes that it would otherwise have
left out. That accounting is the work that the framework asks of the classification anyway.

### The library's vocabulary

The payload names concepts the way the library does, because the library computes them. A per-observation row uses the
library's cell id. A class uses `themis.svcv4.models.Classification`, the gate level uses the gene-disease RPC's
`GateLevel`, and the molecular consequence uses the evidence contracts' `Consequence` enum. The entity's mode of
inheritance uses the framework's five-member `ModeOfInheritance` enum in
[`svcv4.proto`](../../schema/proto/themis/svcv4/models/svcv4.proto) rather than a curated source's term, because
choosing which source record describes the entity is the agent's call
([`evidence-interfaces.md`](evidence-interfaces.md#goal-is-the-gene-disease-relationship-valid-and-by-what-mechanism)).
A code's assessment status and the agent's confidence in a call live in `svcv4.proto` as well, because a run states both
about each code just as a curator states both about each workflow.

Sharing `svcv4.proto` answers the question that [`document-widgets.md`](document-widgets.md) left open: whether the
widget payload reuses the curation contract's messages in
[`curation.proto`](../../schema/proto/themis/curation/models/curation.proto) or maps onto them. It does neither, and
instead defines its own message in
[`svcv4_classification.proto`](../../schema/proto/themis/widgets/models/svcv4_classification.proto) that shares the
worksheet's vocabulary enums. The worksheet's messages record a curator's selection without points, so a payload built
around them would have nowhere to carry the tally. Sharing the vocabulary gives the run-review loop, which compares a
run against a curator's worksheet, the same statuses and confidences on both sides and the same cell ids for codes
priced per observation.

Codes on a decision-tree path are the remaining vocabulary gap. The worksheet stores a cell id for the decision-tree row
that a curator chose on a path, such as the predictor row of `MIS_PRD`, whereas the library prices a path code from a
tier and names no cell for it. The payload therefore carries the agent's plain-text statement of the cell it read, in
the framework's words, and the run-review loop matches that statement to the curator's cell by reading, just as it
matches a run's stated derivations ([`curation-surface.md`](curation-surface.md) §"One vocabulary per concept"). Adding
a cell id for each path row in the library would close this gap for every consumer at once.

The library's cell descriptions are also terser than the calculator's sentences. The library describes a row in the
shorthand of its transcription ("SPECIFIC · unconfirmed parentage"), whereas the worksheet carries the calculator's full
sentence. The payload carries the library's description so that the widget depends only on what the library knows.

### What a curator records

A curator can record a judgement in three places, each represented by a pair of guards: a tick ("I have reviewed this")
and a free-text note. The record as a whole has one pair, the routing has one, and each code has one. Under the guard
rule ([`document-widgets.md`](document-widgets.md) §"A user's judgement stands only while what it judges is unchanged"),
each guard judges every non-guard field in its enclosing message:

| Where    | A tick or note there clears when the agent changes                                  |
| -------- | ----------------------------------------------------------------------------------- |
| a code   | that code: its status, points, cells, evidence, reasoning, or confidence            |
| routing  | the variant, the consequence, the entity, the paths taken, or the routing rationale |
| the root | anything in the record except another code's or the routing's tick and note         |

A tick never clears a note, and neither clears a judgement elsewhere, because one guard is never part of what another
guard protects. A code's points can move without the agent touching that code directly: for example, changing the
exon-relevance call rescales every predictive code on the path. That code's tick then clears, because the curator
reviewed a different point value.

Because a note is also a guard, it judges the content beside it and clears when the agent changes that content—which
happens as soon as the agent acts on the note. The note then leaves the current revision while remaining in the commit
history, and the skill instructions require the agent to record under the document's open items what each answered note
changed. If a note instead survived the agent's revision, it would sit beside content it was not written about, which is
the stale judgement that the guard rule exists to prevent.

The agent learns of a note by reading the committed payload. Publishing a note does not start a turn automatically,
because a curator working through twenty codes would otherwise start twenty turns. Instead, a curator who has left notes
tells the agent in the conversation, and at the start of the turn the agent calls the sandbox widget helper to print
every tick and note that the committed payload carries.

A note can also arrive while the agent is mid-turn. If the agent's next push changes the code that the note is on, the
agent rebases onto the curator's commit, and rebuilding the file clears the note before the agent has read it. The
sandbox helper therefore prints the text of every note it clears during an update, and the agent treats a note printed
that way just like one it read at the start of a turn.

The client derives whether a code needs review from the payload rather than storing a flag. A code needs review when it
is unticked and either its confidence is leaning or open, or a sensitivity or open-value alternative changes its points
and lands in a different class. The "Show only codes needing review" toggle hides the remaining rows.

### How it is drawn

The React renderer in [`apps/web/src/components/widgets/svcv4/`](../../apps/web/src/components/widgets/svcv4) uses
colour to encode the direction and size of points and nothing else. The obvious alternative gives each code family its
own hue to make families easy to spot. With seven family hues, however, colour no longer distinguishes a benign
contribution from a pathogenic one, and the reds and greens it would consume already mean pathogenic and done elsewhere
in the workbench. The fixed reference order and the family headings make each family easy to find without colour.

The ruler is the widget's centrepiece. It draws the bands from the payload along a points axis, and below them two
stacks starting at zero: codes with negative points stack to the left in the benign colour, and codes with positive
points stack to the right in the pathogenic colour. An award on a path, such as SM7's award for a change at a critical
amino-acid residue, stacks alongside the codes, and a needle marks the total. Where a cap removes points from a
subtotal, the removed points appear hatched over the end of the stack that the cap bounded, showing both what the raw
evidence claimed and what the framework counted. The payload marks which audit-trail lines are caps and which are
awards, because both look like signed numbers to a client. A reduction that the matrix multiplier makes to a single code
appears in that code's row as the raw points and the multiplier factor rather than as hatching on the ruler. For the
FBN1 record, where no cap bounds the codes:

```
                                                2                   1
   LB │ VUS-low │ VUS-mid │ VUS-high  ┆│      LP        │   P    ┆
  ────┼─────────┼─────────┼───────────┼────────────────┼─────────
            0  ▐CLN_DNV▐███LOC_PHE███▐MIS▌
                                        ▲ the tally, +7
  1 if parentage confirmed +12 P    2 if MIS_PRD at 0 +6 LP
```

The band edges come from the payload rather than from client constants, so a record written under one revision of the
framework always draws against that revision's bands. The ruler stays pinned to the top of the widget while the curator
scrolls through the codes; hovering over a segment highlights its code row, and hovering over a row highlights its
segment. Each row also repeats its own bar segment in miniature on the same scale, so a row shows its share of the
balance without the curator looking back up to the ruler.

Each evidence item identifies where it came from. A value served by an evidence RPC names the source and release at
which it was read. A paper passage renders as a clickable citation that opens the paper beside the document, just as in
document prose. A passage from the clinical context is marked as the case's own words, and a web-search excerpt is
marked unverifiable, matching the classification skill's rules. A curator's note renders in the palette that the
workbench reserves for the curator's voice, so it is never mistaken for agent-written text.

Container width governs the layout because the document pane sits inside a resizable split. At narrow widths, a row's
title wraps below its code identifier and the miniature bar segment gives way to the numeric points alone. The widget
maintains a single-column layout at every width.

### Open inputs and sensitivity

The payload's main tally is the total that the codes sum to under the input values on which the document proceeds. Two
lists of alternatives sit beside it, each computed through the library under alternative inputs: the other surviving
values of any input that the agent reports as open, and the sensitivity rows. Each alternative carries a label stating
its assumption and records which codes changed points under that assumption.

The builder sets the record's headline class according to the classification skill's rule: the main tally's class when
every open-input alternative lands in that same class, and "not established" otherwise. If `CLN_DNV`'s parentage were
reported open in the FBN1 example, the two surviving values would land at LP and P, so the record would report "not
established" and the ruler would draw both needles as totals with the contending classes beside them. Sensitivity
needles draw more faintly because they represent what-if checks rather than reported outcomes. Reporting "not
established" expresses an inconclusive run in the same vocabulary as a curator's worksheet, where the same status means
that no single class survives what is known.

### The document around the widget

The widget is the single record of each code: its evidence, its cell, its points, and its rationale. The working
document's "Evidence assessment" section retains the reasoning that spans multiple codes—such as the choice of disease
entity, the mechanism level's rubric sum, and the exon relevance—and embeds the widget where the per-code markdown
tables used to stand. The "Point tally" section retains the narrative interpretation of the sensitivity analysis, while
the numbers themselves live in the widget. Recording each per-code claim only in the widget ensures that the surrounding
document prose cannot drift out of sync with the tally.

## Alternatives considered

- **A widget per code**, embedded wherever each code is discussed. Placing a widget beside each code's prose would keep
  discussion and numbers together, but the ruler needs every code's points in one payload to draw the overall balance, a
  code outside any record would be as easy to leave out as it is today, and a single widget makes the whole
  classification one thing a curator can sign off.
- **Wording from the worksheet's inventory.** The client could render each cell using the calculator's full sentence,
  which the curation module already generates. Doing so would couple the widget renderer to the curation module, and any
  cell added to `themis.svcv4` before the worksheet inventory is updated would have no wording to display.
- **Richer judgements: agree or disagree.** A three-state guard would let a curator mark disagreement on a code without
  writing a note. A bare disagreement flag gives the agent no reason to act on, whereas a free-text note carries both
  the disagreement and its explanation.
- **A curator's judgement on each evidence item.** Placing guards on individual evidence items would be finer-grained
  than guarding each code, but a note on the code can already name the specific evidence item in question, and twenty
  codes with several items each would clutter the widget with more controls than a curator uses. A later message type
  can still add item-level guards if needed, because only an already-released message is barred from gaining new guards.
