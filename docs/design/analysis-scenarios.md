# Design: Analysis scenarios

**Related:** [`workspace-model.md`](workspace-model.md) (the Analysis entity this shapes);
[`workbench-navigation.md`](workbench-navigation.md) (the pages that render these); [`proto.md`](proto.md) (schema and
binary serialization); [`frontend-framework.md`](frontend-framework.md) (the BFF that renders the kickoff and persists
the inputs); [`migrations.md`](migrations.md) (the deploy ordering that migration `0008` runs under);
[`agent-runtime.md`](agent-runtime.md) (the guiding prompt and tool surface that a scenario also specializes);
[`conversation-view.md`](conversation-view.md) (where the kickoff turn is read back).

## Overview

In the workbench, an Analysis can investigate a single coding variant, a family case referral, or an exploratory
question ([`workspace-model.md`](workspace-model.md)). Starting every Analysis from a single free-form prompt box left
required identifiers unchecked before a run began, made two runs of the same case comparable only by coincidence, and
forced every UI surface to title an Analysis using the opening characters of a clinical vignette.

This design replaces the free-form prompt with typed **scenarios**. Creating an Analysis fills in a scenario template,
represented on the wire and in Cloud SQL as a protobuf `oneof` (`AnalysisInputs` in
[`workbench.proto`](../../schema/proto/themis/workbench/models/workbench.proto)). When the backend-for-frontend (BFF)
creates an Analysis, it persists `AnalysisInputs` as a single binary protobuf column and renders the opening user turn
for the Anthropic Managed Agents session from those inputs ([`kickoff.ts`](../../apps/web/src/server/kickoff.ts)). Every
UI surface—the Project page cards, the Analysis page app bar, and page titles—derives its labels deterministically from
the stored `AnalysisInputs` through one shared module ([`scenario.ts`](../../apps/web/src/lib/scenario.ts)).

## Background

[`workspace-model.md`](workspace-model.md) defines an Analysis around a specific clinical focus: variant-led, case-led,
or cohort-led. Collapsing those distinct workflows into an unstructured chat prompt meant the platform had no record of
which clinical object an Analysis was about.

Creating and displaying an Analysis spans the browser form and the server layer
([`frontend-framework.md`](frontend-framework.md)):

- The **create composer** ([`apps/web/src/app/project/`](../../apps/web/src/app/project)) is the form on the Project
  page where a curator chooses a scenario, enters its fields, and starts an Analysis.
- The **BFF** is the Next.js server layer ([`data-plane.ts`](../../apps/web/src/server/adapters/live/data-plane.ts) and
  [`sql.ts`](../../apps/web/src/server/adapters/live/sql.ts)) that validates the request, opens the Anthropic Managed
  Agents session with the rendered kickoff instruction, stores the Analysis row in Cloud SQL, and renders the pages that
  list and open Analyses.

## Non-goals

- **Cohort-query scenarios in this initial set.** Cohort-query analyses belong to the broader workspace model
  ([`workspace-model.md`](workspace-model.md)), and adding a new scenario later only requires adding a `oneof` branch
  and its switch arms (§"Identity is derived"). This document scopes the initial set to single-variant classification,
  case analysis, and free-form exploration.
- **Renaming an Analysis.** Every surface derives the Analysis title from `AnalysisInputs`, which the BFF writes once at
  creation time (§Storage). If a curator enters the wrong transcript or sample ID, they create a new Analysis; adding a
  rename path would require a read-modify-write cycle on an immutable payload whose only reader is the naming surface.
- **Resolving the identifiers.** Whether a transcript exists, is versioned, or carries the coding change it is paired
  with—or whether a proband's sample ID is in the registry—is the run's business and never the boundary's. That is a
  permanent split, not a gap: resolution needs the data sources the run has and the boundary does not.

## Design

### A scenario is a template; creating an Analysis fills it in

