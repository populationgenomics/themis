# Workspace model (design)

**Status:** draft · **Parent:** [`../PRODUCT.md`](../PRODUCT.md) §7

The collaboration / data model behind Themis's workspace. PRODUCT.md §7 states the high-level invariants; this doc holds
the mechanics. Vocabulary mirrors Claude.ai's deliberately (familiar to users and to Anthropic); see
[`../../GLOSSARY.md`](../../GLOSSARY.md) for definitions.

## Concepts

- **Project** — the access / data boundary. Restricted to the **datasets explicitly associated** with it and the **users
  explicitly assigned** to it (e.g. an ASD-specific selection of cases). Hosts Analyses and a Project overview. Datasets
  ↔ Projects is **M:N** (a sample/dataset can belong to several Projects; CaRDinal is the first dataset, others follow).
  A Project may additionally draw **sufficiently aggregated / de-identified** data from other Projects — see
  Cross-Project below.
- **Analysis** — a Claude.ai **"Chat"** (with subagents): a collaborative working session bound to one Project, visible
  to its members. It's a **tree of immutable turns** — you **append** turns as work proceeds and **branch** to explore
  an alternative line; navigate between branches, and history is **retained** (a branch can be archived, not discarded).
  Nothing is ever edited in place: "editing" a turn just branches from its (unchanged) parent. Branching stays *within*
  the Analysis (Claude.ai semantics); seeding a *new* Analysis from an existing one — "fork"/duplicate — is a distinct
  action, if we need it. The Analysis **focus is scenario-dependent**: a variant (variant-led); a case — one or more
  focus samples + relatives (case/proband-led — often triggered by an unsolved case, but a focus sample is **not
  required**); or a cohort query (discovery).
- **Working document** — the Analysis's evolving **artifact**, analogous to a Claude.ai artifact: written, rewritten,
  **versioned** (and *not* a conversation). It is the "opinion formed" — *not a rigid schema*, but a loose document with
  lightweight structural contracts only where deep-linking demands them (citations, evidence/provenance, the
  claims/verdicts that comments and traces anchor to). One Analysis ↔ one working document — which evolves through a
  full version history, not a single fixed file.
- **Report** — the **validated, approved** form of a working document; an explicit human **accept-to-publish** step
  promotes it. **Linked up to the Project's entities** (variant / gene / case / individual) and surfaced in the Project
  overview for oversight. **Project-private.** A Report lives at the **Project** level: **one accepted per entity**,
  **linearly versioned** (only versions — **not** branched), with full history retained for auditability.

A working document is *not* a Report: the working document is the live, evolving artifact inside an Analysis; the Report
is the approved snapshot that gets linked to the Project scope.

## Cross-Project sharing (default-deny)

A Project sees only its associated datasets and users. The only data that crosses, and only when sufficiently aggregated
/ de-identified:

- **De-identified variant-level signals** — e.g. "have we seen this variant elsewhere?" and **aggregate frequencies /
  counts** (e.g. allele frequency), subject to a **minimum cohort size** and cohort-scale re-identification limits (see
  PRODUCT.md §9).
- **Individual-level existence signals** — "a Report for this individual exists in another Project" — surfaced only to
  **trigger a consent process that grants further visibility**, never to leak content.
- **Case-level / cross-patient flows** (notably **matchmaking**) — only with explicit consent. The agent can
  **trigger/propose** a match; a **human completes it** (consent, contact).

**Facts cross Projects; literature does not.** Themis maintains a platform-wide, provenance-tracked substrate of **facts
extracted from the literature**, **shared across all Projects** — as are genuinely public reference DBs
(gnomAD/ClinVar). Each shared fact carries a **citation that is only a pointer to its source — never the verbatim
text**. The **source documents** are a different matter: **resolving a citation to the source full text** (e.g. to read
the exact passage and validate the fact) is gated at access time on the user's **institutional licensing agreement**,
and those institutional lines do **not** coincide with Project boundaries. Themis does not pool or redistribute licensed
literature across institutions. Sources are _untrusted content_ (PRODUCT.md §9), reached through a curated, controlled
tool surface.

## Authorization (enforcing the Project boundary)

The boundary is enforced, not just declared. A user belongs to many Projects, so every access names the Project it acts
in — a create and a listing name it directly, a point access (an Analysis's events, working document, poll) resolves it
from the Analysis. Access is admitted **iff the user is a member of that Project**. A non-member is answered exactly as
for an unknown Project or Analysis — **not-found, never a distinguishable forbidden** — so cross-Project *existence*
never leaks (the existence-signal rule above).

Enforcement is a single default-on chokepoint ([`security.md`](security.md)), one level below request auth
([`frontend-framework.md`](frontend-framework.md) §Auth):

- **`AuthorizedBackend`** wraps the raw `AnalysisDataPlane`, bound to the verified user. `userContext` is its only
  constructor, so a route can never hold an unscoped backend. `createAnalysis` and `listAnalyses` name a Project and
  verify membership before touching data; a point access (`getDocument`, `pollEvents`) resolves the Analysis's Project
  and checks membership — and the working document is addressable only through that resolved Analysis, so the check
  cannot be skipped. `listProjects` returns the Projects the user belongs to — the create/list selector.
- **`ProjectMembership`** is the mapping the check reads — a `project_members(project_id, user_email, role)` table in
  the real adapter, a seeded map offline. Empty ⇒ the user reaches nothing (default-deny); a real deploy is closed until
  memberships are seeded.

This mirrors the session plane: `session_context(token_hash, project_id, …)` project-scopes the **agent's** data access
(the store resolves a bearer → its Project); `project_members` is the same boundary for the **user's** access.

## Interaction model (the workbench it grows into)

- **Refine intent first** — pin down the question/goals in dialogue before committing work.
- **Asynchronous + progressive disclosure** — heavy work runs without blocking the curator, who steers at a high level
  and drills into any agent's detail or the trace on demand.
- **Proactive & bidirectional** — the system flags roadblocks and asks the curator for direction or prioritisation; the
  curator can intervene and redirect at any point.
- **Uncertainty surfaced in place** — contentious or under-evidenced parts are highlighted for human scrutiny.
- **Dead ends preserved** — failed hypotheses and excluded paths are first-class and kept (not scrubbed), as reusable
  context for new directions.

## Beyond the MVP

- **Epistemic status + provenance** — track each claim's status (working hypothesis → supported → established) tied to
  its provenance (observed / inferred / assumed — PRODUCT.md §6), not a hard hypothesis-vs-fact binary: even "facts"
  rest on source reliability.
- **Flags + per-variant notes** — the lightweight inter-curator channel (as in seqr).
- **Cross-case variant memory** — within a Project by default: reuse prior Reports on a variant, carrying their
  assumptions, ready to invalidate them when new work warrants; cross-Project only via the signals above.
- **Lines-of-evidence split** — case-derived vs. variant/gene-attached (reusable) vs. public.

## Open questions

- Exactly which aggregations/de-identifications qualify to cross Projects (ties to PRODUCT.md §9 sensitivity tiers).
- How a user's **institutional affiliation** is established and trusted for licensing-gated source access (uploads
  currently rest on user-declared affiliation).
- Report reconciliation when branches diverge on the same entity.
