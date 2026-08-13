# Design: Analysis scenarios

**Status:** draft **Related:** [`workspace-model.md`](workspace-model.md) (the Analysis entity this shapes);
[`workbench-navigation.md`](workbench-navigation.md) (the pages that render these); [`proto.md`](proto.md) (schema +
serialization); [`frontend-framework.md`](frontend-framework.md) (the BFF that renders the kickoff and persists the
inputs); [`migrations.md`](migrations.md) (the deploy ordering `0008` is destructive against, and the condition that
allows it); [`agent-runtime.md`](agent-runtime.md) (the rest of what a scenario specialises — guiding prompt, outline,
tool surface).

## Overview

An Analysis is created from a **scenario** and the fields that scenario takes — not from a free-form instruction. The
scenario decides what a curator fills in, what the agent is asked, and how the Analysis is named on every surface that
shows it.

## Background

[`workspace-model.md`](workspace-model.md) already frames the Analysis focus as scenario-dependent: variant-led,
case-led, or a cohort query. A free-form prompt collapsed that distinction into a chat box. Nothing a curator typed
could be checked before the run started, two runs of the same case were only incidentally comparable, and the sole thing
available to name an Analysis by was the text itself — a clinical vignette whose first sixty characters are not a name.

## Non-goals

- **Scenarios beyond the two below.** Case-led and cohort-query analyses are in the product frame, not in this design.
- **Renaming an Analysis.** The name is derived from the inputs, and the inputs are written once (§Storage). A
  mis-entered Analysis is therefore re-created, not corrected: an edit path would reopen the read-modify-write question
  that section closes, for a row whose only reader is the surface that names it.
- **Resolving or validating the identifiers.** Presence is enforced; whether a transcript exists, is versioned, or
  carries the coding change it is paired with is the run's business, not the boundary's.
- **Persisting the kickoff text.** See below — the conversation carries what was sent.

## Design

### A scenario is a template; the oneof is the instance

A **scenario** is the platform's unit of specialization ([`PRODUCT.md`](../PRODUCT.md) §4): a named kind of analysis,
supplying the guiding prompt, the working-document outline, and the tool surface the shared substrate is specialised
with ([`agent-runtime.md`](agent-runtime.md)). What those leave open is what a curator supplies for one run of it, and
that is what this doc owns: a scenario is also a **template**, a fixed set of fields it asks for. The template is not a
fourth specialization axis. PRODUCT §4's three say what the agent is given; the template says what the curator supplies
for the run those three are applied to, and the guiding prompt is written against it.

Creating an Analysis instantiates that template. `AnalysisInputs` is the filled-in form, carrying the values and their
template at once: the fields hold what the curator entered, and *which* member of the oneof is set is the scenario they
entered it for.

Scenario-iff-inputs is therefore structural. A required oneof over one message per scenario leaves no enum beside the
payload to disagree with the fields (the shape `ConversationEvent.kind` already uses), and no way to name a scenario
without supplying what it asks for.

- **`variant_classification`** — `transcript` (a RefSeq accession with its version), `hgvs_c` (the coding change against
  it), `clinical_context`. The pair is stored apart because what resolves them takes them apart, even though a curator
  copies `NM_001382309.1:c.332del` (public nomenclature, not case-derived) as one string and the composer splits it.
  `clinical_context` is the one field that must stay prose.
- **`free_form`** — `prompt`. The escape hatch for work no structured scenario covers yet. It is a scenario rather than
  a mode beside the scenarios, so every surface reaches it through the same switch and none can forget it.

Each field is checked non-blank at the boundary by a CEL rule, not `min_len`: a whitespace-only clinical context passes
a length check and is not a clinical context. Every field is bounded as well, because every one of them is stored inline
in the row and rendered into the agent's opening instruction, so unbounded text is an unbounded row and unbounded input
tokens: 10 000 characters for the two prose fields, 255 for `transcript` and `hgvs_c`, which are short by construction.
Bounding an identifier is not resolving it — a boundary can refuse a megabyte without knowing whether the accession
exists. The rules are orthogonal: a maximum does not reintroduce the whitespace-only hole.

### Identity is derived, never authored or generated

One module (`lib/scenario.ts`) decides how a scenario presents itself, for every surface — the card, the app bar, the
page. Three things per scenario, and a new scenario adds its case to each — the presentation half of adding one, the
guiding prompt and outline and tool surface being the rest ([`agent-runtime.md`](agent-runtime.md)):

- the **identifying line** — `<transcript>:<hgvs_c>`, or the opening of a free-form instruction;
- the **label**, what the scenario is called where a curator picks or reads one ("Variant classification", "Free-form")
  — the app bar shows it under the identifying line, and the composer's picker is a list of them;
- the **card body**, the prose beneath the identifier — the clinical context, or the whole instruction where free-form
  has no identifier to split from it.

A scenario a build *predates* is a rendered state, not a failure. Proto keeps an unknown oneof member as an unknown
field, so an older reader sees the case as unset — which is what makes adding a scenario additive rather than a
retroactive break. Such an Analysis renders as an unrecognised scenario and says so. Raising instead would cost the
whole Project page, which parses every row server-side, for one Analysis it cannot name, and would make a rollback past
a scenario addition unreadable rather than degraded — the property [`proto.md`](proto.md) §Schema evolution states as "a
reader parses every artifact ever written".

That degradation is a property of the binary encoding, so it covers the `bytea` read and every surface the BFF renders
from it — which is every surface that names an Analysis. It does not extend to the browser seam: proto3-JSON is
name-keyed and keeps no unknown field, so an unrecognised member there is dropped or fails the parse. The composer is
the only client that sends inputs, and a `CreateAnalysis` naming a scenario the server predates is a rejected create,
not a degraded one.

