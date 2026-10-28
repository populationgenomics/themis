# Design: frontend framework and web tier

**Related:** [`spike-infrastructure.md`](spike-infrastructure.md) (IAP, Cloud Run, and secret infrastructure),
[`workspace-model.md`](workspace-model.md) (Project, Analysis, working document, and Report entities),
[`agent-runtime.md`](agent-runtime.md) (Managed Agents coordinator and trace writers),
[`conversation-view.md`](conversation-view.md) (conversation thread rendering and curator turns),
[`workbench-navigation.md`](workbench-navigation.md) (routes and server-component page reads), [`proto.md`](proto.md)
(Connect RPC serialization and boundary validation), [`security.md`](security.md) (default-on request authentication).

## Overview

Themis serves an internal web application for CPG variant curators on Cloud Run behind Identity-Aware Proxy (IAP). The
initial Spike surface is small — a live conversation thread showing the coordinator and its sub-agents working, paired
with a report and result viewer — but it must grow into the full co-scientist workbench ([`PRODUCT.md`](../PRODUCT.md)
§7) without a framework rewrite. That workbench adds an editable working document, an interactive trace tree drilled in
place, and margin comments anchored to text spans and ACMG evidence cells.

Because the genomics tool tier is written in Python, the obvious choice is a Python web stack or a thin Python
backend-for-frontend (BFF). A Python full-stack framework breaks down once the UI takes on high-frequency client state;
a thin Python BFF keeps the backend in Python only by adding a cross-language boundary on the hot browser-to-BFF seam,
even though the BFF is domain-logic-light because the genomics logic lives in the tool tier and writes its outputs to
the store there. We instead place the language boundary at the agent's data plane and build the entire web tier — both
the browser UI and the BFF in [`apps/web/`](../../apps/web) — as a single full-stack TypeScript Next.js service.

The web tier runs as a stateless request/response Cloud Run service with no standing background worker and no held
streaming connections. To observe a live agent run, the browser polls the BFF every 2 to 3 seconds through the
[`Workbench`](../../schema/proto/themis/workbench/rpc/workbench.proto) Connect service, and the BFF reads and projects
Anthropic's persisted event log on each tick. Durable trace data reaches Cloud SQL through two non-standing paths
outside the web tier: internal services write structured records during the run, and a session-end webhook (planned;
which component performs the projection is an open question) triggers a one-shot backfill of stream telemetry.

## Background

When a curator opens an Analysis in the workbench, three runtime tiers participate in the session:

1. **Anthropic Managed Agents** hosts the agent loop and stores the canonical session transcript in a replayable event
   log (`events.list`).
1. **The agent-facing tool tier** runs Python services that mediate access to genomics data stores and execute code
   inside the self-hosted sandbox ([`agent-runtime.md`](agent-runtime.md),
   [`spike-infrastructure.md`](spike-infrastructure.md) §8). Only the sandbox reaches these Python services; the browser
   never calls them directly.
1. **The web tier** ([`apps/web/`](../../apps/web)) serves the curator UI and its BFF behind Google Cloud IAP. The BFF
   authenticates the curator's IAP assertion, checks authorization against Cloud SQL, reads and writes stored entities,
   and relays live session state between the browser and Anthropic.

```
                        IAP JWT cookie / assertion
  Curator Browser  ────────────────────────────────────►  Web Tier (Next.js BFF, Cloud Run)
                   ◄────────────────────────────────────    - verifies IAP at perimeter + data seam
                     HTML (Server Components) +             - serves Workbench Connect RPCs (POST)
                     Workbench Connect RPCs                 - no held connections, no webhooks
                                                            │                  │
                                     reads/writes           │                  │ creates session, Steer,
                                     typed rows             ▼                  ▼ Interrupt, events.list
                                                       Cloud SQL          Anthropic Managed Agents
                                                            ▲                  │
                                                            │                  │ session.status_* webhook
                                     record_claim /         │                  ▼ (HMAC-verified; planned)
                                     gap / verdict          │             Session-end projection (owner open)
                                     during run             │                  │
                                                Internal Python Services       └──► one-shot trace backfill
```

## Non-goals

- **Visual design polish, copywriting, and mobile layouts.** The workbench targets a desktop curator workflow.
- **Working-document editor internals and span-anchor rebasing across document edits.** Those mechanics belong to the
  working-document and trace-schema designs.
