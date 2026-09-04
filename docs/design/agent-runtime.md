# Design: Agent runtime for the Themis Spike

**Parent epic:** [`issues/epic-themis-spike.md`](../../issues/epic-themis-spike.md) (PR #1) **Related:**
[`spike-infrastructure.md`](spike-infrastructure.md) §8 owns the *infra* consequences (project, secrets, CI images,
sandbox build, egress); this doc owns the *runtime* semantics. [`deployment.md`](deployment.md) governs confidential
model config. [`conversation-view.md`](conversation-view.md) reads a session's threads back for the curator.

## Why this exploration

The Spike runs several agent concerns over one `(variant, condition)` case — evidence gather (deterministic and
agentic), aggregation to ACMG-V4 cells, holistic reasoning, adversarial review — sharing tool calls against a curated
registry, structured output (claims, gaps, verdicts), trace emission, and model selection. The runtime choice fixes
where the agent loop runs, how multi-agent work is scheduled, how the seams between roles are typed, and what crosses
the data boundary.

## What Anthropic runs vs. what we host

**Anthropic runs the agent loop and the multi-agent orchestration; we host only the tool surface and the execution
sandbox.** This is the Managed Agents split — a hosted REST API where the loop, per-session container, session/event
log, and coordinator scheduling are Anthropic's — paired with a **self-hosted execution sandbox** so code execution
stays inside CPG's network ([`spike-infrastructure.md`](spike-infrastructure.md) §8).

- **Tools** — the agent reaches our internal services in **code mode**: it writes code against their generated stubs
  rather than issuing chains of discrete tool calls. The services are gRPC and internal-only; those that read private
  data (Cloud SQL/GCS) hold the GCP identity. The sandbox holds no credential — a sandbox-local proxy injects the
  per-session one on the way out, so the agent never touches the store directly (data-plane mediation — §8).
- **Execution sandbox** — the self-hosted worker runs the agent's `bash` and generated code under our egress policy.
- **Session client** — the web-app backend creates a session per submitted case and consumes its event stream (to drive
  the workspace UI and write the trace). It is a thin client, **not** a workflow conductor: it starts the session and
  observes; it does not sequence the agents.

There is no orchestrator in our code. Where [`spike-infrastructure.md`](spike-infrastructure.md) §8 calls the web app
the "orchestrator/session-client," it means this session client — not a conductor; the data-plane mediation is the
services tier's (above).

## Decision: Managed Agents, coordinator-driven

Use **Managed Agents** as the runtime, with its **`multiagent` coordinator** as the model-driven orchestrator. A
coordinator agent, given the case, decides per case how to gather, aggregate, reason, and review, delegating to
sub-agent threads that are copies of itself (§Topology). Scheduling is the model's; we supply only the scenario
specialization: the guiding prompt and tool surface, configured on the runtime, and the working document's outline,
carried by the kickoff the session opens with ([`analysis-scenarios.md`](analysis-scenarios.md)). This is PRODUCT §4
("the orchestrator decides scheduling per case… the framework scaffolds evidence, not agent topology") and the
Bitter-Lesson stance of §6 (dynamic, model-composed workflows; just enough fixed scaffold to guarantee evidence
coverage, traceability, and eval).

### Why Managed Agents

It skips building and operating the agent loop, the execution-sandbox lifecycle, per-session state, and the event stream
— fast results, the Spike's goal — while the parts we do build (the internal services, the data-plane mediation) are
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

## Topology: a coordinator delegating to copies of itself

A coordinator agent holds the long-running session context and delegates work to sub-agent threads — each a
context-isolated thread with its own history, sharing the sandbox and filesystem but not context. It fans threads out in
parallel, incorporates each result as it returns, and steers the next round from what came back, so it keeps an overview
of a long case without its own context window filling with every gatherer's raw output. Threads persist: the coordinator
can follow up with a thread it opened earlier, which retains its prior turns. The platform's limits: one level of
delegation — a thread cannot delegate in turn — and at most 25 concurrent threads. See Anthropic's
[Multiagent orchestration](https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration) docs.

### Every delegate is a copy of the coordinator

The platform configures a coordinator with a **roster**: the list of agent definitions it may delegate to, each an Agent
object in the platform's vocabulary — a model, a system prompt, tools and skills. A roster entry may name another
definition, or it may name the coordinator itself, in which case a delegation opens a thread running the coordinator's
own definition. A Themis scenario's roster names only the coordinator itself. A delegate therefore runs the scenario's
whole definition — the same guiding prompt, carried as the skill attached to the agent, the same tool surface and the
same model — from a fresh context, and the coordinator's brief is all that makes one thread a literature sweep and
another a review: prose saying what to do, what the coordinator already holds, and what to report back. The brief's
shape is part of the guiding prompt and versions with it; its content is the run's.

The reason is what a delegate needs. Every role here reads evidence — a sweep fetches and quotes papers, a reviewer
re-reads what the draft cites — and reading evidence takes the scenario's tool surface (the sandbox and the services
reached from it) and the guiding prompt that says how to use it. A roster entry naming another definition carries none
of the coordinator's: its tools, skill and model would be declared a second time on that definition and kept in step
with the coordinator's, and what that buys is a narrower tool surface or a cheaper model for one role. The decision
flips on either of two conditions: a platform on which a named entry inherits the coordinator's definition, or a role
that eval shows wanting a narrower surface or a cheaper model. Either way the change is one of configuration; the
threads and their briefs stay as they are.

