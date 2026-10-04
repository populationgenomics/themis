# Design: the SVCv4 classification widget

**Related:** [`document-widgets.md`](document-widgets.md) (what a widget is, and the guard rule a curator's judgement
follows); [`evidence-interfaces.md`](evidence-interfaces.md) (the evidence rpcs, and `themis.svcv4`, the library that
computes every point); [`curation-surface.md`](curation-surface.md) (the curator's worksheet, whose vocabulary this
widget shares); [`workbench-workspace.md`](workbench-workspace.md) (how a curator's edit becomes a commit);
[`agent-output-rendering.md`](agent-output-rendering.md) (why agent text is drawn as plain text).

## Overview

A classification run ends in an SVCv4 point tally: each evidence code the variant's workflow admits has a status, some
points, the evidence the call rests on and the reasoning from one to the other, and the points sum to a total that falls
in a class band. The agent writes all of this into the working document as markdown today. The tables change shape from
run to run, every number in them was typed by the model, and a curator has nowhere to record that they checked a code.

The SVCv4 classification widget replaces that markdown with one typed block. The library that computes the tally also
builds the widget's payload, by running the classification itself, so every point, total and class the curator sees is
the library's and none is the model's. The agent adds only what the library cannot know: the identifiers, each code's
evidence and reasoning, and how open it considers each call. A code the agent does not restate keeps the reasoning the
committed payload holds for it, so a curator's tick on a code the agent left alone survives the agent's next turn.

The payload lists every code the paths taken admit, in the reference's fixed order, whether or not it was scored. A code
therefore sits in the same place in every run, and a code the agent never addressed cannot silently drop out: the
library refuses to build a payload that leaves one without a status.

A curator records judgements in three places: a tick and a note on each code, on the routing, and on the record as a
whole. Each is a guard in the sense of [`document-widgets.md`](document-widgets.md), so it clears when the agent changes
what it judges. The agent reads the notes at the start of its next turn and answers them by revising the record, which
clears the note it answered.

The widget draws colour for one thing only, the direction and size of each code's points, because that is what a reader
scans for. Its signature is a points ruler pinned to the top of the widget: the class bands along an axis, each code's
points stacked outward from zero, benign to the left and pathogenic to the right, and a needle at the total. Tallies the
library computed under other values of the judgement inputs appear on the same ruler as further needles, so a curator
sees at a glance what would move the variant over a band edge.

## Background

This section introduces the parts of SVCv4 the widget has to show. A reader who knows the framework can skip to
§Non-goals.

SVCv4, the ClinGen pilot of ACMG V4, classifies one variant against one monogenic disease entity (MDE): a gene, a
disease, a mode of inheritance and a mechanism. Its rules are specified in numbered Supplementary Materials, cited here
as SM*n*. Its unit of evidence is the **code**, named by a family and a concept: `POP_FRQ` is the population family's
frequency code, `CLN_DNV` the clinical family's de novo code, `MIS_PRD` the missense family's predictor code. Each code
carries points, positive towards pathogenic and negative towards benign, and the points sum to a total. The total falls
in one of the framework's class bands:

```
  total  ≤ -4      -4 < t ≤ -1    -1 < t < 2    2 ≤ t < 4    4 ≤ t < 6    6 ≤ t < 10    ≥ 10
  band   B         LB             VUS-low       VUS-mid      VUS-high     LP            P
```

Two families of codes reach the total differently. The **independent** codes, the population (`POP_`), clinical (`CLN_`)
and locus (`LOC_`) families, are each clamped to their own range and added. The **variant-type** codes are scored on a
**path** through the decision tree the variant's molecular consequence selects: a missense variant's amino-acid path
sums `MIS_PRD`, `MIS_FXN` and `MIS_INF`; a nonsense variant's null path sums the `NUL_` codes. Inside a tree, the
questions the variant answers on the way down select an **arm**, and the arm fixes which cells its codes are priced from
and what caps them. The splice tree names its arms by colour: yellow where a splice effect is likely and the frameshift
it causes predicts nonsense-mediated decay (NMD), orange where a splice effect is likely and NMD is not predicted, blue
where the impact is uncertain, and violet where it is unlikely. Two arms can give a code the same points for different
reasons: a predicted splice effect enters both yellow and orange at +3, but on yellow the +3 rests on the transcript
being degraded, and on orange on a product that escapes decay. A path scales its positive predictive points by a
**matrix multiplier** built from two judgement inputs, the mechanism level and the exon relevance, and caps its
subtotal. The missense amino-acid path is the exception, scaled by exon relevance alone. A missense variant has two
paths, amino-acid and splice, and the **max-path rule** counts the more positive one. Finally the gene-disease validity
of the entity **gates** the class: Moderate validity caps it at LP and Limited at VUS, and below Limited the gate
replaces the class outright with "Variant in Gene of Uncertain Significance" or, for a disputed relationship, "Do not
report".

The independent codes are priced per observation. `CLN_DNV` scores each de novo proband by the row of a table it falls
in: a proband with a specific phenotype and confirmed parentage scores +7, one with unconfirmed parentage +2. The
library names each row with a stable **cell id**, such as `CLN_DNV.specific.unconfirmed`, and the curator's worksheet
stores the same ids ([`curation-surface.md`](curation-surface.md)). A path's codes are priced differently, from the tier
the agent read off the decision tree, and the library names no cell for them.

The library that does all of this arithmetic is `themis.svcv4` ([`../../themis/svcv4/`](../../themis/svcv4)). The agent
calls it in code mode with its judgement inputs: the tier it read off each decision tree, the mechanism level, the exon
relevance, the points it priced each clinical observation at, and the entity's gate level. It gets back a
`Classification`: an audit trail of lines whose points sum to the total, the paths it scored, the band, the VUS sub-band
and the gated class. A run has two further outcomes beyond one total. A judgement input is **open** when the evidence
leaves more than one value standing, and the agent then reports the tally under each surviving value. A **sensitivity**
table varies each judgement input across its plausible range and reports what each value yields.

The working document follows an outline the kickoff fixes. Its "Evidence assessment" section carries each code's
evidence, cell and reasoning, and its "Point tally" section carries the audit trail, the total, the class and the
sensitivity table. Both are markdown today.

## Non-goals

- **Computing anything in the client.** Points, totals, bands and classes arrive in the payload from the library. A
  second implementation in the renderer would be a second thing to audit against the ClinGen calculator
  ([`document-widgets.md`](document-widgets.md) §Non-goals).
- **Editing the classification in the widget.** A user may change only guards, so a curator cannot change a point, a
  cell or a routing decision there. A curator who disagrees says so in a note or in the conversation, and the agent
  reruns the library.
- **Choosing the decision tree.** The ClinGen calculator lets a curator pick the variant type, because the curator
  drives it. Here the agent made the routing call and states its reasons, so the widget shows the one route taken rather
  than offering the others, whose codes would all be empty.

## Design

### One classification, end to end

Here is a missense variant in FBN1, classified against Marfan syndrome, followed from the agent's call to a curator's
note and back.

The agent has its judgement inputs: the gene's calibrated predictor places the variant at +1 on the amino-acid path, the
splice path scores 0, the population frequency code scores 0, the proband is a de novo with a specific phenotype and
unconfirmed parentage (+2), and the phenotype's diagnostic yield scores `LOC_PHE` +4. It passes them to the library's
widget builder, the same inputs it would pass to `classify_variant`, together with its identifiers, its evidence and
reasoning for each code, and the inputs of two alternatives. As a sketch, which the builder's own documentation replaces
once it exists:

```python
widgets.update('assets/svcv4.binpb', lambda committed: widget.build(
    ref, inputs,                     # consequence, evidence, independent codes, gate level: as for classify_variant
    routing=..., codes={'CLN_DNV': ..., 'POP_FRQ': ..., ...},     # each code's status, evidence and reasoning
    sensitivity=[('parentage confirmed', inputs_with_dnv_confirmed), ('MIS_PRD at 0', inputs_with_prd_0)],
    committed=committed,
))
```

The builder runs the classification and each alternative itself, fills in every point, total and band from the results,
and checks that the agent's annotations agree with them. It then writes the payload, and the agent names it in the
document with `::embed[assets/svcv4.binpb]`. The curator sees:

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
Each is a commit the browser published on the revision shown. On its next turn the agent's helper prints the note, the
agent reprices `CLN_DNV` at +7, restates that one code, reruns the builder, and pushes. The new payload says `CLN_DNV`
+7 and a total of +12, Pathogenic. `CLN_DNV`'s note judged the old content, so it clears. The tick on `POP_FRQ` stays:
the agent did not restate `POP_FRQ`, so the builder carried its reasoning across from the committed payload, and nothing
`POP_FRQ` says changed. The sign-off on the whole record, had the curator given one, would clear, since the record
changed.

### The library builds the payload

The obvious way to produce the payload is for the agent to build the message itself: call `classify_variant`, read the
result, and copy each total and band into the fields. That needs no new code in the library. It breaks on the property
the widget exists to guarantee. A model copying numbers into a message will sooner or later copy one wrongly, or edit a
point by hand when the numbers don't come out as it expected, and a payload built that way would look exactly like one
built right.

So the library has a builder beside `classify_variant`, in [`themis/svcv4/`](../../themis/svcv4). It takes the same
inputs, runs the classification itself, and fills every computed field from the result. It takes each alternative as a
set of inputs too, and runs it the same way. The agent can still construct a message by hand in code mode, since nothing
in a sandbox stops it, but the instructions have it call the builder, and a payload the builder made is one where every
number came from a tally. The builder also refuses what it can check: an annotation that sets a computed field, cells
whose priced sum differs from the points the code reached the tally with, a scored code with no rationale.

Running the classification inside the builder, rather than accepting a finished result, matters for a second reason. A
result object says what the tally was, but not every input that produced it: the mechanism level and the exon relevance
survive only as a multiplier, and the arm of a tree the variant took survives only in a path's label. The builder has
the inputs, so the routes and paths it records name the arm, the mechanism level and the exon relevance, and those
always match the points beside them.

The payload's validation rules state the consistency a reader relies on: a scored code carries points and a rationale,
and any other code a reason instead; exactly one path is the one counted; the class reported agrees with the tally. A
revision draws only if its payload passes its rules, and a pushed revision must keep drawing, so a rule added after the
first release would hide every earlier revision that breaks it. The rules can loosen later but never tighten, so they
are all written with the schema.

### A revision carries its reasoning forward

The obvious way for the agent to revise the record is to rebuild it whole on every turn, annotations included. It breaks
the curator's ticks. A turn starts with the agent's scratch files gone, so it writes each code's evidence and reasoning
afresh, and prose written twice is never the same bytes. Every tick guards its code's reasoning, so every tick would
clear on every turn, including the ones on codes nothing new had touched.

So the builder starts from the committed payload. A code the agent restates gets the new annotation. A code it leaves
out keeps the annotation the committed payload holds, but only while everything the library computed for the code is
unchanged, and for a code on a path, the path's arm and the judgement inputs that scaled it as well. The builder
([`themis/svcv4/widget.py`](../../themis/svcv4/widget.py)) holds the exact list. A clinical code also needs `POP_FRQ` to
be unchanged, because the rarity gate reads `POP_FRQ`'s points to decide whether the clinical codes apply at all. Where
any of these moved, the builder refuses the build and names the code, so prose never sits beside numbers it was not
written about. In the FBN1 example, after the curator's note:

| Code                                     | Committed | New tally | Restated | The builder                               |
| ---------------------------------------- | --------- | --------- | -------- | ----------------------------------------- |
| `POP_FRQ`                                | 0         | 0         | no       | carries the annotation; the tick stands   |
| `CLN_DNV`                                | +2        | +7        | yes      | takes the new annotation; the note clears |
| `CLN_DNV`, had the agent not restated it | +2        | +7        | no       | refuses: its points moved                 |

Points can stay put while their meaning moves. A splice predictor code whose path moved from yellow to orange keeps its
+3 and is still refused, for the reason §Background gives. The routing's annotation carries across while the
consequence, the paths taken and the gate level stay as they were.

The cost is that stale prose can survive: a code whose evidence the agent revised without its points moving keeps the
old reasoning unless the agent restates it. The instructions have the agent restate every code whose evidence it
touched, and the alternative, a rewrite on every turn, would make a tick worthless.

### Every code the paths taken admit, in the reference's order

The obvious payload lists the codes the tally counted. It breaks twice. A code the agent never assessed would not
appear, and a missing row is hard to see, which is exactly the omission a curator most needs to catch. And a code's
position would shift from run to run as others came and went, which defeats reading a classification by where things
are.

So the payload lists every code the paths taken admit: all the independent codes, and every code of each path the
variant was routed onto. A nonsense variant on the null arm lists the `NUL_` codes and not the `CDS_` ones, and a
missense variant with no predicted splice effect has no splice path and lists no `SPL_` codes. They come in the order
the library's reference declares them, and the client groups them by family without reordering. Each carries a status,
in the vocabulary a curator's worksheet uses: scored, not applicable (the framework's own precondition bars the code,
such as the rarity gate barring the clinical codes for a common variant), or no data (the code applies and nothing
determines it). The builder refuses a payload that leaves an admitted code without one, so the agent has to account for
every code, and a reader can tell an assessed zero from a code nobody looked at.

