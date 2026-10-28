# Design: sandbox RPC exposure

**Related:** [`sandbox-worker.md`](sandbox-worker.md) (the hatch the guest reaches services through, and the worker that
forwards for it), [`services.md`](services.md) (the service pattern whose RPCs are exposed),
[`security.md`](security.md) (the trust boundary and the exfiltration criterion for outbound calls),
[`proto.md`](proto.md) (the proto-first codegen pipeline this extends),
[`literature-evidence-layer.md`](literature-evidence-layer.md) (the first service called by both the agent and the web
tier).

## Overview

The sandboxed agent has no network access and holds no credentials. Its only way to reach Themis's internal gRPC
services is the hatch, a local Unix-socket proxy served by the trusted worker process outside the sandbox. The hatch
admits a fixed allowlist of RPCs and rejects everything else. This document decides how an RPC enters that allowlist,
what security properties an exposed RPC must uphold, and what a session token means when a call reaches a data-plane
service.

An RPC becomes callable from the sandbox when its protobuf declaration carries the `agent_exposed` method option. From
that single option, `regen` derives the hatch allowlist, a pruned copy of the `.proto` sources for the model to read,
the Python stubs compiled from those pruned sources, and the service accessors the agent imports. What cannot be
generated — the worker's per-service forwarder and the agent's prompt instructions — is held to the allowlist by
automated checks. Leaving the option off leaves the RPC unreachable, so the surface is fail-closed by default. Marking
an RPC asserts that its implementation is written for a hostile caller: no single call or stream of calls can trigger
unbounded work, and every outbound request carrying caller-supplied text goes to a fixed destination as a query-only
parameter.

Every call the worker forwards from the sandbox carries the session token of the running Analysis, whereas the web
tier's backend (the BFF) calls data-plane services directly on its own service identity. Rather than treating the
session token as an access ticket that every RPC verifies and discards, an RPC resolves the caller's session only when
its answer depends on the session's Project scope, planned evaluation data-cutoff masking, or spend attribution, and
fails loudly when a required session is absent. An RPC whose answer does not depend on the session does not ask for one,
and any RPC called by both the agent and the BFF answers both callers identically.

## Background

The agent's code runs inside the *guest*, a sandboxed process with no network interface and no credentials
([`sandbox-worker.md`](sandbox-worker.md)). Its one exit is the *hatch*, a gRPC server listening on a Unix domain socket
inside the sandbox and run by the trusted *worker* process outside it in
[`themis/services/sandbox_worker/`](../../themis/services/sandbox_worker/). The hatch checks each incoming method path
against a generated allowlist and returns `PERMISSION_DENIED` for any unlisted method. For allowlisted methods, the
worker registers one *forwarder* per exposed service. The forwarder attaches the session's token as request metadata and
forwards the call over a channel authenticated with the worker's own service identity. Because the guest has no network,
an exposed RPC that queries an external system is the only path by which text composed inside the sandbox can leave the
perimeter. Whether such an RPC can act as an exfiltration channel depends on its implementation: as defined in
[`security.md`](security.md) (§What counts as an exfiltration channel), an outbound request is safe only when its
destination is fixed by constants in code and caller-supplied text is passed as an encoded parameter that selects a
response without naming a host, a route, or content the upstream stores.

Two callers reach Themis's data-plane services, and they sit on opposite sides of the trust boundary:

```
  Sandboxed guest (untrusted model code)
        |
        |  1. gRPC call over Unix socket (/package.Service/Method)
        v
  Worker hatch (checks generated allowlist; injects session token + worker IAM identity)
        |
        |  2. Forwarded gRPC call
        v
  Data-plane service  <---  Direct gRPC call (BFF IAM identity; session token only when needed)
                                  |
                            Web tier BFF (trusted first-party service)
```

The first caller is the agent, reaching the service through the hatch; its code is written by the model and shaped by
untrusted content it has read (papers, database records, and case files), so the service must treat its requests as
potentially hostile. The second caller is the BFF, a trusted first-party service that calls data-plane services directly
using its own IAM identity to serve the browser. Reaching a service at all requires a Cloud Run IAM grant, which both
the worker's identity and the BFF's identity hold.