What follows:

- **No per-role scoping.** Every thread runs the coordinator's model with the whole tool surface; what a thread may do
  is bounded by its brief, which is prose, not by configuration.
- **A delegate does one whole job.** The platform allows one level of delegation, so a copy cannot fan out in turn: a
  job handed to a copy runs whole in its thread — a case analysis's candidate variant, classified in a copy, gets its
  literature sweep there rather than from a further copy.
- **Threads are told apart by their briefs.** Every thread reports the coordinator's own agent name in the session's
  event stream, so the name distinguishes nothing; the workbench shows a thread by its brief
  ([`conversation-view.md`](conversation-view.md)).

### The roles

The roles map onto the coordinator and the copies it briefs — kept as light as eval allows, not a fixed pipeline and not
one-agent-per-cell:

- **Deterministic gather** (gnomAD AF, ClinVar structured fields, predictor scores) is **not** an agent — it is baseline
  annotations precomputed upstream and/or tool calls the coordinator makes; it surfaces through the tool/context
  surface.
- **Agentic gather** (ClinVar free text, literature, gene–disease validity) is the coordinator's own work or a copy's,
  as it sees fit. The literature sweep is the standing delegation: its raw material is what would fill the coordinator's
  context.
- **Aggregation** to ACMG-V4 cells is deterministic or agentic per [`aggregator.md`](aggregator.md); either way the
  reasoning operates on claims and cell tags, not on the rolled-up score (PRODUCT §6).
- **Reasoning and the working document** are the coordinator's. It holds the whole case, and judgement does not
  transfer: a copy's report is a claim the coordinator checks against what it gathered, never a conclusion it adopts.
- **Review** is a copy with a **fresh context**, briefed once the draft exists to read it against the evidence and
  report where they diverge — not to redo the work. Adversarial review needs a separate context to beat self-review
  (PRODUCT §6, §11), which a copy gives directly; the coordinator folds the findings into the draft. The platform's
  advisor — a model the primary thread consults mid-turn, handed a prompt the platform composes and holding no tools —
  is not a fit: a reviewer that cannot re-read the evidence re-reads only the prose.

How much the coordinator decomposes versus working in fewer, broader threads is the scaffold-vs-autonomy dial (PRODUCT
§11) — set by eval, widened as the model proves it can own more.

## Structured output: typed calls into our services

Managed Agents has no per-session output-schema enforcement, and a client-side custom tool would drag the thin session
client back into a handling loop. Instead, claims/gaps/verdicts are emitted by the agent **calling our tightly-typed
services** — `record_claim` / `record_gap` / `record_verdict`, or writes to the working document. The agent writes code
against the generated stubs; the call reaches the service through the sandbox-local proxy, and the service:

- validates the payload against the schema generated from [`proto.md`](proto.md);
- persists it to our store (the service holds the GCP identity; the sandbox never touches the store); and
- writes the matching trace record.

The structured-output contract is therefore part of the service surface — no prose parsing, no file scraping, no
custom-tool round-trip. The **working document** (PRODUCT §7) is the durable artifact, grown through these typed calls.

## Untrusted gathered content

Gathered ClinVar free text and literature are **untrusted content** (PRODUCT §9): the coordinator, and any copy it
briefs to gather, read third-party text that can carry instructions injected to steer the model's tool use or its
`record_*` output. The runtime treats that text as data, not instructions; it adds no separate instruction/data filter
and relies on two properties the rest of the design already buys:

- **Typed tool surface** — tools take constrained arguments (enums where the domain is finite, per PRODUCT §9,
  [`tool-surface.md`](tool-surface.md)), so an injected instruction cannot widen what a tool reads or what
  `record_claim` / `record_gap` / `record_verdict` persists; at most it supplies values the contract admits, which the
  trace still attributes to their source. An identifier with no finite domain — an HGVS descriptor, a gene symbol —
  stays a string, and what bounds it is where the service may put it, not what it looks like.
- **No attacker-chosen destination** — the sandbox holds no GCP identity and no network of its own, so every outbound
  hop is one of our services making the request on the agent's behalf. A steered call therefore reaches neither private
  data nor a destination of the attacker's choosing: what an exposed rpc may dial comes from its own code, which is the
  criterion in [`security.md`](security.md) §What counts as an exfiltration channel.

The fresh-context reviewer is a partial backstop: it can reject a verdict the evidence does not support, but not an
injected tool call mid-gather. Hardening the injection leg beyond this is tool-surface design, owned by
[`tool-surface.md`](tool-surface.md).

## Trace integration

The trace ([`trace-schema.md`](trace-schema.md)) has two feeders, both consistent with hosting only tools and sandboxes.
They cover different halves of what a run did, and neither can supply the other's.

- **Our internal services** are where a call actually executes, so they are the only place that knows what it reached.
  Each writes a provenance-rich record host-side as the call runs — the URL, the arguments, a hash of the response, the
  version of the source database it read. That is the evidentiary half of the trace: what was looked up, where, and in
  which snapshot of the data.
- **The session client** reads Anthropic's event stream, which is the only place that knows what the *agent* did — its
  thinking, the calls it chose to make, and the tokens each turn cost — and projects that stream into the trace
  vocabulary. It reads the per-thread streams as well as the primary one: the coordinator fans out across threads, and a
  sub-agent's narration and thinking never reach the primary stream.

One limit of the second feeder bounds what per-thread cost reporting can ever show. Token and cache usage arrives on a
span that carries no thread id — neither when the stream is read at session scope nor at thread scope — so per-thread
cost cannot be summed from spans; it comes from the thread listing's own aggregated usage instead, at whatever
granularity that listing offers.

The mapping into the trace schema:

| What a feeder carries                     | Trace record                                 |
| ----------------------------------------- | -------------------------------------------- |
| A session, and each thread within it      | `AgentRun`                                   |
| A tool call                               | `ToolCall`, with its `ToolCallIntent`        |
| The agent's typed emits into our services | `EvidenceClaim`, `InformationGap`, `Verdict` |

## Model selection

The model id lives on the Agent object and is pushed at deploy time via `ant` from gcpkms-encrypted stack config — it is
secret-class confidential config (*Confidential config*, [`deployment.md`](deployment.md)): generic statements are
public, the concrete id is not. Every thread of a session runs it, the coordinator's and its copies' alike (§Topology),
so the choice is per scenario, not per role.

## Configuration and lifecycle

Control plane / data plane split, per [`spike-infrastructure.md`](spike-infrastructure.md) §6/§8: agents and
environments are version-controlled YAML applied via the `ant` CLI from CI; sessions are created and driven from the web
backend via the SDK.

Much of the lifecycle is the platform's: automatic prompt caching within a session (the session keeps the 5-minute-TTL
cache alive across the run), context compaction, and rescheduling on retryable errors. We add a per-session timeout, the
idle-break gate (break on a terminal `stop_reason`, not on transient idle), the post-idle status-write race before
cleanup, `user.interrupt` for cancellation, and handling for a stuck sub-agent thread.

## Open questions

- **A role of its own** — whether any delegated job meets §Topology's condition for a named roster entry; per-thread
  usage (§Trace integration) is the evidence and eval the arbiter.
- **Aggregator shape** — deterministic vs. agentic vs. hybrid is owned by [`aggregator.md`](aggregator.md).
- **Coordinator decomposition** — how far the coordinator should fan out versus work in fewer threads (the §11
  scaffold-vs-autonomy dial); resolve via eval.
- **Caching cadence** — what a session's automatic caching covers across a run (tool registry, ACMG-V4 framework text,
  resolved-condition context) and whether explicit cache hints are warranted.