A card is the scenario's whole business, not a shared title-and-subtitle split: free-form has no identifier, so its
instruction fills the card rather than being cut into a heading and the remainder of its own sentence. The creation time
sits under both and is the page's, not the scenario's — re-running a case is the workflow typed inputs exist to make
comparable, so two runs of one variant must not render as the same card.

### The kickoff text is rendered, not stored

`server/kickoff.ts` renders the scenario and its inputs into the instruction the agent's session opens with. That is the
*case*, not the scenario's guiding prompt: the guiding prompt says how to approach this kind of analysis and is part of
the specialization the session is configured with ([`agent-runtime.md`](agent-runtime.md)), while the kickoff says which
instance to work and is the only text that varies per run.

It is server-side because the scenario contract belongs to the server: a run labelled `variant_classification` was
therefore asked the classification instruction, which is what makes two runs of one scenario comparable — the point of
typing the inputs at all. That holds within a template version. An edit changes what later runs are asked and nothing
records which version a run got, so closing the gap is the session-mirroring work below rather than a field beside the
inputs.

It is not a security boundary. A curator filling in a scenario is expressing intent, exactly as they do steering a
running session, and `free_form` exists so they can ask for work no template covers. Prompt injection is outside content
redirecting a run away from what its user asked for; a user's own inputs are not that.

It is not persisted. The conversation is where what was sent is read, and a durable copy of it is the session-mirroring
work, which the workbench needs regardless. Until that lands, the record of a past run's exact kickoff is Anthropic's
event log alone; a template edit changes what later runs are asked, and nothing rewrites what earlier ones were.

### Storage

`analyses.inputs` is the serialized `AnalysisInputs` as `bytea` — the inline-binary shape of [`proto.md`](proto.md)
§"Protos in Cloud SQL columns", taken for the schema rather than for RMW-safety, since inputs are written once at create
and never modified. The proto is the schema, and no query reads inside the payload, so JSONB's SQL-inspectability buys
nothing against the drift it admits. A payload carrying a scenario this build predates is returned as it is — naming it
is the scenario module's job, above.

`Analysis` and `CreateAnalysisRequest` gain `inputs` as a nested `AnalysisInputs` message — the same shape the column
serializes, not an opaque `bytes` each reader re-parses, so the BFF and the composer share the generated types and the
oneof stays visible on the wire. `analyses.prompt` and the `prompt` fields of both messages retire with it. Both numbers
*and* names are reserved: the browser seam is proto3-JSON ([`proto.md`](proto.md)), where the field name is the wire
key, so a later field reusing the name would break it exactly as reusing the number would.

## Alternatives considered

- **A model-generated title, stored at create (the Claude.ai chat-title shape).** The answer while the plan still had
  free-form prompts. One `claude-haiku-4-5` call costs ~$0.0009 per Analysis, which is nothing, but it puts a network
  round trip on the create path, adds a failure mode whose only fallback is the prompt excerpt it was meant to replace,
  and produces a name a curator may want to correct — which is a rename RPC, a column, and an affordance. Typed inputs
  make the name derivable, so none of that is needed. (The Managed Agents API offers no titling of its own: a session's
  `title` is a caller-supplied string, so this would have been hand-rolled regardless.)
- **Free-form removed rather than kept as a scenario.** Structured scenarios cover one kind of work today; removing the
  free-form path would block everything else until its scenario exists.
- **JSONB rather than binary.** Readable in `psql` and queryable in SQL, at the cost of proto3-JSON drift between writer
  and reader for a payload nothing queries.
- **A single `variant` string carrying the full HGVS.** Fewer fields, but every consumer would re-split it, each with
  its own idea of where the boundary is.

## Implementation state

Built on the `wb-nav-build` branch, unmerged, alongside the routes
([`workbench-navigation.md`](workbench-navigation.md)): the proto and generated stubs, migration `0008_analysis_inputs`
(which drops `prompt`, adds `inputs`, and deletes pre-scenario rows and the `session_context` rows referencing them,
since that FK has no cascade; their working-document blobs are left in GCS, keyed by an analysis id nothing now points
at), the fixture seeds, `lib/scenario.ts`, `server/kickoff.ts`, and the composer's scenario picker.

A pre-scenario row would backfill losslessly into `free_form{prompt}` — `analyses.prompt` is `text NOT NULL` and
non-blank at the boundary — so the deletion is a choice, not a forced loss: those rows exist only in dev, which is
rebuilt from main's migration lineage, and the alternative is hand-encoding an `AnalysisInputs` payload in SQL.

The deploy window is accepted on the same grounds. `pulumi up` rolls Cloud Run to the new image before the migrations
run ([`migrations.md`](migrations.md) §"How it runs"), so the new revision is live against the old schema until `0008`
completes: reads fail on the absent `inputs`, and creates fail on `prompt`, which is `NOT NULL` with no default and
which the new code no longer supplies. Those two clear when the migrate step lands. A third does not: `prompt` is also a
field the *browser* sends, so a tab still running the pre-scenario bundle posts a `CreateAnalysisRequest` naming a field
the schema now reserves, and fails the required-`inputs` rule whether the unknown key is dropped or rejected. Reserving
the name governs reuse, not the transition. That one clears on a reload, and retiring a browser-sent field without one
would be two deploys — stop sending it, then delete it. The expand/contract shape that window normally calls for — the
create path still writing `prompt`, the read path tolerating a missing `inputs`, the drop deferred to a later migration
— would put a compatibility mode in every reader and writer of a column that exists to be read one way, for the one
environment this runs in, which has no curators on it and no rows worth keeping.