A **scenario** is the platform's unit of specialization ([`PRODUCT.md`](../PRODUCT.md) §4). It specializes an Analysis
across the guiding prompt, the working document's section outline, and the tool surface the agent can call. The runtime
bundle carries the guiding prompt and the tool surface ([`agent-runtime.md`](agent-runtime.md)), while the opening
kickoff turn carries the working document's outline (§"The kickoff text is rendered, not stored").

To run one of those specialized workflows on a specific case, the curator must supply the instance data that the
workflow operates on. A scenario therefore also defines a **template**: a fixed set of input fields that the create
composer asks the curator to fill in, and that the guiding prompt in the runtime bundle expects to receive in the
opening turn.

An obvious way to represent the chosen scenario and its inputs is a scenario enum alongside a flat struct of optional
fields. That representation breaks as soon as a caller sets the enum to `VARIANT_CLASSIFICATION` while omitting `hgvs_c`
or leaving `proband_sample_id` populated from another scenario; every consumer then has to validate that the populated
fields match the enum.

Instead, [`workbench.proto`](../../schema/proto/themis/workbench/models/workbench.proto) models `AnalysisInputs` as a
required `oneof scenario`, with one message type per scenario:

```
Composer (composer.tsx)
  variant: "NM_001382309.1:c.332del"
  clinicalContext: "Proband with global developmental delay and hypotonia; de novo."
        │
        │ splitVariant() -> CreateAnalysisRequest { project_id, inputs }
        ▼
BFF (data-plane.ts)
  inputs.scenario = variantClassification {
    transcript: "NM_001382309.1",
    hgvs_c: "c.332del",
    clinical_context: "Proband with global developmental delay and hypotonia; de novo."
  }
        │
        ├──── kickoffText(inputs) (kickoff.ts)
        │       └──► Anthropic Managed Agents session (opening user turn + outline)
        │              └──► Read back as the first turn in conversation-view.md
        │
        └──── toBinary(AnalysisInputsSchema, inputs) (sql.ts)
                └──► Cloud SQL `analyses.inputs` (bytea)
                       └──► Decoded on read and projected via scenario.ts
                              ├──► Project page card ("NM_001382309.1:c.332del" + context)
                              └──► Analysis page app bar ("NM_001382309.1:c.332del" / "Variant classification")
```

Putting the fields inside the `oneof` branch binds the scenario choice and its required inputs into a single structure.
Which `oneof` member is set identifies the scenario, and the fields inside that member hold what the curator entered.
There is no separate enum to drift out of sync with the payload, and no way to name a scenario without supplying its
message.

### The three scenarios

The model defines three scenarios (`variant_classification` and `free_form` are in
[`workbench.proto`](../../schema/proto/themis/workbench/models/workbench.proto) today; `case_analysis` is designed here
and not yet wired):

- **`variant_classification`** (`VariantClassificationInputs`) classifies one coding variant within a clinical picture:

  - `transcript` and `hgvs_c` hold the transcript accession and the coding change against it (for example,
    `NM_001382309.1` and `c.332del`). Curators copy and paste a variant identifier as a single string such as
    `NM_001382309.1:c.332del`, so the create composer presents a single `Variant` input field and splits it on the first
    colon via `splitVariant` in [`scenario.ts`](../../apps/web/src/lib/scenario.ts). The protobuf model stores
    `transcript` and `hgvs_c` as two fields so downstream tools that resolve the transcript and the coding change
    separately receive pre-split values. Both fields are public nomenclature and carry no patient-identifying
    information.
  - `clinical_context` holds the free-text clinical summary (phenotype, inheritance, and excluded candidates).

- **`case_analysis`** *(designed, not yet built)* diagnoses a family case from its referral:

  - `proband_sample_id` holds the proband identifier as named in the deployment's sample registry and VCF header. It
    carries no syntax pattern rule because sample naming conventions differ across deployments.
  - `clinical_context` holds the referral prose that frames the diagnostic investigation.

- **`free_form`** (`FreeFormInputs`) is the escape hatch for exploratory work that no structured scenario covers yet:

  - `prompt` holds the task description in the curator's own words.

  Modeling `free_form` as a `oneof` branch inside `AnalysisInputs` keeps it inside the same exhaustive `switch`
  statements as structured scenarios across the BFF and UI.

