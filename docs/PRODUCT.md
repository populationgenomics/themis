# Themis — product north star

> North star for Themis: goals, load-bearing principles, scope — the frame to start
> detailed plans from, so context isn't re-litigated each time. Changes slowly. **Not the
> build plan** — concrete current work lives in `issues/` (epic + exploration tickets),
> `docs/design/` (design docs), and `docs/plans/` (plans). Written as architecture-grounding
> for an LLM reader (you); dense by design, each fact stated once. Public repo — phrase
> accordingly.

## 1. What Themis is

A platform for **expert rare-disease curators**. The curator stays in control; the technical
foundation is **agents on frontier models** doing the heavy evidence-gathering and reasoning.

- **In:** the full patient data — raw reads (BAM/CRAM), variant calls, RNA-seq where
  available, phenotype (HPO + free-text notes), family/sample metadata. A standard pipeline
  produces baseline annotations up front; it is **not a boundary** (the agent can inspect raw
  data and run custom analyses — §6).
- **Structure:** ACMG V4 (the structured, points-based ACMG/AMP criteria) organises _which
  lines of evidence are needed_ (adopted, not reinvented); it does not dictate how they are
  gathered.
- **Output:** not a one-shot report but a **living artifact, co-authored with the curator** —
  findings with cited, reproducible support and explicit calibrated uncertainty, evolved
  through conversation as the work proceeds (interaction model in §7).
- **Two feedback loops, both feeding development directly:** curator behaviour/feedback (→
  prompts, tools, eval), and the agent's own _desire paths_ (the tool or shape it wished it had
  → new tools, prompts).

## 2. Why

- **The bottleneck is expert attention.** Many undiagnosed cases would be solvable with enough
  expert time — intensive digging nobody currently has the bandwidth for. Some are genuinely
  data- or biology-limited (novel biology → the discovery / hypothesis-exploration track,
  §5/§8), but the bet is that a **large fraction are attention-limited**: Themis puts AI on the
  heavy lifting so scarce expertise reaches far more cases — the system gathers and reasons, the
  human judges where it counts.
- **Why now:** frontier agentic reasoning, plus — in the research setting — token cost
  treated as effectively unconstrained during exploration, so we can find _how far reasoning
  can go_ (cost is a later optimisation — §6).
- **Program:** built under **AASGARD** (Australian Alliance for Secure Genomics and AI in
  Rare Disease), an MRFF-funded alliance on safe AI across research and clinical genomics.
  Themis is its build-novel-tools strand, with formal benchmarking alongside.
- **Success — correct, _novel_ diagnoses on hard, long-undiagnosed cases** current workflows
  miss, and curators covering more cases with better-supported reasoning; measured against
  solved/ground-truth cases and curator feedback. Demonstrable,
  well-evidenced wins are themselves a deliverable — they earn clinician trust and sustain the
  work.

## 3. Who it's for