- **Infrastructure, runtime, and trace schemas owned by sibling docs.** IAP provisioning lives in
  [`spike-infrastructure.md`](spike-infrastructure.md), coordinator execution lives in
  [`agent-runtime.md`](agent-runtime.md), and conversation turn rendering lives in
  [`conversation-view.md`](conversation-view.md).

## Design

### Language boundary and component primitives

Because the genomics data services are written in Python, a natural starting point is to build the web app in a Python
full-stack framework (such as FastHTML, Reflex, NiceGUI, or HTMX) so the whole repository shares one language. That
works while the UI only renders static markdown with display-only links. It breaks as soon as the Spike grows into the
co-scientist workbench: an editable working document, margin comments anchored to character spans, and a virtualized
live-appending trace tree require high-frequency client state and React-first libraries (ProseMirror or Lexical,
TanStack Virtual, and custom annotation layers). Wrapping those libraries inside a Python UI framework forces the team
to maintain hand-rolled JavaScript bridges, turning the transition from the Spike to the workbench into a rewrite.

We place the language boundary at the agent's data plane rather than at the browser. Python stays confined to the
internal agent-facing services, whose implementation language is invisible to the web app. The web tier in
[`apps/web/`](../../apps/web) uses Next.js in full-stack TypeScript for both the React UI and the BFF. Because the BFF
contains no genomics domain logic — it only authorizes requests, reads and writes typed database rows, and projects
session events — nothing pulls it toward Python, and keeping the browser-to-BFF seam in one language speeds up iteration
during curator dog-fooding.

For UI primitives, the web tier uses `shadcn/ui` ([`apps/web/src/components/ui/`](../../apps/web/src/components/ui),
Radix primitives styled with Tailwind and copied into the repository so we own the component source) rather than a
pre-packaged widget suite like Mantine or Chakra. Curator-facing surfaces such as the evidence hierarchy and trace tree
are custom assemblies built on top of focus, dialog, and popover primitives; copying the primitive wrappers into the
codebase costs a small amount of maintenance in exchange for direct control over markup and styling.

### Single request/response service

A web application that observes long-running agent sessions and records session telemetry typically deploys a web server
alongside a background worker process or a streaming relay. Operating a second always-on service adds deployment
coordination and idle compute cost.

Instead, the web tier deploys as a single Cloud Run service built with Next.js `standalone` output
([`apps/web/next.config.ts`](../../apps/web/next.config.ts)) configured to scale to zero, with no standing background
worker. After an idle gap, the first curator request waits for a cold start — about 5 to 7 seconds end to end on the
development deployment in October 2026, which is acceptable — and once a curator opens an active session, periodic
browser polling keeps the Cloud Run instance warm for the duration of their work. The cold start stays that short only
while the startup probe checks every second and the server loads its cloud SDKs once per process rather than once per
route.

### Session observation

When an agent run takes several minutes, the standard way to show live progress is to hold an HTTP streaming (SSE) or
WebSocket connection open between the browser, the BFF, and the agent runtime. Held connections complicate Cloud Run
scaling, require connection-lifecycle recovery across proxy timeouts, and couple the web tier to the duration of the
agent run.

Because Anthropic Managed Agents runs the agent loop and persists every turn and tool event to its own log
([`agent-runtime.md`](agent-runtime.md)), the web tier never holds a connection open for a run:

- **Kickoff:** The BFF creates the Managed Agents session and sends the case input as a `user.message` in a standard
  request/response call.
- **Live conversation thread:** While a session is active, the browser polls the BFF every 2 to 3 seconds via the `Poll`
  RPC. On each tick, the BFF reads Anthropic's persisted event log (`events.list`), checks the caller's IAP identity
  against Project membership, projects the raw events into the workbench display model, and returns the entire projected
  stream. The browser replaces its local stream state by item ID rather than appending deltas. The BFF holds the
  Anthropic API key and acts as the authorization and projection boundary; the browser never contacts Anthropic
  directly, and the BFF relays Anthropic's log without copying it into Cloud SQL during the run.
- **Steering and interruption:** When a curator interjects during a run, the browser calls `Steer`, which posts the
  curator's message as a `user.message` on the Analysis's existing session while `Poll` remains the single authority on
  the conversation state ([`conversation-view.md`](conversation-view.md)). Calling `Interrupt` halts the active run on
  that session.

Polling every 2 to 3 seconds trades sub-second token streaming for a completely stateless request/response web tier.
Because coordinator synthesis and sub-agent tool blocks arrive in coarse chunks over several seconds, seconds-level
liveness fits the progressive-disclosure workflow ([`PRODUCT.md`](../PRODUCT.md) §7).