### Presence and size validation at the boundary

A naive validation rule on string inputs is `min_len = 1`. That check passes when a curator enters twenty spaces into
`clinical_context`, starting an agent session with an empty clinical picture. Conversely, omitting an upper bound allows
arbitrarily large payloads. Because the BFF writes every input field inline into the `analyses` row and interpolates it
into the session's opening prompt, an unbounded field would permit unbounded database rows and unbounded input token
counts.

[`workbench.proto`](../../schema/proto/themis/workbench/models/workbench.proto) therefore enforces both presence and
length rules via `buf.validate` on every scenario field (mirrored client-side in the create composer to disable the
submit button while inputs are incomplete or over length):

- **Non-whitespace presence:** every field must contain a non-whitespace character (`this.matches('\\S')`).
- **Bounded byte/character length:** identifier fields (`transcript` and `hgvs_c`, and `proband_sample_id` when
  `case_analysis` is added) are capped at 255 characters, and prose fields (`clinical_context`, `prompt`) are capped at
  10,000 characters.

Both checks run at the RPC boundary without external database or network lookups. Stricter identifier syntax patterns
remain deferred (§Open questions).

### Identity is derived

Every surface that names or summarizes an Analysis—a card on the Project page, the header in the app bar, or the browser
page title—calls [`scenario.ts`](../../apps/web/src/lib/scenario.ts). When a new scenario is added to the platform,
[`scenario.ts`](../../apps/web/src/lib/scenario.ts) is the presentation half of the change (alongside its runtime bundle
in [`agent-runtime.md`](agent-runtime.md) and its kickoff template in
[`kickoff.ts`](../../apps/web/src/server/kickoff.ts)). For each scenario, the module derives:

- The **identifying line** (`analysisTitle`): `transcript:hgvs_c` for variant classification, `proband_sample_id` for
  case analysis *(when added)*, or the opening line of `prompt` (trimmed to 72 characters at a word boundary) for
  free-form.
- The **scenario label** (`scenarioLabel`): `"Variant classification"`, `"Case analysis"` *(when added)*, or
  `"Free-form"`. The create composer renders these labels in its scenario picker, and the Analysis page app bar displays
  the label beneath the identifying line.
- The **card body** (`cardContent` and `analysisDetail`): the prose shown on a Project page card and revealed on hover
  in the Analysis page chrome.

If every Project card forced a shared title-and-subtitle layout, a `free_form` Analysis would slice its own opening
sentence into a heading and render the remainder of the sentence as the subtitle. Instead, `cardContent` returns an
optional `identifier` alongside `body`. Structured scenarios populate both (`identifier: "NM_001382309.1:c.332del"`,
`body: clinicalContext`), while `free_form` sets `identifier: null` so the full instruction fills the card body. Every
card also displays the Analysis creation timestamp (`created_at`): because curators re-run the same variant or case to
compare results across agent versions, two runs of `NM_001382309.1:c.332del` share the same identifier and clinical
context, and the creation timestamp is what distinguishes their cards.

```
┌─ Variant classification card ──────────┐  ┌─ Free-form card ───────────────────────┐
│ NM_001382309.1:c.332del                │  │ Compare ClinGen SVI splicing guidance  │
│                                        │  │ against SVCv4 section 4.2 for deep     │
│ Proband with global developmental      │  │ intronic variants in haploinsufficient │
│ delay and hypotonia; de novo.          │  │ genes.                                 │
│                                        │  │                                        │
│ Variant classification · 14 minutes ago│  │ Free-form · 2 hours ago                │
└────────────────────────────────────────┘  └────────────────────────────────────────┘

┌─ Degraded card (newer scenario read by older BFF) ─┐
│ This analysis was created by a newer version of    │
│ Themis and cannot be shown here.                   │
│                                                    │
│ Unrecognised scenario · 5 minutes ago              │
└────────────────────────────────────────────────────┘
```

