# Design: the agent runtime

**Related:** [`managed-agents.md`](managed-agents.md) (how the web backend creates, steers, and pages a session);
[`sandbox-worker.md`](sandbox-worker.md) (the self-hosted worker, its bubblewrap guest, and the gRPC hatch);
[`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md) (how internal RPCs are exposed to the sandbox);
[`analysis-scenarios.md`](analysis-scenarios.md) (how a scenario's kickoff and agent definition fit together);
[`conversation-view.md`](conversation-view.md) (how the workbench reads and projects a session's threads);
[`security.md`](security.md) (why sandbox egress is bounded by destination-fixedness); [`deployment.md`](deployment.md)
(confidential model configuration and deploy-time agent updates); [`spike-infrastructure.md`](spike-infrastructure.md)
(the Spike's GCP project, image builds, and network boundaries); [`../PRODUCT.md`](../PRODUCT.md) (§4 tool layer, §6
facts and judgement, §7 workspace, §9 security, §11 evaluation).

## Overview

Classifying a `(variant, condition)` case runs several distinct kinds of work over the same evidence: deterministic and
agentic evidence gathering, aggregation into ACMG-V4 evidence codes, holistic synthesis into a working document, and
adversarial review. All of these share a curated tool surface, emit structured claims and verdicts, record provenance
traces, and run under a single model configuration. Choosing the agent runtime fixes where the agent loop executes, how
multi-agent work is scheduled, how roles hand off work, and what crosses CPG's network boundary.

Anthropic Managed Agents runs the agent loop and multi-agent scheduling, while CPG hosts the tool surface and the
self-hosted execution sandbox. Within Managed Agents, a single `multiagent` coordinator agent orchestrates each case by
delegating work to sub-agent threads that are copies of itself. Every thread shares the scenario's guiding prompt, tool
surface, and model, and only the coordinator's prose brief distinguishes a literature sweep from an adversarial review.
To emit structured output without dragging the web backend into a custom-tool handling loop, the agent writes code in
its sandbox against generated gRPC stubs for our internal services, which validate the payloads against protobuf
schemas, persist them, and record provenance.

This split avoids building and operating our own loop, session store, and sub-agent scheduler during the Spike. The
parts we do build — the internal gRPC services and the data-plane mediation — are independent of the agent runtime and
carry over if we ever self-host orchestration.

## Background

In Anthropic Managed Agents, an **Agent** is a versioned control-plane resource that bundles a model, a system prompt,
attached skills, a toolset, and an optional `multiagent` configuration. A **Session** is a per-case data-plane instance
created against an Agent and an **Environment**. The platform runs the model turn loop, maintains the append-only
session event log, manages prompt caching and context compaction, and schedules sub-agent **threads** when the agent is
configured as a `coordinator`.

To keep code execution and private data inside CPG's network, sessions run against a `self_hosted` environment
([`sandbox-worker.md`](sandbox-worker.md)). Anthropic's control plane plans the turns, while our self-hosted sandbox
worker claims the session, runs the agent's commands and generated Python code under our egress policy, and returns tool
results to the session log.

```
  Curator / Workbench
          │
          ▼
   Session client (web BFF)
     creates session, sends kickoff,
     reads session & thread event logs
          │
          ▼
  Anthropic Managed Agents (hosted control plane)
     runs coordinator loop & sub-agent threads,
     holds session state & event stream
          │
          │  dispatches tool calls (self-hosted environment)
          ▼
  Execution sandbox (CPG network)
     runs shell & Python in code mode (no GCP identity);
     local proxy injects per-session credential
          │
          │  gRPC over internal network
          ▼
  Internal services (CPG network)
     validate proto schemas, hold GCP identity,
     read/write Cloud SQL & GCS, emit trace records
```

Three components on our side participate in a run:

- **Tools in code mode.** Rather than issuing long chains of discrete tool calls through the model loop, the agent
  reaches our internal gRPC services in **code mode** by writing Python scripts against generated protobuf stubs inside
  the sandbox ([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)). The internal services hold the GCP identity needed
  to read and write Cloud SQL and GCS. The sandbox itself holds no cloud credentials; a sandbox-local proxy injects a
  per-session credential on outbound calls so the agent never touches backing stores directly
  ([`spike-infrastructure.md`](spike-infrastructure.md) §8).
- **Execution sandbox.** The self-hosted worker
  ([`../../themis/services/sandbox_worker/`](../../themis/services/sandbox_worker)) executes the agent's shell commands
  and file operations inside our network and egress boundary.
- **Session client.** The web backend
  ([`../../apps/web/src/server/adapters/live/`](../../apps/web/src/server/adapters/live)) creates a session for each
  submitted Analysis, sends the scenario kickoff message, and reads the session's event stream to drive the workbench UI
  and emit traces ([`managed-agents.md`](managed-agents.md)). It is a thin client that starts and observes the session;
  it does not sequence agents or conduct workflows. Where [`spike-infrastructure.md`](spike-infrastructure.md) §8 refers
  to the web app as the "orchestrator/session-client", it means this thin session client, while data-plane mediation
  lives in the services tier.

## Design

### Managed Agents coordinator owns scheduling

A variant classification could be wired as a fixed pipeline in application code — a gather step, an aggregation step, a
drafting step, and a review step called in sequence by our backend. That rigid pipeline breaks as soon as cases vary in
shape: a well-characterised ClinVar pathogenic variant needs almost no literature fan-out, whereas a novel missense
variant with conflicting functional assays may need several rounds of targeted paper retrieval and follow-up questions
before the evidence settles.

Instead, Managed Agents runs a `multiagent` coordinator that decides scheduling per case. Given the scenario kickoff
([`analysis-scenarios.md`](analysis-scenarios.md)), the coordinator decides what to gather, when to fan out sub-agent
threads, how to aggregate and reason over the results, and when to commission a review. We supply only the scenario
specialisation: the guiding prompt and tool surface on the Agent resource, and the working document's outline in the
kickoff message. This matches [`../PRODUCT.md`](../PRODUCT.md) §4 (the orchestrator decides scheduling per case while
the framework scaffolds evidence) and §6 (model-composed workflows with just enough fixed scaffold to guarantee evidence
coverage, traceability, and evaluation).

The coordinator holds the long-running session context and delegates sub-tasks to context-isolated sub-agent threads
([Multiagent orchestration](https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration)). Each thread
has its own conversation history and shares the sandbox filesystem (`/workspace`) with the coordinator, but does not
share the coordinator's context window. The coordinator fans threads out in parallel, incorporates each summary as it
returns, and steers the next round from what came back, keeping an overview of a long case without filling its own
context window with raw search results and paper full texts. Threads persist across the session so the coordinator can
send follow-up turns to an earlier thread with its prior context intact.

Relying on the platform's coordinator accepts two platform limits: delegation is one level deep (a sub-agent thread
cannot spawn further threads), and a session may run at most 25 concurrent threads. It also adds a runtime dependency on
Anthropic's beta Managed Agents API, while keeping our internal services and sandbox mediation independent of that API.

### Every delegate is a copy of the coordinator

Managed Agents configures a coordinator with a **roster**: the list of Agent definitions it may spawn as sub-agent
threads. The obvious way to configure a multi-agent system is to define a separate Agent resource on the roster for each
role — a literature gatherer, a variant classifier, an adversarial reviewer — each with its own prompt, tool subset, and
model.

In practice, every delegated role in a classification run has to read primary evidence. A literature sweep fetches and
quotes papers; an adversarial reviewer re-reads the database records and papers that the draft cites to verify whether
they support its claims. Reading evidence requires the scenario's full tool surface (the sandbox and the internal
services reachable from it) and the guiding prompt that explains how to use those tools and interpret ACMG-V4 rules. On
Managed Agents, a roster entry that names another Agent definition inherits nothing from the coordinator: its tools,
attached skills, and model would have to be declared a second time on that definition and kept in lockstep with the
coordinator across deploys. All that duplication would buy is a narrower tool surface or a cheaper model for one role.

A Themis scenario's roster ([`../../agents/svcv4-classifier.agent.yaml`](../../agents/svcv4-classifier.agent.yaml))
therefore contains a single `self` entry naming the coordinator itself:

```yaml
multiagent:
  type: coordinator
  agents:
    - type: self
```

Every delegated thread runs the coordinator's own Agent definition — the same guiding skill, the same tool surface, and
the same model — starting from a fresh context window. The coordinator's prose **brief** is the only thing that makes
one thread a literature sweep and another an adversarial review. Each brief states what job to do, what context the
coordinator already holds, and what to report back. The brief's template lives in the guiding skill and versions with
it, while the coordinator fills in its content per delegation.

Using `self` for every delegate carries three consequences:

- **Prose-bounded roles rather than per-role tool scoping.** Every thread runs the coordinator's model with the full
  scenario tool surface; what a thread does is bounded by the prose brief rather than by platform configuration.
- **Each delegate executes one whole job.** Because the platform allows only one level of delegation, a copy cannot fan
  out further sub-threads. A job handed to a copy — such as classifying one candidate variant during a multi-variant
  case analysis — runs end-to-end inside that single thread, including its own literature sweep.
- **Threads are identified by their briefs in the UI.** Every thread reports the coordinator's own Agent name in the
  session event log, so the agent name distinguishes nothing; the workbench labels each sub-agent card with the opening
  brief the coordinator sent it ([`conversation-view.md`](conversation-view.md)).

This decision flips if either of two conditions holds in the future: if the platform lets a named roster entry inherit
the coordinator's tools and skills, or if evaluation shows a specific role benefits from a narrower tool surface or a
cheaper model. Either way, the change is confined to the Agent configuration while the thread model and briefs stay the
same.

### Mapping classification roles onto the coordinator and its copies

Rather than assigning one agent per ACMG-V4 evidence code or enforcing a fixed multi-stage pipeline, the runtime maps
the work onto the coordinator and the copies it briefs as lightly as evaluation allows:

- **Deterministic gather** (such as gnomAD allele frequencies, ClinVar structured fields, and in-silico predictor
  scores) runs as baseline annotations precomputed upstream or as direct tool calls from the coordinator, rather than as
  a separate sub-agent.
- **Agentic gather** (such as reading ClinVar free-text summaries, searching literature, and checking gene–disease
  validity) is performed either by the coordinator itself or by a delegated copy. The literature sweep is the standing
  delegation because scanning candidate papers and extracting passages produces raw text that would otherwise crowd the
  coordinator's context window.
- **Aggregation** into ACMG-V4 evidence cells is either deterministic or agentic (see
  [`evidence-interfaces.md`](evidence-interfaces.md) and
  [`svcv4-classification-widget.md`](svcv4-classification-widget.md)); either way, downstream reasoning operates on the
  underlying evidence claims and cell tags rather than on a rolled-up point total ([`../PRODUCT.md`](../PRODUCT.md) §6).
- **Reasoning and the working document** stay with the coordinator. The coordinator holds the whole case, and clinical
  judgement does not transfer from a delegate: a copy's return message is a claim that the coordinator checks against
  gathered evidence, never a conclusion it adopts blindly.
- **Adversarial review** runs in a copy with a fresh context window, briefed once the draft exists to check the draft
  against the underlying evidence and report where they diverge rather than redoing the classification from scratch.
  Separating the reviewer's context window from the drafter's is what makes adversarial review catch errors that
  self-review misses ([`../PRODUCT.md`](../PRODUCT.md) §6, §11), and the coordinator folds the reviewer's findings back
  into the working document. Managed Agents also offers an *advisor* feature — a secondary model that the primary thread
  consults mid-turn using a platform-composed prompt and no tools — but a reviewer without tools can only critique the
  draft's prose and cannot re-read the cited evidence to verify whether a paper actually says what the draft claims.

How aggressively the coordinator decomposes work into sub-agent threads versus working in fewer, broader threads is the
scaffold-versus-autonomy dial ([`../PRODUCT.md`](../PRODUCT.md) §11), tuned by evaluation and widened as models improve.

### Structured output via typed calls to internal services

The runtime needs the agent to produce structured records — evidence claims, information gaps, verdicts, and updates to
the working document — that satisfy strict schemas. Managed Agents has no per-session output-schema enforcement on model
turns, and defining client-side custom tools for structured output would force our thin session client back into an
active tool-handling loop on every emit. Scraping structured records out of free-text markdown or files after the fact
is brittle and divorces validation from the moment the agent makes the claim.

Instead, the agent emits structured output by calling our tightly-typed internal gRPC services — such as `record_claim`,
`record_gap`, `record_verdict`, or working-document write calls — directly from the sandbox in code mode. The agent
writes Python against the generated protobuf stubs ([`proto.md`](proto.md)); the call passes through the sandbox-local
proxy, which attaches the per-session credential, and the receiving internal service:

1. validates the payload against the protobuf schema ([`proto.md`](proto.md));
1. persists the record to Cloud SQL or GCS using the service's own GCP identity; and
1. writes the corresponding provenance trace record.

Putting structured output on the internal gRPC surface eliminates prose parsing, file scraping, and custom-tool
round-trips through the web backend. The working document ([`../PRODUCT.md`](../PRODUCT.md) §7) grows durably through
these typed calls.

### Defending against untrusted gathered content

ClinVar free-text submissions and full-text papers are untrusted third-party content ([`../PRODUCT.md`](../PRODUCT.md)
§9). When the coordinator or a gatherer thread reads a paper or database comment, that text can contain prompt
injections attempting to steer the model's next tool calls or corrupt its `record_*` outputs.

Rather than interposing a separate instruction-versus-data classifier inside the runtime, the design relies on two
structural boundaries already enforced by the tool surface and sandbox:

- **Typed tool contracts.** Exposed RPCs take constrained protobuf arguments, using enums wherever the domain is finite
  ([`../PRODUCT.md`](../PRODUCT.md) §9, [`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)). An injected instruction
  cannot widen what a read tool fetches or what `record_claim`, `record_gap`, or `record_verdict` persists; at most it
  can supply values that the schema already admits, which the trace still attributes to their source. Where an
  identifier has no finite domain — such as an HGVS variant descriptor or a gene symbol — the field remains a string,
  and safety rests on where the receiving service puts that string rather than on syntactic filtering.
