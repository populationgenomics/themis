# Design: Agent runtime for the Themis Spike

**Parent epic:** [`issues/epic-themis-spike.md`](../../issues/epic-themis-spike.md) (PR #1) **Related:**
[`spike-infrastructure.md`](spike-infrastructure.md) §8 owns the *infra* consequences (project, secrets, CI images,
sandbox build, egress); this doc owns the *runtime* semantics. [`deployment.md`](deployment.md) governs confidential
model config.

## Why this exploration

The Spike runs several agent concerns over one `(variant, condition)` case — evidence gather (deterministic and
agentic), aggregation to ACMG-V4 cells, holistic reasoning, adversarial review — sharing tool calls against a curated
registry, structured output (claims, gaps, verdicts), trace emission, and per-agent model selection. The runtime choice
fixes where the agent loop runs, how multi-agent work is scheduled, how the seams between roles are typed, and what
crosses the data boundary.

## What Anthropic runs vs. what we host

**Anthropic runs the agent loop and the multi-agent orchestration; we host only the tool surface and the execution
sandbox.** This is the Managed Agents split — a hosted REST API where the loop, per-session container, session/event
log, and coordinator scheduling are Anthropic's — paired with a **self-hosted execution sandbox** so code execution
stays inside CPG's network ([`spike-infrastructure.md`](spike-infrastructure.md) §8).

- **Tools** — our tightly-typed MCP servers ([`tool-surface.md`](tool-surface.md)) are reached by the managed loop
  through MCP tunnels; no public endpoint. The servers that read private data (Cloud SQL/GCS) hold the GCP identity; the
  sandbox holds none and never touches the store directly (data-plane mediation — §8).
- **Execution sandbox** — the self-hosted worker runs the agent's `bash` and generated code under our egress policy.
- **Session client** — the web-app backend creates a session per submitted case and consumes its event stream (to drive
  the workspace UI and write the trace). It is a thin client, **not** a workflow conductor: it starts the session and
  observes; it does not sequence the agents.

There is no orchestrator in our code. Where [`spike-infrastructure.md`](spike-infrastructure.md) §8 calls the web app
the "orchestrator/session-client," it means this session client — not a conductor; the data-plane mediation is the MCP
tier's (above).

## Decision: Managed Agents, coordinator-driven

Use **Managed Agents** as the runtime, with its **`multiagent` coordinator** as the model-driven orchestrator. A
coordinator agent, given the case, decides per case how to gather, aggregate, reason, and review, delegating to
sub-agent threads. Scheduling is the model's; we supply only the scenario specialization — guiding prompt,
working-document outline, tool surface, and the roster of sub-agents the coordinator may delegate to. This is PRODUCT §4
("the orchestrator decides scheduling per case… the framework scaffolds evidence, not agent topology") and the
Bitter-Lesson stance of §6 (dynamic, model-composed workflows; just enough fixed scaffold to guarantee evidence
coverage, traceability, and eval).

### Why Managed Agents

It skips building and operating the agent loop, the execution-sandbox lifecycle, per-session state, and the event stream
— fast results, the Spike's goal — while the parts we do build (the tool/MCP servers, the data-plane mediation) are
runtime-independent and carry over if we ever move off it ([`spike-infrastructure.md`](spike-infrastructure.md) §8). The
dependency it adds is Anthropic's (beta) agent API.

### Why not the alternatives

- **Agent SDK (loop in our container) + Dynamic Workflows.** The Agent SDK runs the loop in our process, and its Dynamic
  Workflows feature lets the model author a JavaScript orchestration script that fans out sub-agents — an attractive
  model-composed-orchestration story. But *we* would then own the loop, session state, scaling, and — the deciding cost
  — a runtime to execute the model-generated workflow plus a mechanism for the sandbox to launch further agents. That is
  orchestration infrastructure we would build and operate, and it pulls agent-launching into the hardened execution
  sandbox we are trying to keep minimal. The Managed Agents coordinator gives model-driven fan-out with none of that on
  us — and is the more adaptive of the two: a Dynamic Workflow fixes its plan when the model writes the script, whereas
  the coordinator decides each next delegation from the actual results returned to it, so the decomposition need not be
  anticipated in advance. (Dynamic Workflows stays a future lever should Themis ever self-host orchestration.)
- **Roll-your-own over the Messages API.** Rebuilds the loop, sub-agent dispatch, and session state we would otherwise
  inherit.
- **Claude Code as a subprocess.** Headless invocation plus stdout parsing loses structured output and turns trace
  emission into a transcript scrape.
- **LangGraph / DSPy / Inspect-AI.** Ceremony out of proportion to the Spike; a declarative-program model that is
  premature; eval-shaped rather than a production runtime (Inspect-AI is the likely tool for the eval exploration, not
  this).

## Topology: a coordinator over specialized sub-agents

A coordinator agent holds the long-running session context and delegates work to sub-agents — each running in its own
context-isolated thread with its own history, model, system prompt, and tools (they share the sandbox, filesystem, and
vault credentials, but not context). It fans sub-agents out in parallel, incorporates each result as it returns, and
steers the next round from what came back, so it keeps an overview of a long case without its own context window filling
with every gatherer's raw output. Threads persist: the coordinator can follow up with a sub-agent it called earlier,
which retains its prior turns. Limits: one level of delegation (a sub-agent's own roster is ignored), up to 20 roster
agents, and up to 25 concurrent threads (it may spawn multiple copies of a roster agent). See Anthropic's
[Multiagent sessions](https://platform.claude.com/docs/en/managed-agents/multi-agent) docs.

The roles map onto that coordinator and a thin roster of sub-agents it may delegate to — kept as light as eval allows,
not a fixed pipeline and not one-agent-per-cell:

- **Deterministic gather** (gnomAD AF, ClinVar structured fields, predictor scores) is **not** an agent — it is baseline
  annotations precomputed upstream and/or tool calls the coordinator makes; it surfaces through the tool/context
  surface.
- **Agentic gatherers** (ClinVar free text, literature, gene–disease validity) are sub-agents with focused tool subsets,
  delegated to as the coordinator sees fit.
- **Aggregation** to ACMG-V4 cells is deterministic or agentic per [`aggregator.md`](aggregator.md); either way the
  reasoner operates on claims and cell tags, not on the rolled-up score (PRODUCT §6).
- **Reasoner** and **reviewer** are sub-agents. The reviewer evaluates the produced artifact from a **fresh context** —
  adversarial review needs a separate context to beat self-review (PRODUCT §6, §11), which the sub-agent-thread model
  gives directly.

Each roster sub-agent is its own versioned Agent, so per-role tool-scoping and per-role model selection fall out for
free. How much the coordinator decomposes versus working in fewer, broader agents is the scaffold-vs-autonomy dial
(PRODUCT §11) — set by eval, widened as the model proves it can own more.

## Structured output: typed calls into our MCP tools

Managed Agents has no per-session output-schema enforcement, and a client-side custom tool would drag the thin session
client back into a handling loop. Instead, claims/gaps/verdicts are emitted by the agent **calling our tightly-typed MCP
tools** — `record_claim` / `record_gap` / `record_verdict`, or writes to the working document. Each call routes through
the MCP tunnel to our MCP server, which:

- validates the payload against the schema generated from [`proto.md`](proto.md);
- persists it to our store (the server holds the GCP identity; the sandbox never touches the store); and
- writes the matching trace record.

The structured-output contract is therefore part of the tool surface — no prose parsing, no file scraping, no
custom-tool round-trip. The **working document** (PRODUCT §7) is the durable artifact, grown through these typed calls.

## Untrusted gathered content

Gathered ClinVar free text and literature are **untrusted content** (PRODUCT §9): the coordinator and its gatherers read
third-party text that can carry instructions injected to steer the model's tool use or its `record_*` output. The
runtime treats that text as data, not instructions; it adds no separate instruction/data filter and relies on two
properties the rest of the design already buys:

- **Typed tool surface** — tools take constrained arguments (enums, not free-form strings — PRODUCT §9,
  [`tool-surface.md`](tool-surface.md)), so an injected instruction cannot widen what a tool reads or what
  `record_claim` / `record_gap` / `record_verdict` persists; at most it supplies in-vocabulary values, which the trace
  still attributes to their source.
- **Nothing to exfiltrate to** — the sandbox holds no GCP identity and runs under the egress policy (data-plane
  mediation, [`spike-infrastructure.md`](spike-infrastructure.md) §8), so a steered call reaches neither private data
  nor an open channel.

The fresh-context reviewer is a partial backstop: it can reject a verdict the evidence does not support, but not an
injected tool call mid-gather. Hardening the injection leg beyond this is tool-surface design, owned by
[`tool-surface.md`](tool-surface.md).

## Trace integration

The trace ([`trace-schema.md`](trace-schema.md)) has two feeders, both consistent with hosting only tools and sandboxes:

- **Our MCP servers** write provenance-rich records host-side on each tool call (URL, args, response hash,
  source-database version) — they are where the call actually executes.
- **The session client** projects the Managed Agents event stream into the trace vocabulary: `span.model_request_end`
  for per-agent token/cache usage, `agent.tool_use` / `agent.mcp_tool_use` for calls, `agent.thinking`, and the
  `session.thread_*` per-thread streams for sub-agent activity — the coordinator fans out across threads, so the client
  consumes those, not just the primary stream.

Mapping: session/thread → `AgentRun`; tool calls → `ToolCall` (+ `ToolCallIntent`); the typed emits → `EvidenceClaim` /
`InformationGap` / `Verdict`.

## Model selection

The per-agent model id lives on the Agent object and is pushed at deploy time via `ant` from gcpkms-encrypted stack
config — it is secret-class confidential config (*Confidential config*, [`deployment.md`](deployment.md)): generic
statements are public, the concrete id and per-agent assignment are not. Because each roster sub-agent is its own Agent,
per-role selection (a frontier model for the reasoner; a faster model where tool-use quality allows for cheap lookups)
is a deploy-time choice, not a runtime branch.

## Configuration and lifecycle

Control plane / data plane split, per [`spike-infrastructure.md`](spike-infrastructure.md) §6/§8: agents and
environments are version-controlled YAML applied via the `ant` CLI from CI; sessions are created and driven from the web
backend via the SDK.

Much of the lifecycle is the platform's: automatic prompt caching within a session (the session keeps the 5-minute-TTL
cache alive across the run), context compaction, and rescheduling on retryable errors. We add a per-session timeout, the
idle-break gate (break on a terminal `stop_reason`, not on transient idle), the post-idle status-write race before
cleanup, `user.interrupt` for cancellation, and handling for a stuck sub-agent thread.

## Open questions

- **Per-role model defaults** — a small benchmark settles which roles need a frontier model versus a faster one.
- **Aggregator shape** — deterministic vs. agentic vs. hybrid is owned by [`aggregator.md`](aggregator.md).
- **Coordinator decomposition** — how far the coordinator should fan out versus work in fewer agents (the §11
  scaffold-vs-autonomy dial); resolve via eval.
- **Caching cadence** — what a session's automatic caching covers across a run (tool registry, ACMG-V4 framework text,
  resolved-condition context) and whether explicit cache hints are warranted.