When a newer build adds a branch to `AnalysisInputs` and writes an Analysis row, an older BFF build (during a rolling
deployment or a rollback) decodes the binary protobuf with the new branch preserved as an unknown field and sees
`inputs.scenario.case` as `undefined`. If [`scenario.ts`](../../apps/web/src/lib/scenario.ts) threw an exception on an
unset `oneof`, a single row written by a newer build would crash the entire server-rendered Project page listing.
Instead, [`scenario.ts`](../../apps/web/src/lib/scenario.ts) maps an unset `oneof` to `"Unrecognised scenario"` with a
fallback body, satisfying the forward-compatibility guarantee in [`proto.md`](proto.md) §Schema evolution ("a reader
parses every artifact ever written"). In [`sql.ts`](../../apps/web/src/server/adapters/live/sql.ts), `listAnalysesIn`
also catches binary decode errors on individual rows and substitutes an empty `AnalysisInputs` message so a corrupt row
degrades its own card while keeping the `/` listing intact (whereas direct point access via `getAnalysis` still raises
an error).

This unknown-branch degradation relies on binary protobuf's unknown-field retention, so it applies to Cloud SQL reads
and every server-rendered surface. It does not apply across the browser-to-BFF Connect RPC seam, which uses proto3-JSON:
JSON payloads are keyed by field name and reject or drop unknown `oneof` members. Only the create composer sends
`AnalysisInputs` over that seam, and if a client sends a scenario that the server predates, the BFF rejects the
`CreateAnalysis` call because the `oneof` arrives unset.

### The kickoff text is rendered, not stored

When `createAnalysis` runs in [`data-plane.ts`](../../apps/web/src/server/adapters/live/data-plane.ts), the BFF passes
`AnalysisInputs` to [`kickoffText`](../../apps/web/src/server/kickoff.ts) to render the opening user turn for the
Anthropic Managed Agents session, and stores only `AnalysisInputs` in Cloud SQL.

The rendered kickoff text specifies the case instance to work on and, for scenarios that produce a working document,
closes with the working document's outline (the required `##` sections and a one-line summary of what each section
holds). This divides responsibility with the runtime bundle ([`agent-runtime.md`](agent-runtime.md)):

- The **kickoff turn** ([`kickoff.ts`](../../apps/web/src/server/kickoff.ts)) supplies the per-run inputs (`transcript`,
  `hgvs_c`, `clinical_context`) and the working document's section list. The curator reads this turn at the top of the
  conversation stream ([`conversation-view.md`](conversation-view.md)).
- The **guiding prompt** (the skill document in the runtime bundle) supplies the detailed domain rules for each section,
  such as notice wording, reflection questions, and evidence derivation formatting.

Each structured scenario's kickoff template is tailored to the clinical task it starts:

- **Variant classification** asks the agent to classify `transcript:hgvs_c` in the stated clinical context, and asks for
  **"the disease entity or entities"** associated with the gene ([`kickoff.ts`](../../apps/web/src/server/kickoff.ts)).
  Under ClinGen SVCv4 rule SM21, a single gene can map to multiple non-mutually-exclusive monogenic disease entities;
  asking for singular "the disease entity" would presuppose that only one entity exists before the run checks the
  gene-disease record. The same kickoff template opens every run in the automated run-review loop
  ([`run-review-loop.md`](run-review-loop.md)), whose `clinical_context` names the specific disease entity the variant
  is registered against, and closes with the seven-section working document outline.
- **Case analysis** *(designed, not yet built)* asks the agent to diagnose the case from the referral: establish the
  disease frame from `clinical_context`, analyze the family's genomic data for `proband_sample_id`, classify the
  candidate variants it prioritizes, and synthesize a case-level conclusion. Single-variant classification is one step
  within that diagnostic workflow.

Rendering the kickoff on the server ensures that every run labeled `variant_classification` received the server's
classification instruction, which is what makes two runs of the same scenario comparable. Because the conversation view
already reads sent turns back from the Anthropic session event log ([`conversation-view.md`](conversation-view.md)), the
kickoff turn is recorded there; keeping a separate durable copy in Cloud SQL belongs to general session-mirroring work
for the entire conversation log.

