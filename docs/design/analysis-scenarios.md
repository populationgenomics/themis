# Design: Analysis scenarios

**Related:** [`workspace-model.md`](workspace-model.md) (the Analysis entity this shapes);
[`workbench-navigation.md`](workbench-navigation.md) (the pages that render these); [`proto.md`](proto.md) (schema +
serialization); [`frontend-framework.md`](frontend-framework.md) (the BFF that renders the kickoff and persists the
inputs); [`migrations.md`](migrations.md) (the deploy ordering `0008` is destructive against, and the condition that
allows it); [`agent-runtime.md`](agent-runtime.md) (the rest of what a scenario specialises — guiding prompt and what
the run can reach); [`conversation-view.md`](conversation-view.md) (where the kickoff turn is read back).

## Overview

An Analysis is created from a **scenario** and the fields that scenario takes — not from a free-form instruction. The
scenario decides what a curator fills in, what the agent is asked, and how the Analysis is named on every surface that
shows it.

## Background

[`workspace-model.md`](workspace-model.md) already frames the Analysis focus as scenario-dependent: variant-led,
case-led, or a cohort query. A free-form prompt collapsed that distinction into a chat box, at three costs. Nothing a
curator typed could be checked before the run started. Two runs of the same case were only incidentally comparable,
because nothing said they had been asked the same thing. And the sole thing available to name an Analysis by was the
text itself — a clinical vignette whose first sixty characters are not a name.

Two surfaces recur below. The **create composer** is the form on the Project page that starts an Analysis; the **BFF**
is the web tier's backend-for-frontend, which serves that form and creates the agent's session
([`frontend-framework.md`](frontend-framework.md)).

## Non-goals