### Durable trace

Although the BFF relays live conversation events directly from Anthropic's `events.list` without storing them on each
poll tick, relying solely on Anthropic's event log for historical traces fails three requirements:

1. **Retention independence:** Curation traces must remain reproducible regardless of a beta API's log retention window.
1. **Cross-session SQL analytics:** The token-cost dashboard and cross-case gap or verdict queries require relational
   aggregation that `events.list` cannot serve ([`spike-infrastructure.md`](spike-infrastructure.md) §5).
1. **Entity joins:** Trace items must join directly against our own Cloud SQL tables, linking claims to tool calls,
   database versions, comment anchors, and deep links.

To populate Cloud SQL without adding a background stream consumer to the web tier, two event-driven writers record the
trace:

- **Internal services during the run:** As the agent invokes `record_claim`, `record_gap`, and `record_verdict`, the
  internal Python services write claims, gaps, verdicts, and tool-call provenance directly to Cloud SQL
  ([`agent-runtime.md`](agent-runtime.md)). The web tier is not in this path.
- **One-shot backfill at session end:** Because Anthropic's event log is replayable, trace telemetry that exists only in
  the event stream (per-agent token and cache usage, thinking blocks, and sub-agent thread structure) only needs to be
  read once when the run stops. When Anthropic emits a `session.status_idled` or `session.status_terminated` webhook, a
  single `events.list` read projects that telemetry into Cloud SQL. Because Anthropic cannot present a Google Cloud IAP
  credential, webhook delivery lands on the dispatcher's HMAC-verified endpoint
  ([`self-hosted-sandbox.md`](../plans/self-hosted-sandbox.md), [`spike-infrastructure.md`](spike-infrastructure.md)
  §4). The web tier exposes no webhook route.