The cost falls on the agent, which must state a status and a reason for up to twenty codes it would otherwise have left
out. That is the work the framework asks of it anyway.

### The library's vocabulary

The payload names things the way the library does, since the library is what computes them. A per-observation row is the
library's cell id. A class is `themis.svcv4.models.Classification`, the gate level the gene-disease rpc's `GateLevel`,
the consequence and inheritance the evidence contracts' enums. A code's status and the agent's confidence in a call are
enums the worksheet used to declare for itself, and they now live beside `Classification` in
[`svcv4.proto`](../../schema/proto/themis/svcv4/models/svcv4.proto), because a run states both about each code as a
curator does about each workflow.

That answers the question [`document-widgets.md`](document-widgets.md) left open, whether the payload reuses the
curation contract's messages or maps onto them. It does neither: it is a message of its own that shares the worksheet's
vocabulary. The worksheet's messages record a curator's selection with no points, and a payload built around them would
have nowhere to carry the tally. Sharing the vocabulary gives the run-review loop, which reads a run against a curator's
worksheet, the same statuses and confidences on both sides, and the same cell ids for the codes priced per observation.

A path's codes are the gap. The worksheet stores a cell id for the decision-tree row a curator chose on a path, such as
the predictor row of `MIS_PRD`, but the library prices a path code from a tier and names no cell for it. The payload
therefore carries the agent's own statement of the cell it read, in the framework's words, and the loop matches that to
the curator's cell by reading, which is how it matches a run's stated derivations in any case
([`curation-surface.md`](curation-surface.md) §"The framework's vocabulary, as used here"). A cell id for each path row
in the library would close the gap for every consumer at once.