When an Analysis ([`GLOSSARY.md`](../../GLOSSARY.md)) is created, the auth service mints a session token that resolves
back to the Project and Analysis it belongs to through the servicer-side client in
[`themis/clients/auth/`](../../themis/clients/auth/). Only trusted processes ever hold a session token: the worker holds
the token for the single session it runs, and the BFF can derive the token for any Analysis it serves. The guest never
sees a token, so it cannot forge, replay, or choose one.

Inside the guest, the agent calls services through generated Python gRPC stubs. Standard `protoc` code generation emits
a stub for an entire `service` block and cannot emit a subset of a service's methods. Because `protoc` emits exactly
what its input `.proto` files declare, giving the guest a callable stub that is narrower than a full service requires
pruning the `.proto` source before `protoc` compiles it.

## Non-goals

- **Filtering responses by caller.** An RPC answers the agent and the BFF identically. If the agent requires a narrower
  view of data that a trusted caller reads in full, that view is exposed as a separate RPC. Putting per-caller filtering
  between the guest and the service would push policy into the worker's forwarder, which is pass-through boilerplate
  where domain policy does not belong.
- **Enforcing user-level paper licensing.** Whether a curator may open a licensed paper's full text depends on their
  institutional subscriptions, which are bound to the individual user rather than to the Project or the session
  ([`PRODUCT.md`](../PRODUCT.md) §7). Only the BFF knows the user, so enforcing institutional licensing belongs on the
  BFF's path to the literature service and is left to the literature layer's design.

## Design

### The rpc is the unit of exposure

The obvious way to expose a backend service to the sandbox is to mark its `.proto` file or `service` block as exposed
and copy the generated stub into the guest rootfs. That breaks as soon as one service has methods for both the agent and
the browser. The `Literature` service ([`literature.proto`](../../schema/proto/themis/rpc/literature.proto)) covers one
domain — papers, their renderings, and quoted citations — across eleven RPCs. Eight of those RPCs serve the agent
(searching indexes, ingesting papers, polling full-text readiness, reading markdown, and validating a quote), while
three serve only the browser's document pane: `DescribePaper` lists a paper's renderings and supplementary files,
`ResolveContent` returns the Cloud Storage URI of a rendering for the BFF to presign, and `Locate` returns the bounding
offsets or PDF coordinates of a quote for the browser highlighter. Marking the whole file or service would expose all
eleven methods, offering the agent a storage URI it cannot follow and has no reason to see; avoiding that under a
file-level mark would force splitting one coherent service into two files solely by who calls it.

Exposure is therefore declared on each RPC using the `agent_exposed` method option defined in
[`sandbox_options.proto`](../../schema/proto/themis/rpc/sandbox_options.proto), which every service proto that exposes
an RPC imports:

```proto
import "themis/rpc/sandbox_options.proto";

service Literature {
  // Whether a quote locates in the paper, for the agent to check while authoring.
  rpc Validate(ValidateRequest) returns (ValidateResponse) {
    option (themis.rpc.agent_exposed) = true;
  }
  // The storage object a rendering lives in, for the BFF to serve. Not exposed: nothing in the
  // guest can follow a location.
  rpc ResolveContent(ResolveContentRequest) returns (ContentLocation);
}
```

Putting the mark on the RPC matches where the security argument lives. An RPC's cost bounds and outbound destinations
belong to that method's implementation, so the change that marks an RPC argues those properties for that specific RPC.

Because the option is compiled into the protobuf descriptor set, `regen` ([`tools/schema/`](../../tools/schema/)) reads
it at build time. Omitting the option defaults to false, so a file, service, or method without it contributes nothing to
the agent's callable surface. From the marked RPCs, `regen` derives four committed artifacts that CI holds fresh:

| Artifact            | What `regen` derives                                                                                                                                            |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hatch allowlist     | The `/package.Service/Method` path of every marked RPC.                                                                                                         |
| Guest contract tree | A pruned copy of the `.proto` sources containing only marked RPCs, the message and enum types they reach, and the imports those types still need.               |
| Guest stubs         | `protoc`'s Python output compiled from the guest contract tree, so each service stub offers only the marked RPCs.                                               |
| Guest accessors     | One function per service with a marked RPC (imported in the guest as `themis.agent.services`), returning that service's stub bound to the shared hatch channel. |

Two pieces remain hand-written by design and are verified against the allowlist by tests rather than generated: the
worker's per-service forwarder (which provides the deliberate second edit described below), and the agent's prompt
instructions, whose every named RPC is checked against the allowlist so the model is never told about a method the hatch
will refuse.

#### Cutting the guest contract tree

Rather than generating stubs over the full contract and wrapping them in a filtered facade, `regen` first derives the
contract as the guest should see it and runs `protoc` over that pruned tree:

```
  schema/proto/*.proto (full contract: 11 Literature RPCs, all types, sandbox_options import)
        |
        |  regen cuts unmarked RPCs, unreachable types, and option marks using descriptor spans
        v
  guest_contract/proto/*.proto  --->  Copied to guest rootfs (read by the model for comments)
        |
        |  protoc compiles pruned sources (verified against full contract's declarations & comments)
        v
  guest_contract/python/*_pb2*.py ->  Copied to guest site-packages (imported by agent code)
```

`regen` makes its cuts in the `.proto` source text using the line and column spans that `protoc` records in the
descriptor's `SourceCodeInfo` — each declaration's span, its leading and trailing comments, and each option's span —
rather than parsing the protobuf grammar itself. Beyond deleting unmarked RPCs, unreachable types, unused imports, and
the `agent_exposed` option lines, the only text transformation collapses an RPC body `{}` that removing the option left
empty; copies of upstream record schemas from third-party wheels stay intact. `regen` then recompiles the cut sources
with `protoc` and checks that every kept declaration and its attached comments match the full contract, failing the
build if a cut dropped a needed type or left an orphaned comment beside the wrong declaration.

The resulting guest contract tree gives the sandbox one consistent surface with two readers: the model reads the pruned
`.proto` files at a fixed path in the guest rootfs to see method and field comments, and the Python code the model
writes imports the stubs compiled from those exact files. Inside the sandbox, no trace of the `agent_exposed` option or
`sandbox_options.proto` remains. Because unreachable types are removed, a file imported only for its messages ships only
the messages a kept RPC reaches, and no internal-only service stub ever enters the rootfs. Every message type that a
kept RPC *does* reach ships with all of its fields intact — cuts remove whole declarations, never fields inside a
message — so wire responses from the full server parse without change. The guest contract tree provides accurate
discoverability for the model, while the hatch allowlist remains the enforcement boundary: if guest code hand-crafts a
gRPC call to an unlisted method path, the hatch rejects it with `PERMISSION_DENIED`.

#### Keeping the option from becoming a rubber-stamp

Four rules ensure that marking an RPC is a deliberate security review rather than an accidental one-line schema edit:

- **Fail-closed default.** Omitting `agent_exposed` leaves the RPC off the allowlist and out of the guest contract tree.
- **Two-edit ceremony across schema and service.** Marking an RPC in `.proto` expands the generated allowlist and guest
  contract tree, which causes the sandbox worker's test suite in
  [`themis/services/sandbox_worker/`](../../themis/services/sandbox_worker/) to fail until a forwarder covers the
  service and the guest Dockerfile copies its stub. Exposing a service therefore requires a second edit outside
  `schema/`. The intended form of that second edit on the backend is a co-signature decorator (`@agent_exposed`) on the
  hand-written servicer method that implements the RPC's hardening, cross-checked against the proto descriptor in CI so
  the security assertion sits directly on the code that upholds it.
- **Visible allowlist diff.** Because the generated allowlist is committed to the repository, widening the sandbox's
  reach always appears in code review as an explicit diff of `/package.Service/Method` strings.