- **No attacker-chosen destination.** The sandbox holds no GCP identity and has no direct network egress, so every
  outbound request is made by one of our internal services on the agent's behalf. A steered call therefore reaches
  neither private data nor a destination of the attacker's choosing: every exposed RPC dials only fixed upstream
  destinations hardcoded in the service implementation ([`security.md`](security.md) §"What counts as an exfiltration
  channel").

The fresh-context reviewer provides a partial backstop on the output side because it can reject a verdict that the cited
evidence does not support, though it cannot prevent an injected tool call mid-gather. Hardening the injection leg beyond
this belongs to the tool-surface design ([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md) and
[`evidence-interfaces.md`](evidence-interfaces.md)).

### Two-feeder trace integration

A complete audit trail of a classification run needs both what the external data sources returned and what the model
reasoned and decided. Because Anthropic hosts the agent loop while we host the internal services, neither side sees the
whole picture on its own.

The trace therefore combines two feeders:

- **Internal services record evidentiary provenance.** Because our services execute the actual upstream lookups and
  structured writes, they are the only place that sees the exact wire call. As each RPC runs, the service writes a
  provenance record host-side capturing the upstream URL, request arguments, a hash of the response, and the version or
  snapshot of the source database.
- **The session client projects agent activity.** Anthropic's session event stream is the only place that records what
  the model did — its thinking blocks, the tool calls it chose to invoke, the threads it spawned, and the token cost of
  each turn. The session client reads both the primary session event stream and each spawned sub-agent thread's event
  stream (since a sub-agent's narration and thinking are not cross-posted to the primary stream; see
  [`conversation-view.md`](conversation-view.md)) and projects them into the trace schema.

One platform limitation constrains per-thread cost attribution from the second feeder. Token and cache usage events
arrive on `span.model_request_end` records that carry no `session_thread_id`, whether read from the primary session log
or from a thread's own log. Per-thread token cost therefore cannot be summed from individual turn spans; it must be read
from the thread listing's aggregated usage object at whatever granularity that listing provides
([`cost-monitoring.md`](cost-monitoring.md)).

Together, the two feeders map into the trace schema:

| Feeder                             | What the feeder carries                        | Trace record                                 |
| ---------------------------------- | ---------------------------------------------- | -------------------------------------------- |
| Session client                     | A session and each sub-agent thread within it  | `AgentRun`                                   |
| Session client + internal services | A tool invocation                              | `ToolCall`, paired with its `ToolCallIntent` |
| Internal services                  | The agent's typed emits into internal services | `EvidenceClaim`, `InformationGap`, `Verdict` |

### Confidential model selection per scenario

Under our confidential configuration policy ([`deployment.md`](deployment.md) §"Confidential config"), our general
family and tier of model is public, but the exact model identifier string is secret-class configuration. Storing the
model ID in the version-controlled agent declaration would expose it in the repository.

The tracked agent declaration ([`../../agents/svcv4-classifier.agent.yaml`](../../agents/svcv4-classifier.agent.yaml))
therefore specifies every model setting (such as `effort`) except `model.id`, and the loader in
[`../../tools/agents/`](../../tools/agents) rejects any YAML file that includes `model.id`. The concrete model ID lives
in `gcpkms`-encrypted Pulumi stack config (`themis:anthropicAgentModelId`) and is injected by `tools/agents` when the
deploy workflow applies the declaration. Because every sub-agent thread is a `self` copy of the coordinator, every
thread in a session runs this same model, making model selection a per-scenario choice rather than a per-role choice.

### Configuration and session lifecycle

The runtime separates control-plane declarations from data-plane sessions
([`spike-infrastructure.md`](spike-infrastructure.md) §6, §8):

- **Control plane.** Each scenario's agent is declared in version-controlled YAML under [`../../agents/`](../../agents)
  and applied as a whole by the deploy workflow through [`../../tools/agents/`](../../tools/agents). When custom skill
  directories under `agents/skills/` change, `tools/agents` publishes a new immutable skill version, pins the Agent
  resource to that version, and records the content digest and git commit in the Agent's metadata so unchanged deploys
  are no-ops. The `self_hosted` Environment resource
  ([`../../agents/selfhosted.environment.yaml`](../../agents/selfhosted.environment.yaml)) is created once out of band.
- **Data plane.** The web backend creates and drives a Session per Analysis through the Anthropic SDK
  ([`managed-agents.md`](managed-agents.md)), while the self-hosted sandbox worker claims and executes the session's
  sandbox work ([`sandbox-worker.md`](sandbox-worker.md)).

Managed Agents handles intra-session mechanics automatically: it keeps the 5-minute-TTL prompt cache warm across turns
within a session, compacts context when a thread grows long, and reschedules turns on retryable upstream errors. On top
of the platform's loop, our session runners
([`../../apps/web/src/server/adapters/live/`](../../apps/web/src/server/adapters/live) and
[`../../tools/run_review/run/`](../../tools/run_review/run)) add five lifecycle controls:

1. **Per-session timeout** to bound total wall time on runaway runs.
1. **Terminal idle-break gate**, distinguishing a transient `session.status_idle` with `stop_reason: requires_action`
   (while the worker is executing a tool call) from a terminal `end_turn` or `retries_exhausted`.
1. **Post-idle settle handling**, accounting for the status-write and workspace-push window after the session idles
   before tearing down or collecting the run.
1. **Cancellation via `user.interrupt`**, allowing a curator or evaluation harness to halt an in-flight step cleanly.
1. **Stuck sub-agent thread handling**, bounding how long the runner waits when a child thread fails to return or idle.

## Alternatives considered

### Agent SDK with Dynamic Workflows in our own container

Anthropic's Agent SDK can run the agent loop inside our own container, and its Dynamic Workflows feature lets the model
author a JavaScript orchestration script that fans out sub-agents. That offers model-composed orchestration without
depending on the hosted Managed Agents session service.

However, running the Agent SDK in our container means we own the turn loop, session state persistence, scaling, and —
the deciding cost — a runtime inside our infrastructure to execute the model-generated workflow script and spawn further
agents from the sandbox. That pulls agent-launching authority into the hardened execution sandbox that we want to keep
minimal ([`sandbox-worker.md`](sandbox-worker.md)). Furthermore, a Dynamic Workflow fixes its decomposition plan at the
moment the model writes the script, whereas the Managed Agents coordinator decides each next delegation dynamically from
the actual results returned by earlier threads. Dynamic Workflows remains a viable fallback if Themis ever needs to move
off Managed Agents and self-host orchestration.

### Custom loop over the Messages API

Building our own turn loop directly on the raw Messages API avoids the beta Managed Agents surface and gives full
control over event storage and thread scheduling. It loses because it forces us to build and operate the loop, tool
dispatch queue, sub-agent scheduler, context compaction, and session store from scratch before we can evaluate a single
variant classification.

### Claude Code as a headless subprocess

Invoking the `claude` CLI headlessly inside a container and reading its stdout is fast to prototype for single-turn
coding tasks. For Themis, it loses structured output guarantees, provides no native multi-agent thread API for the
workbench to project, and turns trace and conversation extraction into brittle stdout scraping.

### LangGraph, DSPy, or Inspect-AI

Graph-based and declarative agent frameworks provide ready-made abstractions for state machines (LangGraph), prompt
optimisation (DSPy), or evaluation harnesses (Inspect-AI). For the production runtime, LangGraph adds ceremony out of
proportion to the Spike, DSPy's declarative-program model is premature, and Inspect-AI is evaluation-shaped rather than
a production runtime (though Inspect-AI is the likely tool for the evaluation exploration rather than the runtime).

## Open questions

- **Dedicated roster entries for specific roles.** Whether any delegated role eventually warrants its own named Agent
  entry on the coordinator's roster (for a narrower tool surface or a smaller model), judged by per-thread token usage
  and evaluation quality.
- **Aggregator shape.** What mix of deterministic library arithmetic versus agentic interpretation belongs in the
  aggregation step across all ACMG-V4 evidence families (see [`evidence-interfaces.md`](evidence-interfaces.md) and
  [`svcv4-classification-widget.md`](svcv4-classification-widget.md)).
- **Coordinator decomposition granularity.** How far the coordinator should fan out into narrow threads versus working
  in fewer, broader threads along the scaffold-versus-autonomy dial ([`../PRODUCT.md`](../PRODUCT.md) §11), to be set by
  evaluation.
- **Prompt caching coverage.** Exactly which parts of a run (tool definitions, ACMG-V4 framework reference text,
  resolved condition context) stay warm under Managed Agents' automatic session caching, and whether explicit cache
  breakpoints are warranted.