The library's vocabulary is also thinner than a reader would like in a cell's wording. The library describes a row
tersely, in the words of its transcription ("SPECIFIC · unconfirmed parentage"), while the worksheet carries the
calculator's full sentence. The payload carries the library's description, because the widget draws what the library
knows.

### What a curator records

A curator can record a judgement in three places, each a pair of guards: a tick, meaning "I have reviewed this", and a
note, the curator's own words. The record as a whole has one pair, the routing has one, and each code has one. Under the
guard rule ([`document-widgets.md`](document-widgets.md) §"A user's judgement stands only while what it judges is
unchanged"), each guard judges everything else in its message:

| Where    | A tick or note there clears when the agent changes                                   |
| -------- | ------------------------------------------------------------------------------------ |
| a code   | that code: its status, points, cells, evidence, reasoning or confidence              |
| routing  | the variant, the consequence, the entity, the paths taken or the routing's rationale |
| the root | anything at all in the record, except another code's or the routing's tick and note  |

A tick never clears a note, and neither clears a judgement elsewhere, because one guard is never part of what another
protects. A code's points can move without the agent touching that code: a changed exon-relevance call rescales every
predictive code on the path. The code's tick clears then, which is right, since the curator reviewed different points.

The note is a guard as well, which has a consequence worth stating. A note judges the content beside it, so it clears
when the agent changes that content, and the agent changes it precisely when it acts on the note. The note then leaves
the current revision. It stays in the history, and the skill has the agent record, under the document's open items, what
each curator answer changed. The alternative, a note that survives the agent's revision, would sit beside content it was
not written about, which is the stale judgement the guard rule exists to prevent.

The agent learns of a note by reading. Nothing starts a turn when a curator publishes, so a curator who has left notes
tells the agent in the conversation, and at the start of each turn the widget helper in the sandbox prints every tick
and note the committed payload carries. Publishing a note does not start a turn itself, because a curator working
through twenty codes would start twenty.

A note can also arrive while the agent is mid-turn. If the agent's next push changes the code the note is on, the agent
rebases onto the curator's commit, and rebuilding the file clears the note before the agent has read it. So the helper
prints the text of every note it clears, and the agent treats a note it meets that way as one it read at the start of a
turn.

"Needs review" is derived, not stored. A code needs review when it is unticked and either its confidence is leaning or
open, or a sensitivity or open-value tally changes its points and lands in a different class. The filter "show only
codes needing review" hides the rest.

### How it is drawn

Colour encodes the direction and size of points and nothing else. The obvious scheme gives each family its own hue,
which makes a family easy to find. It costs the one thing the colour is most needed for: with seven family hues, colour
no longer tells a benign contribution from a pathogenic one, and the reds and greens it would use already mean
pathogenic and done elsewhere in the workbench. The fixed order and the family headings make a family easy to find
without colour.

The ruler is the widget's centre. It draws the bands the payload carries along a points axis, and below them two stacks
that start at zero: the codes with negative points stacked to the left in the benign colour, and those with positive
points stacked to the right in the pathogenic colour. An award on a path, such as SM7's award for a change at a critical
amino-acid residue, stacks with the codes. A needle marks the total. Where a cap took points off a subtotal, the points
it removed show hatched over the end of the stack it bounded, so a reader sees both what the evidence claimed and what
the framework counted. The payload says which lines are caps and which awards, since the two look alike to a client that
sees only a signed number. A reduction the matrix makes to one code shows in that code's row, as the raw points and the
factor, rather than on the ruler. For the FBN1 record, whose codes no cap bounds:

```
                                                2                   1
   LB │ VUS-low │ VUS-mid │ VUS-high  ┆│      LP        │   P    ┆
  ────┼─────────┼─────────┼───────────┼────────────────┼─────────
            0  ▐CLN_DNV▐███LOC_PHE███▐MIS▌
                                        ▲ the tally, +7
  1 if parentage confirmed +12 P    2 if MIS_PRD at 0 +6 LP
```

The band edges come from the payload rather than from the client, so a record written under one revision of the
framework draws against that revision's bands. The ruler stays pinned to the top of the widget while the curator scrolls
through the codes, and hovering a segment highlights its code's row, and the other way round. Each row repeats its own
segment in small, on the same scale, so a row reads as its share of the balance without the curator looking up.

Each evidence item says where it came from. A value an evidence rpc served names the source and release it was read at.
A passage of a paper is a citation the curator can click to open the paper beside the document, as in prose. A passage
of the clinical context is marked as the case's own words, and a web-search excerpt is marked unverifiable, as the skill
already requires. A curator's note is drawn in the colours the workbench uses for the curator's own voice, so it is
never mistaken for the agent's text.

Container width decides the layout, because the document pane is a resizable split. At a narrow width a row's title
moves below its code and the per-row segment gives way to the points alone. The layout holds a single column at every
width.

### Open inputs and sensitivity

The payload's main tally is the one the codes sum to, under the values the document proceeds on. Two lists of
alternatives sit beside it, each a tally the agent computed through the library under other inputs: the other surviving
values of an input the agent reports open, and the sensitivity rows. Each alternative is labelled with the assumption it
makes and records which codes' points it changed.

The class the record reports is the builder's, by the skill's rule: the main tally's class where every open value's
class agrees with it, and "not established" otherwise. With CLN_DNV's parentage reported open in the FBN1 example, the
two values land at LP and P, so the record would report no class, and the ruler would draw both needles as totals with
the classes in contention beside them. Sensitivity needles are drawn fainter, since they are what-ifs rather than
reported outcomes. "Not established" is then a run's finding in the same vocabulary as a curator's, since both mean that
no single class survives what is known.

### The document around the widget

The widget is the record of each code: its evidence, its cell, its points and why. The working document's "Evidence
assessment" section keeps what spans codes, such as the choice of entity, the mechanism level's rubric sum and the exon
relevance, and embeds the widget where the per-code tables stood. The "Point tally" section keeps the reading of the
sensitivity table, whose numbers are in the widget. Writing each per-code claim once means the document cannot disagree
with the widget about it.

## Alternatives considered

- **A widget per code**, embedded where each code is discussed. The prose and the code would sit together. It lost
  because the ruler needs every code's points to draw the balance, a code outside any record would be as easy to leave
  out as it is today, and one record makes the whole classification one thing a curator can sign off.
- **Wording from the worksheet's inventory.** The client could draw each cell with the calculator's sentence, which the
  curation module already generates. It lost because the widget would then depend on the curation module, and a cell the
  library adds before the worksheet does would draw with no wording.
- **Richer judgements: agree or disagree.** A three-state guard would let a disagreement show in the widget without a
  sentence. It lost because a bare disagreement gives the agent nothing to act on, and the note carries the same signal
  with its reason.
- **A curator's judgement on each evidence item.** Finer grained than a code. It lost because the note on the code
  already reaches the item, and twenty codes with several items each would put more controls on the screen than a
  curator would use. A later message type can still add it: only a released message can never gain a guard.