- **Scenarios beyond the three below.** Cohort-query analyses are in the product frame
  ([`workspace-model.md`](workspace-model.md)), not in this design. Adding one later is additive (§"Identity is
  derived"), so this bounds the doc rather than the platform.
- **Renaming an Analysis.** The name is derived from the inputs, and the inputs are written once (§Storage). A
  mis-entered Analysis is therefore re-created, not corrected: an edit path would need a read-modify-write story for a
  payload deliberately written once, for a row whose only reader is the surface that names it.
- **Resolving the identifiers.** Whether a transcript exists, is versioned, or carries the coding change it is paired
  with — or whether a proband's sample id is in the registry — is the run's business and never the boundary's. That is a
  permanent split, not a gap: resolution needs the data sources the run has and the boundary does not.

## Design

### A scenario is a template; creating an Analysis fills it in

A **scenario** is the platform's unit of specialization ([`PRODUCT.md`](../PRODUCT.md) §4): a named kind of analysis,
specialised on three axes — the guiding prompt, the working document's outline, and what the run can reach. The reach
has two parts, the run's tool surface and the roster of sub-agents the coordinator may delegate to;
[`agent-runtime.md`](agent-runtime.md) names the two separately, as the runtime configures them separately, which is why
its count of what we supply comes to four. The axes have two carriers. The bundle the runtime is configured with carries
the guiding prompt, the tool surface and the roster; the kickoff carries the outline (§"The kickoff text is rendered,
not stored").

What all of those leave open is what a curator supplies for one run, and that is what this doc owns: a scenario is also
a **template**, a fixed set of fields it asks for. The template is not another specialization axis. The axes above say
what the agent is given; the template says what the curator supplies for the run they are applied to, and the guiding
prompt is written against it.

Creating an Analysis instantiates that template. `AnalysisInputs` is the filled-in form, carrying the values and their
template at once: the fields hold what the curator entered, and *which* member of the oneof is set is the scenario they
entered it for. Scenario-iff-inputs is therefore structural. A required oneof over one message per scenario leaves no
enum beside the payload to disagree with the fields, and no way to name a scenario without supplying what it asks for.

### The three scenarios

- **`variant_classification`** — classify one coding variant in a clinical picture.

  - `transcript`, and `hgvs_c`, the coding change against it. Two fields rather than one string, because what resolves
    them takes them apart. A curator does copy the two as one string, and the create composer splits it on the way in.
    Both halves are public nomenclature rather than case-derived, so neither carries anything patient-identifying.
  - `clinical_context`, the one field that must stay prose.

- **`case_analysis`** — diagnose a case from its referral.

  - `proband_sample_id`, the proband as the deployment's sample registry and the VCF header name it. No format rule:
    registries differ per deployment.
  - `clinical_context`, the referral prose the diagnosis starts from.

- **`free_form`** — the escape hatch for work no structured scenario covers yet.

  - `prompt`, what the curator wants done, in their own words.

  It is a scenario rather than a mode beside the scenarios, so every surface reaches it through the same switch and none
  can forget it.

### The boundary checks presence and size, and nothing else

Every field must contain a non-whitespace character. A minimum length would not do: a clinical context of twenty spaces
clears any length check and is not a clinical context. Every field is bounded as well. Each one is stored inline in the
Analysis row and rendered into the instruction the run opens with, so an unbounded field is an unbounded row and an
unbounded number of input tokens. The two rules are orthogonal — a maximum does not reintroduce the whitespace-only hole
— and neither is a resolution: a boundary can refuse a megabyte without knowing whether the accession exists. A *format*
rule is a different matter, and there is none yet (§Open questions).

### Identity is derived, never authored or generated

One module of the web app decides how a scenario presents itself, for every surface — the card, the app bar, the page.
Three things per scenario, and a new scenario adds its case to each — the presentation half of adding one; the rest is
its bundle ([`agent-runtime.md`](agent-runtime.md)) and its kickoff case (§"The kickoff text is rendered, not stored"):

- the **identifying line** — the transcript and coding change, the proband's sample id, or the opening of a free-form
  instruction;
- the **label**, what the scenario is called where a curator picks or reads one ("Variant classification", "Case
  analysis", "Free-form") — the app bar shows it under the identifying line, and the create composer's picker is a list
  of them;
- the **card body**, the prose beneath the identifier — the clinical context, or the whole instruction where free-form
  has no identifier to split from it.

A card is the scenario's whole business, not a shared title-and-subtitle split: free-form has no identifier, so its
instruction fills the card rather than being cut into a heading and the remainder of its own sentence. Every card also
carries when its Analysis was created, whichever scenario it is: re-running a case is the workflow typed inputs exist to
make comparable, so two runs of one variant must not render as the same card, and the creation time is what separates
them.

**A scenario a build predates is a rendered state, not a failure.** Binary proto keeps an unknown oneof member as an
unknown field, so an older reader sees the case as unset, and such an Analysis renders as an unrecognised scenario and
says so. That is what makes adding a scenario additive rather than a retroactive break. Raising instead would cost the
whole Project page, which parses every row server-side, for one Analysis it cannot name, and would make a rollback past
a scenario addition unreadable rather than degraded — the property [`proto.md`](proto.md) §Schema evolution states as "a
reader parses every artifact ever written".

The degradation is a property of the binary encoding, so it covers the column read and every surface the BFF renders
from it, which is every surface that names an Analysis. It does not extend to the browser seam: proto3-JSON is
name-keyed and keeps no unknown field, so an unrecognised member there is dropped or fails the parse. The create
composer is the only client that sends inputs, and a create naming a scenario the server predates is a rejected create,
not a degraded one.

### The kickoff text is rendered, not stored

**The decision.** The BFF renders the scenario and its inputs into the instruction the agent's session opens with, and
stores none of it. That instruction is the *case* and the shape of its answer, not the scenario's guiding prompt: the
guiding prompt says how to approach this kind of analysis and is part of the specialization the session is configured
with ([`agent-runtime.md`](agent-runtime.md)), while the kickoff says which instance to work and, where the scenario
writes a working document, closes with that document's outline — the sections to write, one line each on what a section
holds. The inputs are the only text that varies per run; the outline is fixed per scenario and versioned with the BFF
that renders it. The split with the guiding prompt is by kind: the kickoff owns the section list and what each section
holds; the guiding prompt — the agent's skill document in the runtime-configured bundle — keeps each section's rules:
the wording of the notice, the wording of the reflection questions, the format of a derivation. The curator reads the
outline in the conversation as the run's first turn. What each scenario's kickoff asks for is in the Appendix.

**Why the server renders it.** The scenario contract belongs to the server: a run labelled `variant_classification` was
therefore asked the classification instruction, and that is what makes two runs of one scenario comparable — the point
of typing the inputs at all.

**Why nothing is stored.** The conversation is where what was sent is read
([`conversation-view.md`](conversation-view.md)), so the kickoff already has a record; a durable copy of our own is the
session-mirroring work the workbench needs for the conversation as a whole.

Three qualifications follow.

- **Comparability holds within a kickoff version.** The template and the outline are both fixed per scenario and
  versioned with the BFF; an edit to either changes what later runs are asked, nothing records which version a run got,
  and nothing rewrites what earlier runs were asked. Closing that gap is the session-mirroring work, not a version field
  beside the inputs.
- **This is not a security boundary.** A curator filling in a scenario is expressing intent, exactly as they do steering
  a running session, and `free_form` exists so they can ask for work no template covers. Prompt injection is *outside*
  content redirecting a run away from what its user asked for; a user's own inputs are not that.
- **The composer cannot quietly shorten the instruction.** The text is assembled from typed fields rather than pasted
  through, so a field the create composer stopped sending would change what the agent is asked. The required oneof is
  what keeps that visible: a scenario supplied with a field missing is a rejected create, not a shorter prompt.

### Storage

The Analysis row carries the serialized `AnalysisInputs` in a single binary column — the inline-binary shape of
[`proto.md`](proto.md) §"Protos in Cloud SQL columns", where the whole message is one column's bytes rather than a
spread of typed columns. That shape is normally chosen so a writer that read only part of a payload cannot clobber the
rest; here it is chosen for the schema alone, inputs being written once at create and never modified. The proto is the
schema, and no query reads inside the payload, so JSONB's SQL-inspectability buys nothing against the drift it admits. A
payload carrying a scenario this build predates is returned as it is; naming it is the scenario module's job, above.

`Analysis` and the create request gain `inputs` as a nested `AnalysisInputs` message — the same shape the column
serializes, not an opaque `bytes` each reader re-parses, so the BFF and the create composer share the generated types
and the oneof stays visible on the wire. The free-text `prompt` it supersedes retires in two steps, because the column
and the message field are on different clocks: migration `0008` drops the column, while the two message fields stay
deprecated until the code that still sets one is gone. When they do go, both the field number *and* the field name are
reserved: the browser seam is proto3-JSON ([`proto.md`](proto.md)), where the field name is the wire key, so a later
field reusing the name would break it exactly as reusing the number would.

`0008` is destructive, and the deploy that applies it has a failing window. Both are accepted on one ground: this runs
in a single environment, with no curators on it and no rows worth keeping — the condition
[`migrations.md`](migrations.md) §"How it runs" puts on a destructive migration.

**What `0008` destroys.** It deletes the pre-scenario rows first — and the `session_context` rows referencing them, that
foreign key having no cascade — which is what lets it then add `inputs` as a non-null column with no default, and drop
the free-text one; no other order works. Those rows would have backfilled losslessly into a free-form Analysis, the old
column being non-null and non-blank at the boundary, so the deletion is a choice rather than a forced loss. Their
working-document blobs are left in GCS, keyed by an analysis id nothing now points at.

**What the window breaks.** `pulumi up` rolls Cloud Run to the new image before the migrations run, so the new revision
is live against the old schema until `0008` completes: reads fail on the absent `inputs`, and creates fail on the old
column, which is non-null with no default and which the new code no longer supplies. Both clear when the migrate step
lands. A third failure does not: the free-text field is also one the *browser* sends, so a tab still running the
pre-scenario bundle posts a create carrying that field and no `inputs`, which the new create path has nothing to work
from — it fails whether the unknown key is dropped or rejected. That one clears on a reload. Retiring a browser-sent
field without such a window would take two deploys — stop sending it, then delete it — and the expand/contract shape
otherwise called for here (the create path still writing the old column, the read path tolerating a missing `inputs`,
the drop deferred to a later migration) is a compatibility mode in every reader and writer of a column that exists to be
read one way.

## Alternatives considered

- **A model-generated title, stored at create (the Claude.ai chat-title shape).** The answer while the plan still had
  free-form prompts. One cheap-model call costs a fraction of a cent per Analysis, which is nothing, but it puts a
  network round trip on the create path, adds a failure mode whose only fallback is the prompt excerpt it was meant to
  replace, and produces a name a curator may want to correct — which is a rename RPC, a column, and an affordance. Typed
  inputs make the name derivable, so none of that is needed. (The Managed Agents API offers no titling of its own: a
  session's title is a caller-supplied string, so this would have been hand-rolled regardless.)
- **Free-form removed rather than kept as a scenario.** Structured scenarios cover one kind of work today; removing the
  free-form path would block everything else until its scenario exists.
- **JSONB rather than binary.** Readable in `psql` and queryable in SQL, at the cost of proto3-JSON drift between writer
  and reader for a payload nothing queries.
- **A single `variant` string carrying the full HGVS.** Fewer fields, but every consumer would re-split it, each with
  its own idea of where the boundary is.

## Open questions

- **Nothing will check that a proband's sample id agrees between the VCF and the sample registry.** The boundary
  enforces presence only (§"The boundary checks presence and size"), and there is no interface yet that could check the
  two against each other — the sample-metadata interface is not designed. The intended answer is not a check but a
  different input path: the id supplied by the system, from a registry the curator picks a proband out of, rather than
  typed by hand. Until that exists, a mistyped id is caught by the run, or not at all.
- **No format rule on any field.** The identifiers a curator types are still moving, so pinning a pattern now would
  refuse valid input before the shapes settle. Which fields can carry one, and what it should be, stays open.

## Appendix: what each scenario's kickoff asks for

**A classification asks for "the disease entity or entities", never "the entity".** A gene can map to several
non-mutually-exclusive disease entities — the case SVCv4's SM21 rule exists for — so asking for one presupposes the
count before the run has looked. The same text starts a round of the classifier-evaluation loop, which scores that step
against a reference set, and the kickoff closes with the working document's outline
([`kickoff.ts`](../../apps/web/src/server/kickoff.ts)).

**A case analysis asks the agent to diagnose, not to classify one variant.** Establish the disease frame from the
clinical context, analyze the family's genomic data, classify the variants it prioritizes, and synthesize a case-level
conclusion — classification is a step of the case, not its whole.