Rendering the kickoff on the fly shapes how runs are compared, trusted, and validated:

- **Comparability holds within a single BFF kickoff version, and the rendered kickoff is recorded by the session rather
  than alongside the inputs.** Both the kickoff prose and the working document outline are versioned with the BFF code
  in [`kickoff.ts`](../../apps/web/src/server/kickoff.ts). Editing `kickoff.ts` changes what future runs are asked
  without altering what earlier runs were asked. What was sent to an earlier run is the first user event in the Managed
  Agents session; today that sits in Anthropic's store until session mirroring lands
  ([`agent-runtime.md`](agent-runtime.md)). Closing that gap is the session-mirroring work, not a version field beside
  the inputs.
- **This is not a security boundary.** A curator filling in a scenario is expressing intent, just as they do when
  steering a running session, and `free_form` exists so they can ask for work no template covers. Prompt injection is
  *outside* content redirecting a run away from what its user asked for; a user's own inputs are not that.
- **Incomplete client payloads fail loud at creation time.** Because the server assembles the kickoff from validated
  fields inside a required `oneof`, a client bug that drops a field fails boundary validation on `CreateAnalysis`
  instead of quietly sending a shortened prompt to the agent.

### Storage

The `analyses` table in Cloud SQL stores `AnalysisInputs` in a single `inputs bytea NOT NULL` column
([`0008_analysis_inputs.sql`](../../themis/migrate/migrations/0008_analysis_inputs.sql)), following the inline-binary
pattern in [`proto.md`](proto.md) §"Protos in Cloud SQL columns". Inline binary columns are often chosen to preserve
unknown fields across read-modify-write updates; here `analyses.inputs` is written once in `insertAnalysis`
([`sql.ts`](../../apps/web/src/server/adapters/live/sql.ts)) and never updated, so binary storage is chosen to keep
[`workbench.proto`](../../schema/proto/themis/workbench/models/workbench.proto) as the single schema. Because no SQL
query filters or projects inside `inputs`, `jsonb` would add proto3-JSON schema drift without buying useful
queryability.

On the Connect RPC wire ([`workbench.proto`](../../schema/proto/themis/workbench/models/workbench.proto) and
[`rpc/workbench.proto`](../../schema/proto/themis/workbench/rpc/workbench.proto)), both `Analysis` (field 6) and
`CreateAnalysisRequest` (field 3) carry `inputs` as a nested `AnalysisInputs` message rather than raw `bytes`. Sharing
the typed message gives the create composer and the BFF the same generated TypeScript types and keeps the `oneof`
structure visible on the wire.

The legacy free-text `prompt` field retired in two steps because the database column and the protobuf message fields ran
on different clocks: migration [`0008_analysis_inputs.sql`](../../themis/migrate/migrations/0008_analysis_inputs.sql)
dropped the `prompt` column from `analyses`, and once all callers stopped setting the deprecated `prompt` fields on
`Analysis` and `CreateAnalysisRequest`, those protobuf fields were removed and both the field number and the field name
were marked `reserved` (`reserved 4; reserved "prompt";` on `Analysis` and `reserved 1; reserved "prompt";` on
`CreateAnalysisRequest`, [`models/workbench.proto`](../../schema/proto/themis/workbench/models/workbench.proto)).
Because the browser seam uses proto3-JSON ([`proto.md`](proto.md)), where the field name is the wire key, reserving the
name prevents a future field from colliding on JSON keys just as reserving the number prevents binary tag collisions.

Migration [`0008_analysis_inputs.sql`](../../themis/migrate/migrations/0008_analysis_inputs.sql) is destructive and
creates a brief failing window during deployment. Both costs are accepted because the platform runs in a single
pre-production environment with no live curator data worth preserving, matching the condition for destructive migrations
in [`migrations.md`](migrations.md) §"How it runs":