**CPG (Centre for Population Genomics) rare-disease curators** are the initial
users _and_ the product team — dog-fooding is non-negotiable (_if nobody's
using it, it's not done_), engineers and curators in lockstep. The **CaRDinal**
cohort — CPG-managed, consented, research — is the basis from the beginning.
Non-research **clinical** cohorts (VCGS, SA Pathology) come later: an eventual
requirement, not the immediate target.

## 4. Core thesis — the actual contribution

Themis is a **scenario-parameterised platform**. One shared substrate — **workspace** (the
curator↔agent surface), **orchestrator**, curated **tool/data layer**, end-to-end **trace** —
is specialised to an analysis scenario by three things: a **guiding prompt** (how to approach
_this kind_ of analysis, with freedom to make case-dependent choices), a **working-document
outline** (the scenario's starting structure for the evolving artifact), and the **tools/DBs**
in reach.

- **The durable value is the codified RD domain expertise** in those three; the generic
  plumbing (runtime, tool-calling, orchestration loops) is **thin and replaceable**, so the
  platform **rides model improvements** instead of being obsoleted by them. (As models improve,
  that value concentrates in the **working-document outline + tools/data** while the **guiding
  prompt shrinks** — an explicit bet; §11.)
- **ACMG is the _what_; the orchestrator owns the _how_.** No fixed one-agent-per-cell mapping
  (a _cell_ is one ACMG evidence criterion) — the orchestrator decides scheduling per case
  (one gather feeding many cells, gather + synthesis folded together, etc.). The framework
  scaffolds _evidence_, not agent topology.

## 5. Use cases & modes

- **Easy diagnosis** — fast answers on tractable cases.
- **Borderline VUS** — classify _and_ explain what evidence would move it over the line, with
  consideration to how feasible/costly that evidence is to obtain.
- **Hypothesis exploration** — open threads on non-obvious causative variants (VUS and beyond)
  and novel gene–disease links, where curator steering helps close them out.

Two modes cut across these: **scoring** (evidence → classification) and **hypothesis**
(generate, rank, and develop candidate explanations, with the curator steering). Output shows all its working
and **supports, not displaces** the curator.

## 6. Design principles (load-bearing)

- **Composable tools, not a monolithic pipeline.** We don't build new
  variant-calling/pathogenicity pipelines; a standard best-practices pipeline produces baseline
  annotations up front (the agent isn't handed a FASTQ and left to it). The agent orchestrates
  **composable, deterministic, reproducible tools**, and can run **custom analyses over the raw
  data** when standard outputs fall short (e.g. a variant the pipeline never called) — every
  step trace-captured and reproducible. Repeated custom analyses graduate into new deterministic
  tools/annotations (desire paths).
- **Hybrid facts + judgement.** Deterministic code → reproducible facts (parsing, QC,
  frequencies, arithmetic, API calls); agents → judgement (weighing, synthesis, critique).
- **Verifiable provenance.** Every claim cites a reproducible source; "unknown" is valid;
  observed / inferred / assumed kept distinct.
- **Scores shown, not load-bearing.** ACMG scores exist for transparency; the holistic
  verdict reasons over the claims, not the rolled-up score.
- **Adversarial review.** A fresh-context reviewer (even the same model) beats self-review;
  candidates must _survive scrutiny_, not merely score well.
- **Calibrated uncertainty.** Verdicts carry an explicit uncertainty the curator can
  threshold on.
- **Human-in-the-loop steering.** The curator supplies direction/judgement; the system
  surfaces where it is stuck or flying blind.
- **Reasoning-first economics.** Optimise for the model behaviour we want now; optimise token
  cost once load-bearing behaviour is known.
- **Avoid obsolescence (the Bitter Lesson).** Prefer general, model-driven approaches over
  hand-built heuristics and rigid orchestration; lean into dynamic, model-composed workflows.
  Keep just enough fixed scaffold to guarantee evidence coverage, traceability and eval;
  widen agent agency as eval proves the model can own more.
- **Minimal guiding prompts.** Keep each scenario's guiding prompt as light as possible —
  encode only what steers past failure modes we actually observe (e.g. "visualise tricky
  regions with IGV to catch SVs"), not exhaustive instructions; expect prompts to thin as
  models improve.
- **Build with users; open source** (not yet portability-optimised).

## 7. Workspace, interaction & artifact model (north-star; MVP defers most)

Workspace vocabulary mirrors Claude.ai's, deliberately (familiar to users and to Anthropic):

- **Project** — the **data boundary**: restricted to the datasets and users explicitly
  associated with it (e.g. an ASD-specific selection of cases); hosts the Analyses and a
  Project overview. Datasets ↔ Projects is M:N.
- **Analysis** — a Claude.ai **"Chat"** (subagents and all): a collaborative working session
  bound to a Project, with Claude.ai-style **branching** to explore alternatives. Its **focus**
  is scenario-dependent (a variant; a case = focus sample + relatives; a cohort query).
- **Working document** — the Analysis's evolving **artifact** (à la Claude.ai artifacts:
  written, rewritten, versioned) — the "opinion formed," _not a rigid schema_ but a loose
  document with lightweight structural contracts only where deep-linking demands them
  (citations, evidence/provenance, the claims/verdicts that comments and traces anchor to).
- **Report** — the **validated, approved** form of a working document, **linked up to the
  Project's entities** (variant / gene / case / individual) and surfaced in the Project
  overview. An explicit **accept-to-publish** step promotes a working document to a Report.
  Project-private.

**Cross-Project scope is default-deny:** a Project may draw only sufficiently
aggregated/de-identified data from others — a variant-level "seen elsewhere?" signal, an
existence signal that triggers a consent process, or consented matchmaking. Case-level content
never crosses implicitly.

**Facts are shared across Projects; literature is not.** Themis shares a platform-wide,
provenance-tracked substrate of **facts extracted from the literature**; these cross Projects
freely, as do genuinely public reference DBs like gnomAD/ClinVar. Each fact keeps a **citation —
a pointer to its source, not a copy of it**. The **source documents** themselves are not pooled:
opening the underlying paper — e.g. to read the verbatim passage and validate a fact — follows the
user's **institutional licensing**, an axis distinct from the Project boundary, so licensed
literature is never shared across institutional lines. Sources are _untrusted content_ (§9),
reached through a curated, controlled tool surface.

**Interaction model** (the workbench it grows into): curator and agent co-author the working
document through conversation, not one-shot. The system refines intent up front, works
asynchronously with progressive disclosure, **proactively flags roadblocks and asks for
direction**, surfaces uncertainty in place, and preserves dead ends as first-class.

Beyond MVP: hypothesis-vs-fact; flags + per-variant notes (as in seqr); cross-case variant
memory (within-Project by default); and the case-derived / variant-attached / public
lines-of-evidence split.

> Mechanics — branching, access rules, cross-Project exceptions, sample scope, Report
> versioning — live in `docs/design/workspace-model.md`; term definitions in `GLOSSARY.md`.

## 8. Trajectory

MVP (variant-led ACMG; §12) → **proband/case-led analysis** (full WGS + HPO + notes →
candidates; pedigree / segregation / phenotype-from-notes) **with an eval/benchmarking harness
in parallel from the start** → deepen interactive steering and hypothesis mode → **cohort
gene-discovery** (cross-cohort, phenotype-driven, matchmaking; a goal, not the first
follow-on) → clinical hardening. Each new scenario is cheap: swap {guiding prompt,
working-document outline, tools}.

## 9. Security & data governance (first-class)

Genomic and (later) patient data are the sensitive asset; the architecture is shaped around
**not being able to leak them**.

- **Lethal trifecta** (private data × untrusted content × external channel), mitigated
  primarily by **tool-surface design** — per-tool permissions and data-flow, tightly-typed
  constrained tools (enums, not free-form strings), **local data over open-internet queries**.
  Not post-hoc output filtering.
- **Self-hosted execution boundary:** execution + tools run inside CPG's network (self-hosted
  sandboxes + MCP tunnels); Anthropic does orchestration only; agent code-execution,
  filesystem, tool calls and data never leave the boundary — critical because the agent runs
  code over raw genomic data. Deferrable for the earliest MVP (public evidence sources, no
  patient data — §12), but required for the product. The UI is auth-controlled but
  internet-reachable (not VPN-gated).
- **Sensitivity tiers:** individual-variant public-DB lookups are low-risk; cohort-scale
  genomic data and PII/clinical-notes progressively tighten the surface.
- **Tainted-data propagation** is real but secondary — the primary mitigation is leaving the
  agent nothing to exfiltrate _to_.
- **Build the hard-to-retrofit parts now** (data boundary, end-to-end provenance/audit,
  uncertainty); defer regulatory framing, formal validation, UI polish.
- **Patient-safety posture:** research-only for now; a confidently-wrong call is mitigated
  _epistemically_ (calibrated uncertainty, adversarial review, provenance), and clinical use
  gates on formal validation (§8, §10).

## 10. Scope boundaries & ecosystem

**Themis is not:** a portable general-purpose curation suite; a public service; a regulated
clinical tool (yet); a new variant-calling/pathogenicity pipeline.

**Standalone for now:** no coupling to **Talos / Metamist / seqr / Analysis Runner**
(import/export only); cohort-wide ops over Themis's _own_ store are expected.

**Ecosystem:**

- **Talos** — CPG's older, in-production heuristic reanalysis tool (no AI). Thematic name
  only, no coupling.
- **PanelApp** (Australia; forked from the Genomics England instance) — a gene-curation
  platform that feeds Talos.
- **CaRDinal** — a CPG-managed, consented research cohort; the founding dataset Themis's
  Projects draw from (others may follow) (§3, §7).
- **seqr / Metamist / Analysis Runner** — existing CPG infrastructure; possible later
  integration.

## 11. Bets & open questions

**Founded bets** (held deliberately; keep validating via eval — each notes what would falsify it):

- Durable value concentrates in the **tools/data layer** and **curator-facing artifacts**, with
  **guiding prompts staying minimal and shrinking further** as models improve (§4, §6).
  _Falsified if_ better models need *more* domain-specific steering, not less.
- A **fresh-context adversarial reviewer beats self-review** (§6). _Falsified if_ same-model
  reviewers share the producer's blind spots and add no signal.
- For a **large fraction** of undiagnosed cases the bottleneck is **expert attention**
  (intensive digging nobody has time for), not missing data or novel biology — the latter is
  the discovery / hypothesis-exploration track (§2, §5). _Falsified if_ most hard cases turn out
  data- or biology-limited.

**Open questions** (resolve via build + eval, not pre-decided):

- The **eval/benchmarking harness**: how it establishes ground truth and validates a "novel
  diagnosis" (Aim 1, runs alongside — §8).
- Whether **LLM-expressed confidence can be calibrated** on hard, novel, low-base-rate calls —
  it currently carries patient-safety weight (§6, §9).
- How much **fixed scaffold vs. orchestrator autonomy** — evidence-coverage/reproducibility
  vs. riding the model curve (§4, §6).
- The real **cost envelope** behind "effectively unconstrained" once we leave pure exploration
  (§2).
- **Model/orchestration dependency** — "thin, replaceable plumbing" is the hedge, but
  orchestration sits on Anthropic's side (§9); how real is the switching cost?
- **Clinical patient-safety hardening** as we approach non-research cohorts (§9).
- **When/whether to integrate CPG infra** (currently standalone; §10).

## 12. Current slice — the MVP ("Spike")

The product is intentionally narrowed to one slice first — the **Spike**: `(variant,
free-text condition)` → a versioned ACMG-V4 report, from **public evidence sources** only
(patient data is the sensitive input — §9), dog-fooded with CPG curators. Its concrete
definition — scope, hybrid gather strategy, infrastructure, the exploration tickets — lives
with the active work, not here: the **Spike epic** (`issues/epic-themis-spike.md`) and the
design docs under `docs/design/`.
