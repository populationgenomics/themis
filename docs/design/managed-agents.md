# Design: Managed Agents wiring

**Status:** current **Related:** [`agent-runtime.md`](agent-runtime.md) (runtime semantics — coordinator, sub-agents,
structured output, trace), [`frontend-framework.md`](frontend-framework.md) (web tier, poll-through, auth),
[`workspace-model.md`](workspace-model.md) (Project / Analysis / working document, and the authorization boundary),
[`../plans/self-hosted-sandbox.md`](../plans/self-hosted-sandbox.md) (execution sandbox, credential proxy, session
tokens), [`spike-infrastructure.md`](spike-infrastructure.md) (deployables, identities, write boundary),
[`security.md`](security.md) (chokepoint rule).

## Overview

One case, from submission to a persisted Analysis, crosses every doc above and is owned by none of them. This doc is the
map: the order of the hops, and which doc decides each. It states no mechanism the linked docs already state.

## Design

**The vertical.**

1. **Auth** — the curator reaches the app through IAP; the app verifies the assertion at its chokepoint. →
   [`frontend-framework.md`](frontend-framework.md) §Auth, [`security.md`](security.md)
1. **Create** — the BFF writes the Analysis row, bound to a Project the caller belongs to, and creates the Managed
   Agents session under WIF Path B. → [`workspace-model.md`](workspace-model.md),
   [`../runbooks/claude-api-wif.md`](../runbooks/claude-api-wif.md)
1. **Execute** — the session's tool calls run in our sandbox. The agent works the case in code mode and authors
   `/workspace/document.md`; it holds no credential. →
   [`../plans/self-hosted-sandbox.md`](../plans/self-hosted-sandbox.md) §6–§9, [`agent-runtime.md`](agent-runtime.md)
1. **Observe** — the browser polls the BFF, which reads Anthropic's event log, authorizes the caller against Project
   membership, and projects it to the display model. → [`frontend-framework.md`](frontend-framework.md) §Session
   observation
1. **Steer** — a curator interjection is a POST to the BFF that sends a `user.message` to the same session. →
   [`frontend-framework.md`](frontend-framework.md)
1. **Session end** — Anthropic's delivery is HMAC-verified by the dispatcher, the only public non-IAP surface; the
   durable trace is materialized from one `events.list` read. → [`frontend-framework.md`](frontend-framework.md)
   §Durable trace

**Identity split.** Anthropic runs the loop and holds no credential of ours beyond the session it was created with. The
BFF holds the Anthropic identity and a display-scoped Cloud SQL identity. The internal services hold the write identity
for agent-authored content. The sandbox holds none — the credential proxy injects the per-session token on the way out.
Each is stated once in [`spike-infrastructure.md`](spike-infrastructure.md) §8.

## Alternatives considered

- **MCP servers reached by the managed loop** — the agent would call tightly-typed MCP tools through a tunnel, with a
  per-session bearer minted per Analysis. Rejected: it requires a beta tunnel we don't hold or an Anthropic-reachable
  endpoint of ours, and an LLM drives long chains of discrete tool calls less reliably than code against generated
  stubs. Code mode over internal services replaces it — see
  [`../plans/self-hosted-sandbox.md`](../plans/self-hosted-sandbox.md).
- **A webhook receiver on the BFF** — session-end deliveries would land on an IAP-exempt path of the web app, reached
  through a second backend service with IAP disabled. Rejected: it puts an unauthenticated surface on the IAP-gated tier
  to duplicate what the dispatcher already does.

## Implementation state

The vertical is built end to end against the self-hosted sandbox; the durable trace and the source view are not. See
each linked doc for its own state.