In the UI, the trace renders as a virtualized, live-appending tree and timeline (coordinator → sub-agent threads → tool
calls → emitted claims and gaps), with claims and verdicts cross-linked to the working document and ACMG grid cells.
[`trace-dag`](https://github.com/populationgenomics/trace-dag) — the React and SVG layered-DAG viewer extracted from
`pubmedifier` — is a candidate renderer, with the final viewer choice deferred to `trace-schema.md`.

### Auth

Google Cloud IAP fronts the Cloud Run service ([`spike-infrastructure.md`](spike-infrastructure.md) §2), but relying on
perimeter network configuration alone leaves the application vulnerable if a route is misconfigured or called without a
verified identity. Following the repository's single-chokepoint rule ([`security.md`](security.md)), the web tier
verifies IAP's signed `X-Goog-IAP-JWT-Assertion` header on every request at two layers:

1. **Request perimeter ([`apps/web/src/proxy.ts`](../../apps/web/src/proxy.ts)):** A default-deny proxy intercepts every
   incoming request and verifies the IAP JWT before routing. If verification fails, the proxy returns a
   Connect-formatted `unauthenticated` JSON body with HTTP 401 so generated Connect clients parse a structured error
   code rather than failing on an empty response body.
1. **Data seam ([`apps/web/src/server/context.ts`](../../apps/web/src/server/context.ts)):** Server components and RPC
   handlers obtain their backend handle through a shared request-scoped accessor that re-verifies the request assertion
   and scopes all data-plane access to the authenticated curator's Projects. No route handler or page can query backend
   state without passing through this accessor.

The proxy allowlists only the container liveness probe route (`/api/healthz` in
[`apps/web/src/app/api/healthz/`](../../apps/web/src/app/api/healthz)). Cloud Run's startup probe connects directly to
the container, bypassing the load balancer and IAP, so it carries no JWT assertion. Allowlisting `/api/healthz` in the
proxy does not expose it to the internet: Cloud Run ingress is locked to the load balancer, and only the IAP service
agent holds `roles/run.invoker`. The web tier has no unauthenticated public surface.

Once authenticated, the asserted email from the IAP JWT identifies the curator for comment attribution and audit logs,
while roles, Project membership, and per-report access checks live in the application database and are enforced by the
BFF. Because the browser automatically sends the IAP session cookie on same-origin requests, both page navigations and
Connect RPC calls carry the curator's identity.

### Data fetching

To move typed state between the browser and the BFF, the web tier uses the
[`Workbench`](../../schema/proto/themis/workbench/rpc/workbench.proto) Connect service generated by `protobuf-es`
([`proto.md`](proto.md)), paired with TanStack Query on the client ([`apps/web/src/lib/`](../../apps/web/src/lib)):

- **Client-fetched state and liveness:** Stored entities that update or refetch on the client (projects, analyses,
  working-document versions, reports, projected traces, and comments) and live session polling (`Poll`, `Steer`,
  `Interrupt`) flow through the `Workbench` Connect service. Every method on this surface — including read-only `Poll`
  ticks — uses HTTP `POST` ([`proto.md`](proto.md)). React hooks call the generated Connect client; application code
  never calls raw `fetch`.
- **Page-lifetime state:** State that is fixed for the lifetime of a page is read directly by the page's React Server
  Component from the authorized backend before rendering ([`workbench-navigation.md`](workbench-navigation.md)). If a
  curator requests an entity ID that does not exist or belongs to a Project they cannot access, the server component
  returns an HTTP 404 before any client shell renders.
- **Single RPC mount:** One App Router catch-all route under `/api/rpc/` serves the entire `Workbench` service through a
  thin Fetch API adapter in [`apps/web/src/server/rpc/`](../../apps/web/src/server/rpc) around `@connectrpc/connect`'s
  universal handler.

#### Error masking and validation layering

If an RPC handler throws an unhandled database error or a downstream internal service returns a diagnostic message,
letting Connect serialize that error directly to the browser leaks internal table names, stack traces, or service
topology. Conversely, if a single blanket catch masks every error as `internal`, a client that sends a malformed request
loses the field-level `protovalidate` violations explaining what was wrong with its own input.

The RPC pipeline in [`apps/web/src/server/rpc/`](../../apps/web/src/server/rpc) solves this by layering `protovalidate`
outside the error-masking interceptor and wrapping the outer HTTP mount a second time:

```
  Incoming POST /api/rpc/themis.workbench.rpc.Workbench/<Method>
    │
    ▼
  ┌─ Outer mount mask (maskInternal in handler.ts) ───────────────────────────┐
  │  Connect interceptors wrap only the method call, so wire-level failures   │
  │  (request decoding, reply serialization, timeout header parsing) settle   │
  │  outside the chain. Inspects outgoing 500 responses (non-500 client wire  │
  │  errors pass through with their Connect status), lets data_loss pass,     │
  │  logs any other unmasked 500 server-side, and rewrites the body to        │
  │  {"code":"internal","message":"internal server error"}                    │
  │                                                                           │
  │  ┌─ 1. Request validation interceptor (@connectrpc/validate) ──────────┐  │
  │  │  Validates caller's request message against proto rules.            │  │
  │  │  Invalid caller input ──► returns invalid_argument + field details  │  │
  │  │                                                                     │  │
  │  │  ┌─ 2. Error-masking interceptor ────────────────────────────────┐  │  │
  │  │  │  Maps domain errors to Connect codes (e.g. not_found with     │  │  │
  │  │  │  generic message that never names the resource).              │  │  │
  │  │  │  Everything else — including an InvalidArgument returned by   │  │  │
  │  │  │  a downstream service the BFF called — is logged server-side  │  │  │
  │  │  │  and masked to internal.                                      │  │  │
  │  │  │                                                               │  │  │
  │  │  │  ┌─ 3. Identity interceptor + service method ──────────────┐  │  │  │
  │  │  │  │  Verifies IAP identity at data seam and executes RPC    │  │  │  │
  │  │  │  └─────────────────────────────────────────────────────────┘  │  │  │
  │  │  └───────────────────────────────────────────────────────────────┘  │  │
  │  └─────────────────────────────────────────────────────────────────────┘  │
  └───────────────────────────────────────────────────────────────────────────┘
```

For example, suppose a curator's browser sends an empty `analysis_id` on `Steer`. The validation interceptor rejects the
request at layer 1 before the error mask runs, so the browser receives `invalid_argument` with the field violation on
`analysis_id`. By contrast, suppose the caller's request is valid, the BFF calls an internal service, and that internal
service rejects the BFF's outbound request with `InvalidArgument` because of a bug in how the BFF constructed it.
Because that error is raised inside layer 2, the error-masking interceptor logs it server-side and returns `internal` to
the browser. Furthermore, because Connect interceptors wrap only the parsed method invocation, wire-level failures
(request decoding, reply serialization, or timeout header parsing) settle inside the HTTP handler outside the
interceptor chain. Non-`500` client wire errors (such as malformed JSON, unknown compression, or an invalid timeout
header) return their Connect status directly; the outer mount mask (`maskInternal` in `handler.ts`) inspects outgoing
`500` responses, lets `data_loss` pass through, logs any other unmasked `500` server-side, and rewrites the body to
`{"code":"internal","message":"internal server error"}`.

#### Caching posture

Because the browser authenticates with an IAP cookie rather than an HTTP `Authorization` header, standard HTTP caches
(RFC 9111) do not automatically treat responses as private. If a CDN or forward proxy cached a response keyed only by
URL, one curator could receive another curator's response.

To prevent cross-curator cache hits:

- App Router route handlers never opt into `GET` caching, and the RPC mount in
  [`apps/web/src/server/rpc/`](../../apps/web/src/server/rpc) attaches `Cache-Control: private, no-store` to every RPC
  reply.
- Every page whose server component reads per-caller state opts into dynamic rendering (`force-dynamic`). Without
  dynamic rendering, Next.js would attempt to prerender the route at build time (where no request or IAP assertion
  exists) or serve one caller's rendered HTML to another from the Full Route Cache.

### Comments

Curators leave margin comments (similar to pull-request review annotations) anchored to spans of a working-document
markdown file: `(doc_id, start, end)`.

An obvious representation is to store `start` and `end` as string indices from the browser's JavaScript selection.
However, JavaScript strings index by UTF-16 code units, whereas Python `str` objects in the tool tier index by Unicode
code points. Any character outside the Basic Multilingual Plane (above `U+FFFF`, such as a mathematical symbol like `𝕏`
or an emoji) occupies one Unicode code point in Python (`len("𝕏") == 1`) but a two-unit surrogate pair in JavaScript
(`"𝕏".length === 2`). If a document contains ten such characters before a comment span, a JavaScript UTF-16 offset
drifts by 10 characters from the Python tool tier's view of the same markdown file.

To keep the Python tool tier and the browser aligned on a single coordinate system, stored comment anchors
`(doc_id, start, end)` always use **Unicode code-point offsets** (Python `str` indices), and the browser converts
code-point offsets to and from UTF-16 indices at render time. This matches the anchoring convention used in
[`populationgenomics/flowa`](https://github.com/populationgenomics/flowa). Comment records are stored in Cloud SQL with
the shape `{doc_id, start, end, author, body, thread}`, where `author` is the curator's IAP-asserted email.

## Alternatives considered

- **Python full-stack frameworks (FastHTML, Reflex, NiceGUI, HTMX):** Rejected because the editable working document,
  anchored margin comments, and virtualized live-appending trace tree require high-frequency client state and
  React-first libraries that these frameworks force into hand-wrapped JavaScript bridges.
- **SvelteKit:** Provides the same full-stack TypeScript boundary as Next.js, but has a thinner library ecosystem for
  rich-text editors, virtualization, and text-annotation layers, which are predominantly React-first.
- **Standalone React SPA plus a separate API service:** Because the BFF is thin and shares its TypeScript types with the
  UI, Next.js's integrated UI and route-handler model is simpler to deploy and maintain than two separate services.
- **GraphQL for the BFF:** A single first-party client does not need caller-driven field selection; `protobuf-es`
  Connect RPCs give end-to-end generated types and validation with less ceremony ([`proto.md`](proto.md)).
- **Off-the-shelf widget libraries (Mantine, Chakra):** Heavier and more opinionated than `shadcn/ui` (Radix +
  Tailwind), whereas our core curation surfaces require custom composition over unstyled accessible primitives.
- **Force-directed graph for the trace viewer:** Over-built for inspecting agent runs; a virtualized layered tree and
  timeline matches the hierarchical structure of coordinator, sub-agent, and tool-call execution.

## Open questions

- **Live-narration latency:** Whether 2 to 3 second stateless polling (`Poll`) remains sufficient once curators use the
  workbench daily, or whether we should add an SSE pass-through (sub-second latency over a held connection subject to
  Cloud Run's 60-minute request cap with `EventSource` auto-reconnect, hosted on Cloud Run or GCE/GKE).
- **`events.list` incremental cursor:** Whether the Managed Agents `events.list` endpoint supports a clean `since` or
  `starting_after` cursor parameter, or whether the BFF should fetch the newest page and deduplicate by event `id`.
- **Working-document editor choice:** Whether to build the editable working document on ProseMirror, Lexical, or Tiptap.
- **Session-end trace projection runner:** Which component executes the one-shot `events.list` trace projection into
  Cloud SQL once the dispatcher receives the `session.status_idled` or `session.status_terminated` webhook.
