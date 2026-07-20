# Design: Frontend and web tier for the Themis Spike

**Parent epic:** [`issues/epic-themis-spike.md`](../../issues/epic-themis-spike.md) (PR #1) **Related:**
[`spike-infrastructure.md`](spike-infrastructure.md) owns the IAP/Cloud Run/secret infra this rides on;
[`workspace-model.md`](workspace-model.md) owns the Project/Analysis/working-document/Report entities the UI surfaces;
[`agent-runtime.md`](agent-runtime.md) owns the Managed Agents coordinator and the trace feeders.

## Why this exploration

The Spike ships a Cloud Run web app from day one — internal CPG curators, behind IAP. The Spike's own UI is deliberately
minimal: a **live conversation thread** (the coordinator and its sub-agents working, surfaced by polling) and a **report
/ result viewer**. The near-term target it grows into is the **co-scientist workbench** (PRODUCT §7) — an editable
working document, the trace drilled in place, comments anchored to claims and ACMG cells. The framework and web-tier
shape are chosen so the Spike reaches that workbench **without a rewrite**, even though Phase 1 builds little of it.
This doc fixes the frontend framework and the web-tier data flow against the Managed Agents runtime
([`agent-runtime.md`](agent-runtime.md)).

## Decision: Next.js (full-stack TypeScript); Python confined to the tool tier

The language boundary falls at the **agent's data plane**, not at the browser:

- **Agent-facing tier** — the internal services and data-plane mediation ([`agent-runtime.md`](agent-runtime.md),
  [`spike-infrastructure.md`](spike-infrastructure.md) §8). Reached from the self-hosted sandbox in code mode, never by
  the browser. Python; isolated; its language is invisible to the web app.
- **Web tier** (this doc) — **Next.js**: the UI plus the BFF (backend-for-frontend: the data API, IAP handling, the
  webhook receiver, and the live session relay). One server-side language across the frontend↔backend seam — the surface
  that iterates fastest in the dog-fooding loop.

The BFF is domain-logic-light (it reads typed rows and relays events; the genomics logic lives in the tool tier and is
written to the store there), so nothing pulls it toward Python; unifying the web app in TypeScript keeps the hot seam
single-language. The editable artifact, anchored comments, and virtualized live trace tree are React-first surfaces
(ProseMirror/Lexical-class editor, TanStack Virtual, annotation layers); Next.js is the stock-standard host for them and
mirrors Claude.ai's shape (PRODUCT §7).

### Why not the alternatives

- **Python full-stack** (FastHTML / Reflex / NiceGUI / HTMX) — viable only while the artifact stays *rendered* with
  display-only anchors. The editable artifact, anchored margin comments, and a virtualized live-appending trace tree are
  high-frequency client-state surfaces these frameworks push into hand-wrapped JS; reaching the workbench from there is
  a rewrite, not an extension.
- **SvelteKit** — same full-stack-TS shape, thinner ecosystem for exactly the editor / virtualization / annotation
  libraries (React-first).
- **React SPA + separate API** — with the BFF this thin, Next.js's integrated UI+BFF is the cleaner form of the same
  idea.

## Web-tier architecture

A **single Cloud Run service** (Next.js `standalone` output), scale-to-zero; the 1–2 s cold start on the first request
after an idle gap is acceptable, and active polling keeps the instance warm through a working session. **No standing
background worker.**

### Session observation — no held connection

Anthropic runs the agent loop; the web tier never holds a connection for the run's duration
([`agent-runtime.md`](agent-runtime.md)).

- **Kick off** — the BFF creates the session and sends the case as a `user.message` (request/response).
- **Live conversation thread** — the browser polls the BFF (~2–3 s); the BFF reads Anthropic's persisted event log
  (`events.list`), authorizes the IAP identity against Project membership, translates events to the display model, and
  returns the whole projected stream (the client replaces by id, never appends). The BFF holds the Anthropic API key and
  is the **authorization and projection point** — the browser never reaches Anthropic directly. Anthropic's log *is* the
  live transcript; the BFF relays it, it does not copy it.
- **Steering** — occasional curator interjections are plain POSTs to the BFF that call `sessions.events.send`
  (`user.message` / `user.interrupt`). SSE/WebSocket push is deferred (Open questions).

The poll-through fits the async, progressive-disclosure interaction model (PRODUCT §7): agent messages and thinking
arrive in chunks over seconds, so seconds-granularity liveness is adequate, and the whole web tier stays
request/response — Cloud-Run-native, no held connections.

### Durable trace — materialized at session end

Two writers feed our store, neither standing:

- **Internal services**, during the run — claims/gaps/verdicts and tool-call provenance, written host-side as the agent
  calls `record_claim` / `record_gap` / `record_verdict` ([`agent-runtime.md`](agent-runtime.md)); the web tier is not
  involved.
- **A one-shot backfill**, at session end — a `session.status_idled` / `session.status_terminated` webhook triggers a
  single `events.list` read that projects the event-stream-only telemetry (per-agent token/cache usage, thinking,
  sub-agent thread structure) into Cloud SQL. The webhook receiver is an HMAC-verified route **exempt from IAP**
  (Anthropic cannot present an IAP credential); its signing key is inventoried in
  [`spike-infrastructure.md`](spike-infrastructure.md) §4.

Anthropic's log is replayable, so the backfill needs no live consumer. We materialize despite that log because the trace
must be **reproducible independent of a beta API's retention**, must support **cross-session SQL analytics** (the
token-cost dashboard and gap/verdict queries `events.list` cannot serve —
[`spike-infrastructure.md`](spike-infrastructure.md) §5), and must **join our own entities** (claim → tool call → DB
version; comment/deep-link anchors).