- **Marked failure text.** When an exposed RPC fails, the hatch forwarder passes the upstream error message to the guest
  only if the servicer explicitly marked the status as authored, and its text is held to what
  [`sandbox-worker.md`](sandbox-worker.md#the-hatch-is-the-capability-boundary) says it may carry; a servicer that
  aborts without the mark (or any unmarked channel, routing, or authentication diagnostic generated by gRPC or Cloud
  Run) leaves the agent a bare status code so internal infrastructure details never leak into the sandbox.

### Security: what an exposed rpc asserts

Because the untrusted agent invokes exposed RPCs using arguments shaped by unvetted external content — papers, database
records, and case files ([`PRODUCT.md`](../PRODUCT.md) §9) — every marked RPC must be written for a hostile caller.
Marking an RPC asserts that **nothing it does on the caller's behalf has unbounded cost, and every outbound request it
makes carrying caller-supplied text is destination-fixed and query-only.**

Bounding cost requires two controls, and a marked RPC must have both:

1. **Per-call cap.** The RPC implementation bounds the work a single call can start, such as a maximum batch size, a
   result-count ceiling, or a byte read limit.
1. **Cross-call pacing.** The deployment behind the RPC bounds what a stream of calls can accumulate, such as a queue's
   concurrency limit or an upstream client's rate limit.

Every change that marks an RPC must name both bounds. A per-call cap without pacing leaves a tight loop in the sandbox
free to hammer an upstream or enqueue unbounded work, while pacing without a per-call cap leaves the first oversized
request free to exhaust memory or upstream quota.

The outbound destination clause governs the agent's one path to third-party networks. It sits on the RPC implementation
rather than on the hatch forwarder because the forwarder only sees gRPC messages and cannot inspect how the servicer
constructs an outbound HTTP request. Whether an outbound URL is built from constants in code or from data fetched during
the call is a property of the servicer's code. Any RPC that builds a URL from fetched data — reading a host out of a
database record, following a link from a search result, or chasing an HTTP redirect — fails the exfiltration criterion
in [`security.md`](security.md) and cannot be marked `agent_exposed`.

Two internal services fail these conditions outright and carry no marks: `auth` and `sheaf` are infrastructure services
that are not written for a hostile caller.

### A session is context

The obvious rule for authorizing calls to a data-plane service is to require a valid session token on every exposed RPC.
Consider what a blanket session check actually guards. The guest cannot present a token that the worker did not inject,
so the check cannot catch a forged or borrowed session from the sandbox. Cloud Run IAM already restricts which service
identities can reach the server, so the check cannot stop an outside caller. A check that verifies the session token and
immediately discards the resolved binding protects nothing, and reads to the next maintainer as an incomplete guard.
Worse, stating the check as a blanket property of the RPC forces trusted callers that share the RPC to mint and carry
session tokens that the RPC never uses — which is how the BFF came to derive session tokens for browser reads of the
shared literature corpus.

An RPC therefore resolves the caller's session token only when its answer depends on the session, and fails loudly
without one. An RPC whose answer does not depend on the session does not ask for one. An RPC's answer can depend on a
session in three ways:

- **Scope.** When reading Project-bound data — case genomes, patient phenotypes, or anything inside a Project's access
  boundary ([`PRODUCT.md`](../PRODUCT.md) §7) — the RPC derives the Project scope from the resolved session token
  server-side, never from a Project identifier chosen by the caller.
- **Cutoff.** When querying a time-varying source or running a versioned computational tool, the RPC answers as of the
  session's evaluation data cutoff, described below.
- **Attribution.** When starting paid work — such as a full-text PDF conversion or a fan-out to a metered upstream — the
  RPC records which Analysis initiated the work so spend can be attributed per Analysis.

Failing loudly when a required session is missing is load-bearing. An answer that should have been scoped to a Project,
masked to a cutoff date, or attributed to an Analysis — and instead ran unscoped or unmasked — is a wrong answer that
looks right ([`docs/style/general.md`](../style/general.md)). Accordingly, an RPC that uses session context aborts any
call that carries no session token with an error status rather than falling back to a default.

Each RPC declares its session requirement in its `.proto` comment, stating for callers what the session is used for and
that a call without one is refused. This requirement is not a proto option today. A proto option enforces nothing by
itself; its one benefit is letting a descriptor-driven test detect a missing session check, which only pays off once a
single service has RPCs that resolve a session alongside RPCs that do not. The `Literature` service reaches that split
when its data cutoff lands — the eight RPCs the agent calls will resolve the session for the cutoff, while the three
browser-only RPCs will not — so an option belongs with that cutoff change rather than with `agent_exposed`. Until the
cutoff lands, `Literature` resolves a session only where it enqueues a conversion, and the other nine evidence services
keep their existing session gates on every RPC rather than removing a gate only to re-add it when the cutoff gives it a
use.

#### Masking is the first use

An evaluation runs the agent over a historical variant or case whose clinical classification is already known. That
benchmark is meaningless if the agent can read the later ClinVar record, paper, or model release that settled the case.
An evaluation session will therefore carry a **data cutoff** date, and every evidence RPC the agent calls will apply it
so that databases answer as of that date and computational tools run at versions consistent with it.

Each time-varying evidence source masks its records by the date that reflects when the fact became public:

- ClinVar records, by the date the submission was evaluated;
- Literature indexes and the full-text corpus, by publication date;
- Population allele frequencies (gnomAD), by release;
- Functional-assay deposits (MaveDB), by deposit date;
- Gene-disease curations, by assertion date;
- ACMG/ClinGen specifications (CSpec), by version.

Computational tools (`Variant`, `Vep`, `Transcript`, and `Splice`) are pinned to versions consistent with the cutoff
date even though their output is a deterministic function of their input. A splice model trained on variants deposited
after the cutoff carries the knowledge the cutoff exists to withhold, and a later release of an annotation tool can
change what it reports for the same variant. Normalisation, effect and splice prediction, and transcript structure
therefore resolve session context just as the database services do; the only exposed RPC that takes no session context
is the `Hello` smoke test.

When a record or paper falls after the session's cutoff date, the RPC reports it as absent or unknown rather than
refusing the call. A ClinVar submission evaluated after the cutoff, or a paper published after it, is indistinguishable
from a record that never existed. Returning a refusal that said "exists, but after your cutoff" would leak the existence
of post-cutoff evidence on that variant, handing the agent the signal the cutoff is meant to withhold — the same
non-existence rule the workspace model applies across Projects ([`workspace-model.md`](workspace-model.md)).

The cutoff date is stored as an immutable attribute of the session record. It is written once when the evaluation
harness (such as the run-review loop) creates the Analysis, and comes back alongside the Project and Analysis IDs
whenever a service resolves the session token. The cutoff is never passed as a request argument — because the agent
under evaluation has every incentive to advance the date — and never injected as a separate metadata header by the
worker, because a value resolved from the auth service's session record is pinned and auditable whereas a request header
is whatever the sender sent. Because an RPC that applies a cutoff cannot tell whether a cutoff applies without resolving
the session, it refuses any call that arrives without a session token, preventing an evaluation run from silently seeing
future data. Which date a given evaluation pins (for example, the date a curator recorded the reference classification)
is chosen by the harness that creates the session.

### Callers

The worker injects the session token into every call forwarded from the guest, and the guest has no other path out of
the sandbox. Whether the receiving RPC resolves that token or ignores it is the RPC's decision under the rule above, and
the guest's code sees no difference either way.

The BFF presents a session token only when calling an RPC that uses session context, deriving the token for the Analysis
the work belongs to. When calling an RPC that takes no session context, the BFF presents only its own IAM service
identity. The browser's document-pane reads (`DescribePaper`, `ResolveContent`, and `Locate`) take no session context:
the literature corpus is shared across Projects, a curator opening a paper in the browser is not an evaluation subject,
and reading an existing rendering starts no paid work. Consequently, the browser-to-BFF API for those reads names no
Analysis, and the BFF derives no session token for them.

Any RPC reachable by both callers answers both identically. Under data-cutoff masking, an RPC that uses session context
can be shared only with a caller that presents a session token; a caller that presents none is refused rather than
answered unmasked. That rule lets each domain service remain a single service: the difference between the agent and the
BFF is which RPCs each caller invokes and what credentials each carries, never how an RPC computes its answer. In the
`Literature` service, the two callers' RPC sets are disjoint: the eight RPCs the agent calls will all take the cutoff
(and therefore require a session), and the three RPCs the browser calls take no session.

### The literature service as a worked instance

The `Literature` service ([`literature.proto`](../../schema/proto/themis/rpc/literature.proto)) illustrates how per-RPC
exposure, guest contract pruning, and session context fit together across one service's eleven RPCs:

| RPC group                  | Methods                                                                                                                     | `agent_exposed` | In guest contract tree? | Session context used for          |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------- | --------------- | ----------------------- | --------------------------------- |
| Agent discovery and store  | `SearchEuropePmc`, `FetchPubmedArticles`, `SearchLitVar`, `ListLitVarEntities`, `PollFullTexts`, `ReadMarkdown`, `Validate` | Yes             | Yes                     | Data cutoff masking               |
| Agent ingestion            | `MaybeIngestPapers`                                                                                                         | Yes             | Yes                     | Data cutoff masking + attribution |
| Browser document pane only | `DescribePaper`, `ResolveContent`, `Locate`                                                                                 | No              | Cut (with their types)  | None (BFF IAM identity only)      |

Each of the eight agent-callable RPCs carries `agent_exposed = true`, and the change that marks them states the per-call
cap, cross-call pacing, and fixed outbound destination for each. Because `DescribePaper`, `ResolveContent`, and `Locate`
are unmarked, the guest's `.proto` catalog and generated stubs never list them, and the message types used only by their
replies — such as `PaperInfo`, `ContentLocation`, `LocateResponse`, `TextOffsets`, and `PdfRegion` — are cut from the
guest contract tree.

On the session side, `MaybeIngestPapers` starts paid full-text production and passes `AuthContext` into
`LiteratureBackend.view_for` as the seam to attribute conversion and ingestion spend to the Analysis that requested it
*(intended; recording and charging are not yet wired)*. Once data-cutoff masking lands, all eight agent-callable RPCs
will filter by publication date so that any paper published after the session's cutoff is reported as absent or unknown
— including on `MaybeIngestPapers` and `PollFullTexts`, ensuring that a post-cutoff PMID the model recalls from
pre-training buys it nothing. Meanwhile, the three document-pane reads made by the BFF carry no session token at all.

## Alternatives considered

- **A file-level exposure option.** Marking whole `.proto` files guarantees that `protoc`'s generated stub matches the
  exposed surface without source pruning. It was rejected because callers cross-cut a single domain service: three of
  `Literature`'s eleven RPCs belong to the browser alone, so a file-level option would either expose storage URIs to the
  agent or force splitting one coherent service file by caller. Generating the pruned guest contract tree preserves
  exact stubs while keeping the service in one file.
- **A service-level exposure option.** Marking a `service` block is simpler than marking each method, but fails for the
  same reason as a file-level option whenever one service serves both the agent and the BFF.
- **A generated facade wrapping `protoc`'s full-service stub.** Generating a wrapper class that exposes only the marked
  methods avoids pruning `.proto` files before running `protoc`. It was rejected because it introduces a second client
  abstraction to explain to the model, strips the RPC docstrings from the callable methods, and still ships all
  unexposed message types, comments, and `sandbox_options.proto` imports inside the guest rootfs beneath the wrapper.
  Cutting the contract before `protoc` runs gives the model one artifact that matches its callable surface at every
  layer.
- **Pruning the compiled `FileDescriptorProto` and feeding it back to `protoc`.** Filtering the descriptor tree in
  memory avoids slicing source text. It was rejected because `SourceCodeInfo` addresses comments by numeric path index
  into the descriptor's repeated fields; dropping an RPC or message shifts indices and attaches the dropped
  declaration's comments onto its neighbour unless every path index is remapped, and a binary descriptor still cannot
  directly produce the `.proto` text files the model reads. The same `SourceCodeInfo` spans that a descriptor remap
  would need are enough to cut the `.proto` text directly.
- **A blanket session gate on every exposed RPC.** Requiring every `agent_exposed` RPC to verify a session token is
  uniform across all services. It was rejected because the guest cannot forge a token that the worker did not inject,
  IAM already restricts which callers can reach the service, and verifying a token only to discard the binding guards
  nothing. Stated as a property of the RPC, a blanket session gate also forced trusted callers sharing the RPC to derive
  session tokens for reads of a shared corpus.
- **No session tokens at the data plane at all.** Relying solely on Cloud Run IAM and the hatch allowlist eliminates
  session resolution in data-plane services. It was rejected because Project scoping, evaluation data-cutoff masking,
  and spend attribution all require knowing which Analysis a call belongs to, and resolving the session where the answer
  depends on it admits those cases without imposing a blanket check.
- **Applying the data cutoff to databases but not to computational tools.** Filtering only database records is simpler
  than pinning tool versions. It was rejected because a splice predictor trained on later variants or a newer annotator
  release embeds post-cutoff knowledge, and an evaluation cannot distinguish leakage through a tool from leakage through
  a database lookup.
- **Injecting the data cutoff as request metadata from the worker.** Having the worker send a `cutoff-date` header on
  forwarded calls avoids looking up the cutoff through the auth service. It was rejected because request metadata is an
  unverified claim from the sender, whereas an attribute on the auth service's session record is immutable and
  auditable, and because any shared RPC called by the BFF would otherwise require the BFF to look up and inject the
  header as well.
- **Declaring exposure with `.proto` comments or magic tags instead of a method option.** Magic comments require no
  `sandbox_options.proto` import. They were rejected because comments are stripped from the compiled descriptor,
  invisible to `buf lint` and `buf breaking`, require source scraping, and cannot express a fail-closed default — a
  missing comment is indistinguishable from a forgotten one.
- **A generic byte-forwarding proxy in the hatch.** A single generic handler could route every allowlisted method path
  without hand-written per-service forwarder classes. It was rejected because `postern`'s hatch API supports only
  concrete servicer registration, and even with an upstream API change a generic proxy would still need descriptor
  lookup for unary versus streaming cardinality plus custom method-to-channel routing. Subclassing the generated
  servicer base per service uses the existing API and inherits static typing and streaming support.
- **A hand-authored allowlist verified against the proto in CI.** Maintaining the allowlist by hand makes every newly
  exposed RPC an explicit code edit. It was rejected because deriving the allowlist from the proto is correct by
  construction, while the committed allowlist diff and the servicer co-signature decorator provide the same deliberate
  review friction.
- **Using the worker's runtime forwarder registry as the single source of truth.** Deriving the allowlist from the
  forwarders registered in Python avoids a custom proto option. It was rejected because the guest rootfs is built at
  image build time before any worker process exists and cannot import the worker's runtime modules, whereas the `.proto`
  contract is readable statically at build time.
- **Explicitly marking every guest-visible `.proto` file, including message-only files.** Requiring an option on every
  shipped file avoids computing a transitive type closure. It was rejected because the set of messages a marked RPC
  needs is already determined by its request and response types, so marking message files by hand is redundant and can
  fail by omitting a transitive dependency.
- **Enforcing the guest shipping closure as a `buf lint` rule.** Running the check inside `buf lint` keeps all schema
  checks in one tool. It was rejected because built-in `buf` rules cannot express a cross-file reachability closure, and
  a custom lint plugin would require building and pinning a separate binary, whereas `regen` already walks the compiled
  descriptor set and is gated by CI freshness checks.

## Open questions

- **User entitlement on the browser leg.** How the BFF and the literature service check a curator's institutional
  licensing before opening a subscription paper in the browser is outside this document's scope and remains to be
  designed in the literature layer.
- **Servicer co-signature decorator wiring.** Which module defines the `@agent_exposed` decorator placed on hand-written
  servicer methods, and how the CI check imports the servicer implementations to cross-check them against the proto
  descriptors, remains undecided.