- **Rows deleted by `0008`:** `0008` deletes all rows from `session_context` first (because its foreign key to
  `analyses` has no `ON DELETE CASCADE`) and then deletes all rows from `analyses` rather than backfilling them, which
  allows it to drop `prompt` and add `inputs bytea NOT NULL` without a default value. Those rows would have backfilled
  losslessly into a `free_form` `Analysis`, the old column being non-null and non-blank at the boundary, so the deletion
  is a choice rather than a forced loss: the rows in `dev` are development test runs, and keeping them would mean
  encoding protobuf bytes inside a SQL migration, which nothing in `dev` is worth. Any working-document blobs those
  development runs wrote to GCS remain as unreferenced objects keyed by deleted analysis IDs.
- **Deployment availability window:** `pulumi up` rolls Cloud Run to the new container image before `themis.migrate`
  applies `0008`, so the new BFF code runs against the pre-`0008` table schema until the migration finishes. During that
  window, reads fail because `analyses.inputs` does not exist yet, and creates fail because the old `analyses.prompt`
  column is `NOT NULL` with no default and the new code no longer populates it. Both errors clear automatically once
  `0008` completes. A browser tab still running the pre-deployment JavaScript bundle will also fail if it submits a
  `CreateAnalysis` request containing the old `prompt` field and no `inputs`; that failure clears when the curator
  reloads the page. Retiring a browser-sent field without such a window would take two deploys—stop sending it, then
  delete it—and the expand/contract shape otherwise called for here (the create path still writing the old column, the
  read path tolerating a missing `inputs`, the drop deferred to a later migration) is a compatibility mode in every
  reader and writer of a column that exists to be read one way.

## Alternatives considered

- **Model-generated title stored at creation time (the chat-app pattern).** A small language model can summarize any
  free-form prompt into a short title for a fraction of a cent per Analysis. However, generating a title puts an extra
  LLM network round trip on `CreateAnalysis`, requires falling back to a truncated prompt excerpt whenever the call
  fails, and produces a guessed title that curators may need to edit—which in turn requires a rename RPC, a mutable
  `title` column, and rename UI. (Anthropic's Managed Agents API provides no automatic session titling; session titles
  are caller-supplied strings.) Deriving the title deterministically from typed scenario inputs avoids all of those
  failure modes and extra surfaces.
- **Removing free-form altogether.** Requiring a structured scenario for every Analysis would guarantee structured
  identifiers on every card. However, structured scenarios cover one kind of work today, so removing `free_form` would
  block curators from running any other investigation until a dedicated scenario exists for it.
- **Storing `inputs` as `jsonb` instead of `bytea`.** A `jsonb` column is directly readable in `psql` and queryable in
  SQL. However, no SQL query filters or projects inside `inputs`, and `jsonb` introduces proto3-JSON field-name and
  unknown-field drift between writers and readers.
- **A single `variant` string field carrying `transcript:hgvs_c`.** Storing a single combined string in
  `VariantClassificationInputs` would match the single input box in the create composer and save one protobuf field.
  However, every downstream consumer that resolves the transcript or the coding change would then have to split the
  string itself and agree on delimiter edge cases. Splitting once in [`scenario.ts`](../../apps/web/src/lib/scenario.ts)
  gives every downstream reader pre-separated `transcript` and `hgvs_c` fields.

## Open questions

- **Verifying `proband_sample_id` against the VCF and sample registry.** Boundary validation will check only that
  `proband_sample_id` is non-blank and at most 255 characters (§"Presence and size validation at the boundary"), and no
  sample-metadata service exists yet to check that the sample ID appears in both the deployment's sample registry and
  the VCF header. Once the sample-metadata interface is designed, the create composer should let the curator select a
  proband from the registry rather than typing an identifier by hand. Until then, a mistyped sample ID is only caught
  when the agent run queries the sample's data.
- **No format rule on any field.** The identifiers a curator types are still moving, so pinning a pattern now would
  refuse valid input before the shapes settle. Which fields can carry one, and what it should be, stays open.