### Auth

IAP is the gate ([`spike-infrastructure.md`](spike-infrastructure.md) §2). The app **verifies** IAP's signed
`X-Goog-IAP-JWT-Assertion` per request at a single default-on chokepoint ([`security.md`](security.md)): a proxy
default-deny perimeter (`apps/web/src/proxy.ts`) plus a shared request-scoped accessor (`server/context.ts`) that
re-verifies at the data seam, so no route reaches the backend unauthenticated. The proxy allowlists `healthz`; the
HMAC-verified webhook receiver is the other IAP-exempt surface in the design. The asserted email is the identity for
comment attribution and audit; roles, Project membership, and per-report authorization are app-DB, enforced by the BFF.
The browser carries the IAP cookie on same-origin requests (including the polling fetch).

### Data fetching

- **Stored state** (projects, analyses, working-document versions, reports, projected trace, comments) — REST/JSON over
  the BFF, types generated from the protos as protobuf-es ([`proto.md`](proto.md)), TanStack Query on the client. Not
  GraphQL: a single first-party client does not earn it.
- **Liveness** — polling, as above.

### Component library

shadcn/ui (Radix primitives + Tailwind, copy-in so we own the code). Off-the-shelf component libraries (Mantine/Chakra)
are heavier and more opinionated; the curator-facing surfaces (evidence hierarchy, trace) are bespoke on primitives
regardless.

### Trace visualization

A **virtualized, live-appending tree/timeline** of agent runs (coordinator → sub-agent threads → tool calls → emitted
claims/gaps), with claims and verdicts cross-linked to the working document and ACMG cells. Not a force-directed graph
(over-built for the Spike). [`trace-dag`](https://github.com/populationgenomics/trace-dag) — the React/SVG layered-DAG
renderer extracted from pubmedifier — is one candidate viewer; the renderer choice defers to
[`trace-schema.md`](trace-schema.md).

### Comments

Anchored annotations (PR-review / margin style). A comment always targets a span in a working-document markdown file:
anchor `(doc_id, start, end)` where `start`/`end` are **Unicode code-point offsets** (Python `str` indices), not UTF-16
code units. The Python tool tier and the JS browser must agree on the unit, and JS strings index by UTF-16 code unit, so
they diverge on supra-BMP characters; the browser converts the code-point anchor to UTF-16 at render time. flowa
([`populationgenomics/flowa`](https://github.com/populationgenomics/flowa)) anchors the same way (code-point spans into
assembled markdown, converted browser-side). Record shape `{doc_id, start, end, author=IAP email, body, thread}` in
Cloud SQL.

## Out of scope

- Visual-design polish, copywriting, mobile (desktop curator workflow).
- The working-document editor internals and how span anchors rebase under edits (owned with the working-document /
  trace-schema work).
- Auth, runtime, and trace mechanics owned by the sibling docs referenced above.

## Open questions

- **Live-narration latency** — poll-through (~2–3 s, stateless, the baseline) vs an SSE pass-through (sub-second, a held
  connection with Cloud Run's 60-min request cap → `EventSource` auto-reconnect). Add SSE if curators want real-time
  push; host where it suits (Cloud Run, or GCE/GKE).
- **`events.list` incremental cursor** — a clean `since` / `starting_after` parameter vs.
  fetch-newest-and-dedupe-by-event-`id`; confirm against the Managed Agents API at build.
- **BFF data-access shape** — RSC/server-actions reading Cloud SQL directly vs. explicit API routes with client
  fetching; a within-Next.js tuning call, deferred to build.
- **Editor choice** — ProseMirror vs. Lexical vs. Tiptap for the working document; resolved with the editable-artifact
  work.
